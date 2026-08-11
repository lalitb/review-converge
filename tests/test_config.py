import tempfile
import unittest
from pathlib import Path

from review_converge.config import Settings, load_settings, override_settings
from review_converge.core import ConvergeError
from review_converge.models import ReviewerSpec


class ReviewerSpecTest(unittest.TestCase):
    def test_requires_explicit_copilot_model(self):
        for value in ("copilot", "copilot:auto", "copilot:"):
            with self.subTest(value=value), self.assertRaises(ConvergeError):
                ReviewerSpec.parse(value)
        self.assertEqual(
            ReviewerSpec.parse("copilot:gemini-3.1-pro-preview").model,
            "gemini-3.1-pro-preview",
        )

    def test_rejects_unknown_provider(self):
        with self.assertRaisesRegex(ConvergeError, "Unknown reviewer provider"):
            ReviewerSpec.parse("mystery:model")


class ConfigurationTest(unittest.TestCase):
    def test_defaults_pin_reviewer_and_decider_models(self):
        settings = Settings()
        self.assertEqual(
            [reviewer.display_name for reviewer in settings.reviewers],
            ["claude:opus", "codex:gpt-5.6-sol"],
        )
        self.assertEqual(settings.final_decider.display_name, "codex:gpt-5.6-sol")
        self.assertEqual(settings.codex_reasoning_effort, "low")

    def test_explicit_toml_and_cli_override_precedence(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "review.toml"
            path.write_text(
                'reviewers = ["claude:sonnet", "codex"]\n'
                'final_decider = "claude:opus"\nrounds = 2\n'
                'context_files = ["CONTRIBUTING.md"]\n'
                'instructions = ["Focus on compatibility"]\n',
                encoding="utf-8",
            )
            configured = load_settings(path)
        resolved = override_settings(configured, rounds=1, fail_on="high")
        self.assertEqual(resolved.rounds, 1)
        self.assertEqual(resolved.fail_on, "high")
        self.assertEqual(resolved.reviewers[0].model, "sonnet")
        self.assertEqual(resolved.context_files, ("CONTRIBUTING.md",))
        self.assertEqual(resolved.instructions, ("Focus on compatibility",))

    def test_exactly_two_distinct_reviewers(self):
        with self.assertRaisesRegex(ConvergeError, "Exactly two"):
            override_settings(Settings(), reviewers=["claude"])
        with self.assertRaisesRegex(ConvergeError, "must be distinct"):
            override_settings(Settings(), reviewers=["codex", "codex"])

    def test_unknown_keys_fail_closed(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "review.toml"
            path.write_text("surprise = true\n", encoding="utf-8")
            with self.assertRaisesRegex(ConvergeError, "Unknown configuration keys"):
                load_settings(path)

    def test_copilot_credit_cap_respects_cli_minimum(self):
        with self.assertRaisesRegex(ConvergeError, "at least 30"):
            override_settings(Settings(), copilot_max_ai_credits=29)
        self.assertEqual(
            override_settings(
                Settings(), copilot_max_ai_credits=30
            ).copilot_max_ai_credits,
            30,
        )

    def test_codex_reasoning_effort_is_validated(self):
        with self.assertRaisesRegex(ConvergeError, "codex_reasoning_effort"):
            override_settings(Settings(), codex_reasoning_effort="maximum")
        self.assertEqual(
            override_settings(
                Settings(), codex_reasoning_effort="high"
            ).codex_reasoning_effort,
            "high",
        )
