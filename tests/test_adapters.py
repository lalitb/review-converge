import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from review_converge.adapters import (
    AdapterInvocationError,
    AuthenticationStatus,
    ClaudeAdapter,
    CodexAdapter,
    CopilotAdapter,
    _copilot_content,
    authentication_status,
)
from review_converge.core import ConvergeError
from review_converge.models import Reviewer, ReviewerSpec


class CopilotAdapterTest(unittest.TestCase):
    def reviewer(self):
        return Reviewer("r1", ReviewerSpec.parse("copilot:gemini-test"))

    def test_extracts_structured_object_from_jsonl_events(self):
        response = '{"reviewer":"r1","findings":[]}'
        events = [{"type": "assistant.message", "data": {"content": response}}]
        self.assertEqual(_copilot_content(events), response)

    def test_extracts_single_json_fence_from_copilot_response(self):
        response = '{"reviewer":"r1","findings":[]}'
        events = [
            {
                "type": "assistant.message",
                "data": {"content": f"```json\n{response}\n```"},
            }
        ]
        self.assertEqual(_copilot_content(events), response)

    def test_rejects_fenced_json_with_surrounding_prose(self):
        events = [
            {
                "type": "assistant.message",
                "data": {"content": 'Result:\n```json\n{"ok":true}\n```'},
            }
        ]
        with self.assertRaisesRegex(ConvergeError, "structured JSON response"):
            _copilot_content(events)

    def test_permission_argv_is_read_only_and_model_is_explicit(self):
        event = (
            json.dumps({"type": "assistant.message", "data": {"content": "{}"}}) + "\n"
        )
        completed = subprocess.CompletedProcess([], 0, stdout=event, stderr="")
        with (
            tempfile.TemporaryDirectory() as temp,
            mock.patch(
                "review_converge.adapters.command_version", return_value="copilot 1"
            ),
            mock.patch("review_converge.adapters.require_json_schema"),
            mock.patch("review_converge.adapters.validate_json_schema"),
            mock.patch(
                "review_converge.adapters.run", return_value=completed
            ) as runner,
        ):
            adapter = CopilotAdapter(self.reviewer(), Path(temp), 10)
            adapter._invoke_once("prompt")
        argv = runner.call_args.args[0]
        self.assertIn("--model", argv)
        self.assertIn("gemini-test", argv)
        self.assertIn("--available-tools=view,glob,grep,rg", argv)
        self.assertIn("--allow-all-tools", argv)
        self.assertIn("--deny-tool=shell", argv)
        self.assertIn("--disable-builtin-mcps", argv)
        available = next(
            value for value in argv if value.startswith("--available-tools=")
        )
        self.assertNotIn("write", available)
        self.assertNotIn("edit", available)
        self.assertFalse(any("git push" in value for value in argv))

    def test_operational_failure_is_not_retried_as_json_repair(self):
        with (
            tempfile.TemporaryDirectory() as temp,
            mock.patch(
                "review_converge.adapters.command_version", return_value="copilot 1"
            ),
            mock.patch("review_converge.adapters.require_json_schema"),
        ):
            schema = Path(temp) / "schema.json"
            schema.write_text('{"type":"object"}', encoding="utf-8")
            adapter = CopilotAdapter(self.reviewer(), Path(temp), 10)
            with (
                mock.patch.object(
                    adapter,
                    "_invoke_once",
                    side_effect=ConvergeError("authentication failed"),
                ) as invoke,
                self.assertRaisesRegex(ConvergeError, "authentication failed"),
            ):
                adapter.invoke("prompt", schema)
            self.assertEqual(invoke.call_count, 1)


class AuthenticationStatusTest(unittest.TestCase):
    def test_claude_status_parses_method_without_exposing_credentials(self):
        completed = subprocess.CompletedProcess(
            [],
            0,
            stdout=json.dumps({"loggedIn": True, "authMethod": "oauth"}),
            stderr="",
        )
        with mock.patch(
            "review_converge.adapters.run", return_value=completed
        ) as runner:
            status = authentication_status("claude", Path("."), 10)
        self.assertEqual(status, AuthenticationStatus("claude", True, "oauth"))
        self.assertEqual(
            runner.call_args.args[0], ["claude", "auth", "status", "--json"]
        )

    def test_codex_status_parses_login_type(self):
        completed = subprocess.CompletedProcess(
            [], 0, stdout="Logged in using ChatGPT\n", stderr=""
        )
        with mock.patch("review_converge.adapters.run", return_value=completed):
            status = authentication_status("codex", Path("."), 10)
        self.assertEqual(status, AuthenticationStatus("codex", True, "ChatGPT"))

    def test_copilot_has_no_noninteractive_status(self):
        self.assertIsNone(authentication_status("copilot", Path("."), 10))


class FailedUsageTest(unittest.TestCase):
    def test_claude_failure_retains_reported_usage(self):
        envelope = {
            "is_error": True,
            "total_cost_usd": 0.25,
            "usage": {"input_tokens": 10, "output_tokens": 20},
            "modelUsage": {"claude-test": {}},
            "terminal_reason": "structured_output_retry_exhausted",
        }
        completed = subprocess.CompletedProcess(
            [], 1, stdout=json.dumps(envelope), stderr=""
        )
        with (
            tempfile.TemporaryDirectory() as temp,
            mock.patch(
                "review_converge.adapters.command_version", return_value="claude 1"
            ),
            mock.patch("review_converge.adapters.run", return_value=completed),
        ):
            schema = Path(temp) / "schema.json"
            schema.write_text('{"type":"object"}', encoding="utf-8")
            adapter = ClaudeAdapter(
                Reviewer("r1", ReviewerSpec.parse("claude")), Path(temp), 10
            )
            with self.assertRaises(AdapterInvocationError) as raised:
                adapter.invoke("prompt", schema)
        self.assertEqual(raised.exception.result.usage.cost_usd, 0.25)
        self.assertEqual(raised.exception.result.usage.output_tokens, 20)


class CodexAdapterTest(unittest.TestCase):
    def test_model_and_reasoning_effort_are_explicit(self):
        event = '{"type":"turn.completed"}\n'

        def fake_run(argv, **_kwargs):
            output = Path(argv[argv.index("--output-last-message") + 1])
            output.write_text('{"reviewer":"r2"}', encoding="utf-8")
            return subprocess.CompletedProcess(argv, 0, stdout=event, stderr="")

        with (
            tempfile.TemporaryDirectory() as temp,
            mock.patch(
                "review_converge.adapters.command_version", return_value="codex 1"
            ),
            mock.patch("review_converge.adapters.run", side_effect=fake_run) as runner,
        ):
            schema = Path(temp) / "schema.json"
            schema.write_text('{"type":"object"}', encoding="utf-8")
            adapter = CodexAdapter(
                Reviewer("r2", ReviewerSpec.parse("codex:gpt-5.6-sol")),
                Path(temp),
                10,
                "low",
            )
            adapter.invoke("prompt", schema)
        argv = runner.call_args.args[0]
        self.assertIn("gpt-5.6-sol", argv)
        self.assertIn('model_reasoning_effort="low"', argv)
