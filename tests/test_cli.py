import argparse
import contextlib
import io
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from review_converge.cli import (
    SCHEMAS,
    ConvergeError,
    artifact_path,
    build_parser,
    collect_github_snapshot,
    collect_local_snapshot,
    collect_review_threads,
    converged,
    extract_claude_result,
    format_usage,
    graphql,
    main,
    parse_repo,
    render_prompt,
    response_decisions,
    run_all,
    validate_args,
    validate_review,
)
from review_converge.config import Settings
from review_converge.models import InvocationResult, Usage
from review_converge.schema import generate_schemas


class ExtractClaudeResultTest(unittest.TestCase):
    def test_structured_output_object(self):
        value = {"verdict": "approve"}
        self.assertEqual(
            extract_claude_result(json.dumps({"structured_output": value})), value
        )

    def test_result_json_string(self):
        value = {"verdict": "comment"}
        self.assertEqual(
            extract_claude_result(json.dumps({"result": json.dumps(value)})), value
        )

    def test_rejects_non_json_result(self):
        with self.assertRaises(ConvergeError):
            extract_claude_result(json.dumps({"result": "not json"}))


def reconciliation(**changes):
    value = {
        "converged": True,
        "revised_verdict": "approve",
        "responses": [
            {
                "finding_id": "claude:F1",
                "decision": "reject",
                "corrected_severity": None,
            }
        ],
        "material_disagreements": [],
        "new_findings": [],
    }
    value.update(changes)
    return value


class ConvergenceTest(unittest.TestCase):
    def test_requires_matching_verdict_and_decisions(self):
        clean = reconciliation()
        expected = {"claude:F1"}
        self.assertTrue(converged({"r1": clean, "r2": clean}, expected))
        self.assertFalse(
            converged(
                {"r1": clean, "r2": reconciliation(revised_verdict="comment")}, expected
            )
        )
        self.assertFalse(
            converged({"r1": clean, "r2": reconciliation(responses=[])}, expected)
        )
        self.assertFalse(
            converged(
                {"r1": clean, "r2": reconciliation(new_findings=[{"id": "r2:F2"}])},
                expected,
            )
        )

    def test_matching_omissions_do_not_converge(self):
        omitted = reconciliation(responses=[])
        self.assertFalse(converged({"r1": omitted, "r2": omitted}, {"claude:F1"}))

    def test_rejects_duplicate_finding_ids(self):
        duplicate = reconciliation(responses=reconciliation()["responses"] * 2)
        self.assertFalse(converged({"r1": duplicate, "r2": duplicate}, {"claude:F1"}))

    def test_rejects_incoherent_corrected_severity(self):
        accepted = reconciliation()
        accepted["responses"][0]["corrected_severity"] = "low"
        self.assertIsNone(response_decisions(accepted))
        downgraded = reconciliation()
        downgraded["responses"][0]["decision"] = "downgrade"
        self.assertIsNone(response_decisions(downgraded))

    def test_review_identity_and_finding_namespace_are_validated(self):
        review = {"reviewer": "r1", "findings": [{"id": "r1:F1"}]}
        self.assertEqual(validate_review(review, "r1"), {"r1:F1"})
        with self.assertRaisesRegex(ConvergeError, "review identity"):
            validate_review(review, "r2")
        with self.assertRaisesRegex(ConvergeError, "must start"):
            validate_review({"reviewer": "r1", "findings": [{"id": "F1"}]}, "r1")


class HelpersTest(unittest.TestCase):
    def test_progress_usage_formats_known_and_unknown_fields(self):
        invocation = InvocationResult(
            {},
            Usage(input_tokens=10, output_tokens=5, cost_usd=0.25),
            "model",
            1.5,
        )
        self.assertEqual(
            format_usage(invocation),
            "tokens: input 10, cached unknown, output 5 — reported cost $0.250000",
        )

    def test_run_all_prints_stage_completion_and_tokens(self):
        invocation = InvocationResult({}, Usage(output_tokens=5), "model", 1.5)
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            completed, failed = run_all(
                {"r1": lambda: invocation},
                stage="Initial reviews",
                labels={"r1": "claude:opus"},
                heartbeat_seconds=1,
            )
        self.assertEqual(completed, {"r1": invocation})
        self.assertFalse(failed)
        self.assertIn("Initial reviews started: r1=claude:opus", output.getvalue())
        self.assertIn("output 5", output.getvalue())

    def test_help_documents_defaults_guidance_safety_and_exit_codes(self):
        help_text = build_parser().format_help()
        for expected in (
            "claude:opus and codex:gpt-5.6-sol",
            "--instruction-file",
            "cannot override these constraints",
            "review completed but failed --fail-on",
        ):
            with self.subTest(expected=expected):
                self.assertIn(expected, help_text)

    def test_artifact_paths_have_no_round_zero_special_case(self):
        root = Path("/tmp/output")
        self.assertEqual(artifact_path(root, 0, "r1"), root / "round-0-r1.json")
        self.assertEqual(artifact_path(root, 3, "r2"), root / "round-3-r2.json")

    def test_repo_requires_owner_and_name(self):
        self.assertEqual(parse_repo("owner/name"), ("owner", "name"))
        for invalid in ("name", "/name", "owner/", "a/b/c"):
            with self.subTest(invalid=invalid), self.assertRaises(ConvergeError):
                parse_repo(invalid)

    def test_render_prompt_rejects_missing_values(self):
        with self.assertRaises(ConvergeError):
            render_prompt("review.md", {})

    def test_render_prompt_preserves_placeholder_like_operator_text(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "custom.md").write_text("Guidance: {{value}}\n", encoding="utf-8")
            with mock.patch("review_converge.cli.PROMPTS", root):
                rendered = render_prompt(
                    "custom.md", {"value": "Keep {{literal}} text"}
                )
        self.assertEqual(rendered, "Guidance: Keep {{literal}} text\n")

    def test_local_argument_validation(self):
        base = {
            "rounds": 3,
            "local": True,
            "base": None,
            "head": "HEAD",
            "include_dirty": False,
            "repo": None,
            "no_fetch": False,
            "resume": None,
        }
        with self.assertRaisesRegex(ConvergeError, "requires --base"):
            validate_args(argparse.Namespace(**base), Settings())
        validate_args(argparse.Namespace(**{**base, "base": "main"}), Settings())

    def test_parallel_invocations_retain_successes_when_peer_fails(self):
        success = object()

        def fail():
            raise ConvergeError("provider failed")

        completed, failed = run_all({"r1": lambda: success, "r2": fail})
        self.assertIs(completed["r1"], success)
        self.assertIsInstance(failed["r2"], ConvergeError)


class LocalSnapshotTest(unittest.TestCase):
    def git(self, repo: Path, *args: str) -> str:
        return subprocess.run(
            ["git", *args],
            cwd=repo,
            check=True,
            text=True,
            capture_output=True,
        ).stdout.strip()

    def make_repo(self, root: Path) -> Path:
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

    def test_collects_pinned_snapshot_without_github(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            repo = self.make_repo(root)
            with mock.patch(
                "review_converge.cli.gh_json",
                side_effect=AssertionError("GitHub called"),
            ):
                snapshot = collect_local_snapshot(
                    repo, "base", "HEAD", root / "out", 10, include_dirty=False
                )
            self.assertEqual(snapshot.source, "local")
            self.assertNotEqual(snapshot.merge_base_sha, snapshot.head_sha)
            self.assertIn(
                "-base", (root / "out" / "diff.patch").read_text(encoding="utf-8")
            )
            self.assertEqual(
                json.loads((root / "out" / "threads.json").read_text()),
                {"reviewThreads": [], "reviews": []},
            )

    def test_local_dry_run_exercises_cli_without_github(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            repo = self.make_repo(root)
            with mock.patch(
                "review_converge.cli.gh_json",
                side_effect=AssertionError("GitHub called"),
            ):
                result = main(
                    [
                        "--local",
                        "--base",
                        "base",
                        "--repo-dir",
                        str(repo),
                        "--output-dir",
                        str(root / "out"),
                        "--instruction",
                        "Focus on compatibility",
                        "--dry-run",
                    ]
                )
            self.assertEqual(result, 0)
            self.assertTrue((root / "out" / "snapshot.json").is_file())
            manifest = json.loads((root / "out" / "run.json").read_text())
            self.assertEqual(
                manifest["configuration"]["instructions"],
                ["Focus on compatibility"],
            )

    def test_dirty_changes_require_opt_in(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            repo = self.make_repo(root)
            (repo / "file.txt").write_text("dirty\n", encoding="utf-8")
            with self.assertRaisesRegex(ConvergeError, "worktree is dirty"):
                collect_local_snapshot(repo, "base", "HEAD", root / "out", 10, False)
            snapshot = collect_local_snapshot(
                repo, "base", "HEAD", root / "dirty-out", 10, True
            )
            self.assertIsNotNone(snapshot.worktree_fingerprint)
            self.assertIn(
                "Uncommitted tracked changes",
                (root / "dirty-out" / "diff.patch").read_text(),
            )

    def test_untracked_files_are_not_silently_omitted(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            repo = self.make_repo(root)
            (repo / "new.txt").write_text("new\n", encoding="utf-8")
            with self.assertRaisesRegex(ConvergeError, "untracked files"):
                collect_local_snapshot(repo, "base", "HEAD", root / "out", 10, True)

    def test_dirty_changes_require_selected_head_to_be_checked_out(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            repo = self.make_repo(root)
            self.git(repo, "branch", "feature")
            self.git(repo, "checkout", "-q", "base")
            with self.assertRaisesRegex(ConvergeError, "checked-out HEAD"):
                collect_local_snapshot(repo, "base", "feature", root / "out", 10, True)


class PaginationTest(unittest.TestCase):
    def test_graphql_errors_become_converge_errors(self):
        with (
            mock.patch(
                "review_converge.cli.gh_json",
                return_value={
                    "data": None,
                    "errors": [{"message": "Something failed"}],
                },
            ),
            self.assertRaisesRegex(ConvergeError, "Something failed"),
        ):
            graphql(Path("."), "query { viewer { login } }", {}, 10)

    def test_graphql_requires_data(self):
        with (
            mock.patch("review_converge.cli.gh_json", return_value={}),
            self.assertRaisesRegex(ConvergeError, "did not contain data"),
        ):
            graphql(Path("."), "query { viewer { login } }", {}, 10)

    def test_paginates_threads_nested_comments_and_reviews(self):
        def fake_graphql(_repo_dir, query, variables, _timeout):
            after = variables.get("after")
            if "reviewThreads" in query:
                node = {
                    "id": "T1" if after is None else "T2",
                    "isResolved": False,
                    "isOutdated": False,
                    "path": "a.py",
                    "line": 1,
                    "originalLine": 1,
                    "comments": {
                        "nodes": [{"body": "first"}],
                        "pageInfo": {"hasNextPage": after is None, "endCursor": "C1"},
                    },
                }
                return {
                    "data": {
                        "repository": {
                            "pullRequest": {
                                "reviewThreads": {
                                    "nodes": [node],
                                    "pageInfo": {
                                        "hasNextPage": after is None,
                                        "endCursor": "T-CURSOR",
                                    },
                                }
                            }
                        }
                    }
                }
            if "node(id:$id)" in query:
                return {
                    "data": {
                        "node": {
                            "comments": {
                                "nodes": [{"body": "second"}],
                                "pageInfo": {"hasNextPage": False, "endCursor": None},
                            }
                        }
                    }
                }
            return {
                "data": {
                    "repository": {
                        "pullRequest": {
                            "reviews": {
                                "nodes": [
                                    {
                                        "state": "COMMENTED"
                                        if after is None
                                        else "APPROVED"
                                    }
                                ],
                                "pageInfo": {
                                    "hasNextPage": after is None,
                                    "endCursor": "R-CURSOR",
                                },
                            }
                        }
                    }
                }
            }

        with mock.patch("review_converge.cli.graphql", side_effect=fake_graphql):
            result = collect_review_threads(Path("."), "owner/repo", 1, 10)
        self.assertEqual(len(result["reviewThreads"]), 2)
        self.assertEqual(len(result["reviewThreads"][0]["comments"]["nodes"]), 2)
        self.assertEqual(len(result["reviews"]), 2)


class GitHubSnapshotTest(unittest.TestCase):
    def test_keeps_base_tip_and_merge_base_separate_without_fetch(self):
        metadata = {
            "number": 7,
            "headRefOid": "head",
            "baseRefOid": "new-base-tip",
            "baseRefName": "main",
            "headRefName": "feature",
        }

        def fake_gh(_repo_dir, args, _timeout):
            if args[:2] == ["pr", "view"]:
                return metadata
            if args[:2] == ["api", "repos/owner/repo/compare/new-base-tip...head"]:
                return {"merge_base_commit": {"sha": "old-merge-base"}}
            if "--slurp" in args:
                return [[]]
            raise AssertionError(args)

        completed = subprocess.CompletedProcess([], 0, stdout="diff\n", stderr="")
        with (
            tempfile.TemporaryDirectory() as temp,
            mock.patch("review_converge.cli.gh_json", side_effect=fake_gh),
            mock.patch(
                "review_converge.cli.collect_review_threads",
                return_value={"reviewThreads": [], "reviews": []},
            ),
            mock.patch("review_converge.cli.run", return_value=completed),
        ):
            snapshot = collect_github_snapshot(
                Path("."), "owner/repo", 7, Path(temp) / "out", 10, fetch_refs=False
            )
        self.assertEqual(snapshot.base_tip_sha, "new-base-tip")
        self.assertEqual(snapshot.merge_base_sha, "old-merge-base")
        self.assertEqual(snapshot.pr, 7)


class SchemaTest(unittest.TestCase):
    def test_all_schemas_are_closed_json_objects(self):
        for name in ("review.json", "reconciliation.json", "final.json"):
            value = json.loads((SCHEMAS / name).read_text(encoding="utf-8"))
            self.assertEqual(value["type"], "object")
            self.assertFalse(value["additionalProperties"])

    def test_generated_schemas_omit_nonportable_meta_schema(self):
        with tempfile.TemporaryDirectory() as temp:
            generated = generate_schemas(SCHEMAS, Path(temp), ["r1", "r2"])
            for path in generated.values():
                self.assertNotIn(
                    "$schema", json.loads(path.read_text(encoding="utf-8"))
                )


if __name__ == "__main__":
    unittest.main()
