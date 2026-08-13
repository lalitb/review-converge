import tempfile
import unittest
from pathlib import Path

from review_converge.config import (
    Settings,
    apply_review_profile,
    load_settings,
    override_settings,
)
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

    def test_review_profiles_bound_models_effort_rounds_and_invocations(self):
        expected = {
            "cheap": ("claude:sonnet", "low", 0, 3),
            "balanced": ("claude:sonnet", "low", 1, 5),
            "thorough": ("claude:opus", "medium", 3, 9),
        }
        for profile, (claude, effort, rounds, calls) in expected.items():
            with self.subTest(profile=profile):
                settings = apply_review_profile(Settings(), profile)
                self.assertEqual(settings.reviewers[0].display_name, claude)
                self.assertEqual(settings.codex_reasoning_effort, effort)
                self.assertEqual(settings.rounds, rounds)
                self.assertEqual(3 + 2 * settings.rounds, calls)

    def test_toml_profile_can_be_overridden_by_explicit_toml_values(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "review.toml"
            path.write_text('review_profile = "cheap"\nrounds = 2\n', encoding="utf-8")
            settings = load_settings(path)
        self.assertEqual(settings.review_profile, "cheap")
        self.assertEqual(settings.reviewers[0].display_name, "claude:sonnet")
        self.assertEqual(settings.rounds, 2)

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
