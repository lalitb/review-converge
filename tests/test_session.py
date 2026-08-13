import contextlib
import io
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from review_converge.core import ConvergeError
from review_converge.models import InvocationResult
from review_converge.session import Session, execute_command, session_main


class SessionTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        subprocess.run(
            ["git", "init", "-b", "main"],
            cwd=self.repo,
            check=True,
            capture_output=True,
        )
        subprocess.run(
            ["git", "config", "user.email", "test@example.com"],
            cwd=self.repo,
            check=True,
        )
        subprocess.run(
            ["git", "config", "user.name", "Test"], cwd=self.repo, check=True
        )
        (self.repo / "value.txt").write_text("old\n", encoding="utf-8")
        subprocess.run(["git", "add", "value.txt"], cwd=self.repo, check=True)
        subprocess.run(
            ["git", "commit", "-m", "initial"],
            cwd=self.repo,
            check=True,
            capture_output=True,
        )
        self.directory = self.root / "session"

    def tearDown(self):
        self.temporary.cleanup()

    def test_create_load_and_mode_changes_are_durable(self):
        session = Session.create(self.directory, self.repo, "main")
        self.assertEqual(session.mode, "review")
        session.set_mode("propose")

        resumed = Session.load(self.directory)
        self.assertEqual(resumed.mode, "propose")
        events = [
            json.loads(line)
            for line in (self.directory / "events.jsonl").read_text().splitlines()
        ]
        self.assertEqual(events[-1]["kind"], "mode_changed")

    def test_session_artifacts_must_be_outside_checkout(self):
        with self.assertRaisesRegex(ConvergeError, "outside the reviewed checkout"):
            Session.create(self.repo / ".session", self.repo, "main")

    def test_capabilities_prevent_propose_and_apply_by_default(self):
        session = Session.create(self.directory, self.repo, "main")
        with self.assertRaisesRegex(ConvergeError, "requires 'propose'"):
            session.invoke("change it", kind="proposal", provider="codex:model")
        with self.assertRaisesRegex(ConvergeError, "requires 'edit'"):
            session.apply("proposal-001", confirmed=True)

    def test_proposal_is_recorded_but_not_applied(self):
        session = Session.create(self.directory, self.repo, "main")
        session.set_mode("propose")
        adapter = mock.Mock()
        adapter.cli_version = "codex test"
        adapter.invoke.return_value = InvocationResult(
            {
                "summary": "Update value",
                "patch": "diff --git a/value.txt b/value.txt\n"
                "--- a/value.txt\n+++ b/value.txt\n@@ -1 +1 @@\n-old\n+new\n",
                "risks": [],
                "verification": ["Inspect value.txt"],
            }
        )
        with mock.patch("review_converge.session.make_adapter", return_value=adapter):
            metadata = session.invoke("update it", kind="proposal", provider="codex:model")

        self.assertEqual((self.repo / "value.txt").read_text(), "old\n")
        value = json.loads(metadata.read_text())
        self.assertEqual(value["id"], "proposal-001")
        self.assertEqual(value["invocation"]["cli_version"], "codex test")
        self.assertTrue(metadata.with_suffix(".patch").exists())

    def test_apply_requires_confirmation_and_rejects_tampering(self):
        session = Session.create(self.directory, self.repo, "main")
        session.set_mode("propose")
        adapter = mock.Mock()
        adapter.cli_version = "codex test"
        adapter.invoke.return_value = InvocationResult(
            {
                "summary": "Update value",
                "patch": "diff --git a/value.txt b/value.txt\n"
                "--- a/value.txt\n+++ b/value.txt\n@@ -1 +1 @@\n-old\n+new\n",
                "risks": [],
                "verification": [],
            }
        )
        with mock.patch("review_converge.session.make_adapter", return_value=adapter):
            metadata = session.invoke("update it", kind="proposal", provider="codex:model")
        session.set_mode("edit")
        with self.assertRaisesRegex(ConvergeError, "explicit --yes"):
            session.apply("proposal-001", confirmed=False)
        metadata.with_suffix(".patch").write_text("tampered\n", encoding="utf-8")
        with self.assertRaisesRegex(ConvergeError, "changed after creation"):
            session.apply("proposal-001", confirmed=True)

    def test_apply_checks_and_applies_recorded_patch(self):
        session = Session.create(self.directory, self.repo, "main")
        session.set_mode("propose")
        adapter = mock.Mock()
        adapter.cli_version = "codex test"
        adapter.invoke.return_value = InvocationResult(
            {
                "summary": "Update value",
                "patch": "diff --git a/value.txt b/value.txt\n"
                "--- a/value.txt\n+++ b/value.txt\n@@ -1 +1 @@\n-old\n+new\n",
                "risks": [],
                "verification": [],
            }
        )
        with mock.patch("review_converge.session.make_adapter", return_value=adapter):
            session.invoke("update it", kind="proposal", provider="codex:model")
        session.set_mode("edit")
        session.apply("proposal-001", confirmed=True)
        self.assertEqual((self.repo / "value.txt").read_text(), "new\n")

    def test_command_parser_supports_status_mode_history_and_quit(self):
        session = Session.create(self.directory, self.repo, "main")
        output = io.StringIO()
        for command in ("status", "mode propose", "history"):
            self.assertTrue(
                execute_command(
                    session,
                    command,
                    provider="codex:model",
                    review_args=(),
                    stdout=output,
                )
            )
        self.assertFalse(
            execute_command(
                session,
                "quit",
                provider="codex:model",
                review_args=(),
                stdout=output,
            )
        )
        self.assertIn("mode: review", output.getvalue())
        self.assertIn('"kind": "command"', output.getvalue())

    def test_checkpoint_and_markdown_export_are_durable(self):
        session = Session.create(self.directory, self.repo, "main")
        output = io.StringIO()
        for command in ("/checkpoint", "export markdown"):
            execute_command(
                session,
                command,
                provider="codex:model",
                review_args=(),
                stdout=output,
            )
        checkpoints = list((self.directory / "checkpoints").glob("*.json"))
        self.assertEqual(len(checkpoints), 1)
        summary = (self.directory / "summary.md").read_text(encoding="utf-8")
        self.assertIn("# review-converge session", summary)
        self.assertIn("checkpoint_created", summary)

    def test_one_shot_session_cli_can_create_and_resume(self):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = session_main(
                [
                    "--new",
                    str(self.directory),
                    "--repo-dir",
                    str(self.repo),
                    "--command",
                    "status",
                ]
            )
        self.assertEqual(code, 0)
        with contextlib.redirect_stdout(output):
            code = session_main(["--resume", str(self.directory), "--command", "status"])
        self.assertEqual(code, 0)


if __name__ == "__main__":
    unittest.main()
