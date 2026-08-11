import json
import tempfile
import unittest
from pathlib import Path

from review_converge.artifacts import capture_context, record_invocation, verify_context
from review_converge.core import ConvergeError
from review_converge.models import InvocationResult, Reviewer, ReviewerSpec, Usage


class ContextCaptureTest(unittest.TestCase):
    def test_captures_and_detects_source_mutation(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            repo, output = root / "repo", root / "output"
            repo.mkdir()
            (repo / "CONTRIBUTING.md").write_text("rules\n", encoding="utf-8")
            captured = capture_context(repo, output, ("CONTRIBUTING.md",))
            verify_context(repo, output, captured)
            (repo / "CONTRIBUTING.md").write_text("changed\n", encoding="utf-8")
            with self.assertRaisesRegex(ConvergeError, "Context source changed"):
                verify_context(repo, output, captured)

    def test_context_must_stay_inside_checkout(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            repo, output = root / "repo", root / "output"
            repo.mkdir()
            (root / "secret").write_text("no\n", encoding="utf-8")
            with self.assertRaisesRegex(ConvergeError, "inside"):
                capture_context(repo, output, ("../secret",))


class UsageLedgerTest(unittest.TestCase):
    def test_unknown_usage_remains_null_and_known_usage_totals(self):
        with tempfile.TemporaryDirectory() as temp:
            output = Path(temp)
            (output / "usage.json").write_text(
                '{"format_version":1,"invocations":[],"totals":{}}\n', encoding="utf-8"
            )
            reviewer = Reviewer("r1", ReviewerSpec("claude"))
            record_invocation(
                output,
                stage="initial",
                reviewer=reviewer,
                cli_version="1",
                result=InvocationResult(
                    {}, Usage(input_tokens=10, output_tokens=4), duration_seconds=1.5
                ),
            )
            value = json.loads((output / "usage.json").read_text())
            self.assertEqual(value["totals"]["input_tokens"], 10)
            self.assertIsNone(value["totals"]["cost_usd"])
