import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from review_converge.cli import main
from review_converge.models import InvocationResult, Usage


class FakeAdapter:
    def __init__(self, reviewer, calls):
        self.reviewer = reviewer
        self.calls = calls
        self.cli_version = f"{reviewer.spec.provider} test"

    def invoke(self, _prompt, schema_path):
        self.calls.append((self.reviewer.slot, schema_path.stem))
        if schema_path.stem == "review":
            value = {
                "reviewer": self.reviewer.slot,
                "verdict": "approve",
                "summary": "clean",
                "findings": [],
                "scope_notes": [],
                "rejected_candidates": [],
                "confidence": "high",
            }
        elif schema_path.stem == "reconciliation":
            value = {
                "reviewer": self.reviewer.slot,
                "responses": [],
                "new_findings": [],
                "material_disagreements": [],
                "revised_verdict": "approve",
                "converged": True,
                "summary": "agreed",
            }
        else:
            value = {
                "head_sha": "head",
                "verdict": "approve",
                "converged": True,
                "remaining_disagreements": [],
                "findings": [],
                "final_markdown": "LGTM",
            }
        return InvocationResult(
            value, Usage(input_tokens=1, output_tokens=1), duration_seconds=0.01
        )


class WorkflowTest(unittest.TestCase):
    def git(self, repo, *args):
        return subprocess.run(
            ["git", *args],
            cwd=repo,
            check=True,
            text=True,
            capture_output=True,
        ).stdout.strip()

    def make_repo(self, root):
        repo = root / "repo"
        repo.mkdir()
        self.git(repo, "init", "-q")
        self.git(repo, "config", "user.name", "Test")
        self.git(repo, "config", "user.email", "test@example.com")
        (repo / "file.txt").write_text("base\n", encoding="utf-8")
        self.git(repo, "add", "file.txt")
        self.git(repo, "commit", "-qm", "base")
        self.git(repo, "branch", "base")
        (repo / "file.txt").write_text("head\n", encoding="utf-8")
        self.git(repo, "commit", "-qam", "head")
        return repo

    def test_full_run_and_completed_resume_do_not_repeat_invocations(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            repo, output, calls = self.make_repo(root), root / "out", []

            def factory(reviewer, *_args, **_kwargs):
                return FakeAdapter(reviewer, calls)

            with mock.patch("review_converge.cli.make_adapter", side_effect=factory):
                result = main(
                    [
                        "--local",
                        "--base",
                        "base",
                        "--repo-dir",
                        str(repo),
                        "--output-dir",
                        str(output),
                        "--rounds",
                        "1",
                    ]
                )
            self.assertEqual(result, 0)
            self.assertEqual(len(calls), 5)
            usage = json.loads((output / "usage.json").read_text())
            self.assertEqual(usage["totals"]["invocation_count"], 5)

            with mock.patch(
                "review_converge.cli.make_adapter",
                side_effect=AssertionError("adapter created"),
            ):
                resumed = main(["--resume", str(output), "--repo-dir", str(repo)])
            self.assertEqual(resumed, 0)
            self.assertEqual(len(calls), 5)

    def test_partial_pair_resume_only_invokes_missing_work(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            repo, output, calls = self.make_repo(root), root / "out", []

            def factory(reviewer, *_args, **_kwargs):
                return FakeAdapter(reviewer, calls)

            with mock.patch("review_converge.cli.make_adapter", side_effect=factory):
                self.assertEqual(
                    main(
                        [
                            "--local",
                            "--base",
                            "base",
                            "--repo-dir",
                            str(repo),
                            "--output-dir",
                            str(output),
                            "--rounds",
                            "1",
                        ]
                    ),
                    0,
                )
            (output / "round-1-r2.json").unlink()
            (output / "final.json").unlink()
            manifest = json.loads((output / "run.json").read_text())
            del manifest["stages"]["rounds"]["1"]["r2"]
            manifest["stages"]["final"] = "pending"
            (output / "run.json").write_text(json.dumps(manifest), encoding="utf-8")
            before = len(calls)
            with mock.patch("review_converge.cli.make_adapter", side_effect=factory):
                self.assertEqual(
                    main(["--resume", str(output), "--repo-dir", str(repo)]), 0
                )
            self.assertEqual(
                len(calls) - before, 2
            )  # missing r2 reconciliation + final

    def test_resume_rejects_changed_context_and_artifact(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            repo, output, calls = self.make_repo(root), root / "out", []
            (repo / "rules.md").write_text("rules\n", encoding="utf-8")
            self.git(repo, "add", "rules.md")
            self.git(repo, "commit", "-qm", "rules")

            def factory(reviewer, *_args, **_kwargs):
                return FakeAdapter(reviewer, calls)

            with mock.patch("review_converge.cli.make_adapter", side_effect=factory):
                self.assertEqual(
                    main(
                        [
                            "--local",
                            "--base",
                            "base",
                            "--repo-dir",
                            str(repo),
                            "--output-dir",
                            str(output),
                            "--rounds",
                            "0",
                            "--context-file",
                            "rules.md",
                        ]
                    ),
                    0,
                )
            (repo / "rules.md").write_text("changed\n", encoding="utf-8")
            self.assertEqual(
                main(["--resume", str(output), "--repo-dir", str(repo)]), 1
            )

    def test_verdict_gate_uses_exit_two(self):
        from review_converge.cli import gate_failed
        from review_converge.models import ExecutionResult

        result = ExecutionResult(Path("/tmp/out"), "comment", ({"severity": "high"},))
        self.assertTrue(gate_failed(result, "high"))
        self.assertTrue(gate_failed(result, "comment"))
        self.assertFalse(gate_failed(result, "request_changes"))
