from __future__ import annotations

import json
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from .core import ConvergeError, command_version, load_json, run
from .models import InvocationResult, Reviewer, Usage
from .schema import require_json_schema, validate_json_schema


class ReviewerAdapter(Protocol):
    reviewer: Reviewer
    cli_version: str

    def invoke(self, prompt: str, schema_path: Path) -> InvocationResult: ...


@dataclass(frozen=True)
class AuthenticationStatus:
    provider: str
    authenticated: bool
    detail: str


def authentication_status(
    provider: str, repo_dir: Path, timeout: int
) -> AuthenticationStatus | None:
    """Return a non-billable CLI authentication status when supported."""
    if provider == "claude":
        result = run(
            ["claude", "auth", "status", "--json"],
            cwd=repo_dir,
            timeout=timeout,
            check=False,
        )
        try:
            value = json.loads(result.stdout)
        except json.JSONDecodeError as exc:
            raise ConvergeError(
                "Claude authentication status returned invalid JSON"
            ) from exc
        if not isinstance(value, dict) or not isinstance(value.get("loggedIn"), bool):
            raise ConvergeError("Claude authentication status was incomplete")
        method = value.get("authMethod")
        detail = (
            method if isinstance(method, str) and method != "none" else "not logged in"
        )
        return AuthenticationStatus("claude", value["loggedIn"], detail)
    if provider == "codex":
        result = run(
            ["codex", "login", "status"],
            cwd=repo_dir,
            timeout=timeout,
            check=False,
        )
        text = (result.stdout.strip() or result.stderr.strip()).splitlines()
        detail = text[-1] if text else "status unavailable"
        if result.returncode:
            detail = "not logged in"
        elif detail.lower().startswith("logged in using "):
            detail = detail[len("Logged in using ") :]
        return AuthenticationStatus("codex", result.returncode == 0, detail)
    if provider == "copilot":
        return None
    raise ConvergeError(f"Unsupported authentication provider: {provider}")


class AdapterInvocationError(ConvergeError):
    """Provider failure carrying billable usage observed before failure."""

    def __init__(self, message: str, result: InvocationResult) -> None:
        super().__init__(message)
        self.result = result


def _command_failure(provider: str, returncode: int, stdout: str, stderr: str) -> str:
    detail = "\n".join(value.strip() for value in (stderr, stdout) if value.strip())[
        -4000:
    ]
    return f"{provider} command failed ({returncode})" + (
        f":\n{detail}" if detail else ""
    )


def _integer(value: Any) -> int | None:
    return int(value) if isinstance(value, (int, float)) else None


def _number(value: Any) -> float | None:
    return float(value) if isinstance(value, (int, float)) else None


def _usage_from_mapping(value: Any) -> Usage:
    if not isinstance(value, dict):
        return Usage()
    return Usage(
        input_tokens=_integer(value.get("input_tokens") or value.get("inputTokens")),
        output_tokens=_integer(value.get("output_tokens") or value.get("outputTokens")),
        cached_input_tokens=_integer(
            value.get("cached_input_tokens") or value.get("cache_read_input_tokens")
        ),
        cost_usd=_number(value.get("cost_usd") or value.get("total_cost_usd")),
        ai_credits=_number(value.get("ai_credits") or value.get("aiCredits")),
    )


def _merge_usage(values: list[Usage]) -> Usage:
    def total(field: str) -> int | float | None:
        present = [
            getattr(value, field)
            for value in values
            if getattr(value, field) is not None
        ]
        return sum(present) if present else None

    return Usage(
        input_tokens=total("input_tokens"),  # type: ignore[arg-type]
        output_tokens=total("output_tokens"),  # type: ignore[arg-type]
        cached_input_tokens=total("cached_input_tokens"),  # type: ignore[arg-type]
        cost_usd=total("cost_usd"),  # type: ignore[arg-type]
        ai_credits=total("ai_credits"),  # type: ignore[arg-type]
    )


def _json_lines(stdout: str) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    for line in stdout.splitlines():
        if not line.strip():
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            events.append(value)
    return events


def _find_usage(value: Any) -> list[Usage]:
    found: list[Usage] = []
    if isinstance(value, dict):
        for key, child in value.items():
            if key in ("usage", "token_usage", "tokenUsage", "billing") and isinstance(
                child, dict
            ):
                found.append(_usage_from_mapping(child))
            found.extend(_find_usage(child))
    elif isinstance(value, list):
        for child in value:
            found.extend(_find_usage(child))
    return found


def _strict_json_object(text: str, provider: str) -> dict[str, Any]:
    try:
        value = json.loads(text.strip())
    except json.JSONDecodeError as exc:
        raise ConvergeError(
            f"{provider} returned invalid structured JSON: {exc}"
        ) from exc
    if not isinstance(value, dict):
        raise ConvergeError(f"{provider} structured output must be a JSON object")
    return value


@dataclass
class ClaudeAdapter:
    reviewer: Reviewer
    repo_dir: Path
    timeout: int
    max_budget: float | None = None

    def __post_init__(self) -> None:
        self.cli_version = command_version("claude", self.repo_dir, self.timeout)

    def invoke(self, prompt: str, schema_path: Path) -> InvocationResult:
        schema = schema_path.read_text(encoding="utf-8")
        argv = [
            "claude",
            "-p",
            "--output-format",
            "json",
            "--json-schema",
            schema,
            "--permission-mode",
            "plan",
            "--allowedTools",
            "Read",
            "Bash(git diff:*)",
            "Bash(git show:*)",
            "Bash(git log:*)",
            "Bash(git status:*)",
            "Bash(git merge-base:*)",
            "Bash(git rev-parse:*)",
            "--disallowedTools",
            "Edit",
            "Write",
            "WebFetch",
            "WebSearch",
            "--max-turns",
            "30",
            "--no-session-persistence",
        ]
        if self.reviewer.spec.model:
            argv.extend(["--model", self.reviewer.spec.model])
        if self.max_budget is not None:
            argv.extend(["--max-budget-usd", str(self.max_budget)])
        argv.append(prompt)
        started = time.monotonic()
        command = run(argv, cwd=self.repo_dir, timeout=self.timeout, check=False)
        duration = time.monotonic() - started
        envelope = _strict_json_object(command.stdout, "Claude")
        usage = _usage_from_mapping(envelope.get("usage"))
        usage.cost_usd = _number(envelope.get("total_cost_usd")) or usage.cost_usd
        model_usage = envelope.get("modelUsage") or envelope.get("model_usage")
        resolved = (
            next(iter(model_usage), None) if isinstance(model_usage, dict) else None
        )
        metadata = {
            key: envelope.get(key)
            for key in (
                "session_id",
                "duration_ms",
                "duration_api_ms",
                "num_turns",
                "is_error",
                "stop_reason",
                "terminal_reason",
                "subtype",
            )
            if key in envelope
        }
        if command.returncode:
            raise AdapterInvocationError(
                _command_failure(
                    "Claude", command.returncode, command.stdout, command.stderr
                ),
                InvocationResult({}, usage, resolved, duration, raw_metadata=metadata),
            )
        structured: Any = envelope.get("structured_output", envelope.get("result"))
        if isinstance(structured, str):
            structured = _strict_json_object(structured, "Claude")
        if not isinstance(structured, dict):
            raise ConvergeError("Claude JSON envelope has no structured result")
        return InvocationResult(
            structured, usage, resolved, duration, raw_metadata=metadata
        )


@dataclass
class CodexAdapter:
    reviewer: Reviewer
    repo_dir: Path
    timeout: int
    reasoning_effort: str = "low"

    def __post_init__(self) -> None:
        self.cli_version = command_version("codex", self.repo_dir, self.timeout)

    def invoke(self, prompt: str, schema_path: Path) -> InvocationResult:
        with tempfile.NamedTemporaryFile(
            prefix="review-converge-codex-", suffix=".json", delete=False
        ) as file:
            output_path = Path(file.name)
        try:
            argv = [
                "codex",
                "exec",
                "--sandbox",
                "read-only",
                "--ephemeral",
                "--json",
                "--color",
                "never",
                "--output-schema",
                str(schema_path),
                "--output-last-message",
                str(output_path),
                "--cd",
                str(self.repo_dir),
            ]
            if self.reviewer.spec.model:
                argv.extend(["--model", self.reviewer.spec.model])
            argv.extend(
                ["--config", f'model_reasoning_effort="{self.reasoning_effort}"']
            )
            argv.append(prompt)
            started = time.monotonic()
            result = run(argv, cwd=self.repo_dir, timeout=self.timeout, check=False)
            duration = time.monotonic() - started
            events = _json_lines(result.stdout)
            usages = _find_usage(events)
            metadata = {
                "event_types": [
                    event.get("type") for event in events if event.get("type")
                ],
                "thread_id": next(
                    (
                        event.get("thread_id")
                        for event in events
                        if event.get("thread_id")
                    ),
                    None,
                ),
            }
            resolved = next(
                (
                    event.get("model")
                    for event in events
                    if isinstance(event.get("model"), str)
                ),
                self.reviewer.spec.model,
            )
            invocation = InvocationResult(
                {},
                _merge_usage(usages),
                resolved,
                duration,
                raw_metadata=metadata,
            )
            if result.returncode:
                raise AdapterInvocationError(
                    _command_failure(
                        "Codex", result.returncode, result.stdout, result.stderr
                    ),
                    invocation,
                )
            return InvocationResult(
                load_json(output_path),
                _merge_usage(usages),
                resolved,
                duration,
                raw_metadata=metadata,
            )
        finally:
            output_path.unlink(missing_ok=True)


def _copilot_content(events: list[dict[str, Any]]) -> str:
    candidates: list[str] = []

    def visit(value: Any, key: str = "") -> None:
        if isinstance(value, dict):
            for child_key, child in value.items():
                visit(child, child_key)
        elif isinstance(value, list):
            for child in value:
                visit(child, key)
        elif isinstance(value, str) and key in {
            "content",
            "text",
            "result",
            "response",
            "message",
            "final_output",
        }:
            candidates.append(value)

    visit(events)
    for candidate in reversed(candidates):
        try:
            if isinstance(json.loads(candidate.strip()), dict):
                return candidate
        except json.JSONDecodeError:
            continue
    raise ConvergeError(
        "Copilot JSONL output did not contain a structured JSON response"
    )


@dataclass
class CopilotAdapter:
    reviewer: Reviewer
    repo_dir: Path
    timeout: int
    accessible_dirs: tuple[Path, ...] = ()
    max_ai_credits: float | None = None
    structured_retries: int = 1

    def __post_init__(self) -> None:
        self.cli_version = command_version("copilot", self.repo_dir, self.timeout)
        require_json_schema()

    def _invoke_once(self, prompt: str) -> tuple[str, list[dict[str, Any]], float]:
        argv = [
            "copilot",
            "-p",
            prompt,
            "--output-format",
            "json",
            "--stream",
            "off",
            "--no-ask-user",
            "--no-custom-instructions",
            "--disable-builtin-mcps",
            "--no-remote",
            "--no-remote-export",
            "--no-auto-update",
            "--no-color",
            "--available-tools=view,glob,grep,rg",
            "--allow-all-tools",
            "--deny-tool=write",
            "--deny-tool=edit",
            "--deny-tool=create",
            "--deny-tool=apply_patch",
            "--deny-tool=shell",
            "--model",
            self.reviewer.spec.model or "",
            "-C",
            str(self.repo_dir),
        ]
        for directory in self.accessible_dirs:
            argv.extend(["--add-dir", str(directory)])
        if self.max_ai_credits is not None:
            argv.extend(["--max-ai-credits", str(self.max_ai_credits)])
        started = time.monotonic()
        result = run(argv, cwd=self.repo_dir, timeout=self.timeout, check=False)
        duration = time.monotonic() - started
        events = _json_lines(result.stdout)
        if result.returncode:
            raise AdapterInvocationError(
                _command_failure(
                    "Copilot", result.returncode, result.stdout, result.stderr
                ),
                InvocationResult(
                    {},
                    _merge_usage(_find_usage(events)),
                    self.reviewer.spec.model,
                    duration,
                    raw_metadata={
                        "event_types": [
                            event.get("type") for event in events if event.get("type")
                        ]
                    },
                ),
            )
        return _copilot_content(events), events, duration

    def invoke(self, prompt: str, schema_path: Path) -> InvocationResult:
        schema = schema_path.read_text(encoding="utf-8")
        structured_prompt = f"{prompt}\n\nReturn only one JSON object conforming exactly to this schema:\n{schema}"
        usages: list[Usage] = []
        all_events: list[dict[str, Any]] = []
        total_duration = 0.0
        last_error: ConvergeError | None = None
        previous = ""
        for attempt in range(self.structured_retries + 1):
            current_prompt = structured_prompt
            if attempt:
                current_prompt = (
                    "Repair the previous response. Return only a schema-conforming JSON object.\n"
                    f"Schema:\n{schema}\nPrevious response:\n{previous}"
                )
            # Command, authentication, and provider failures are not repairable
            # structured-output failures and must never trigger a paid retry.
            response, events, duration = self._invoke_once(current_prompt)
            previous = response
            all_events.extend(events)
            total_duration += duration
            usages.append(_merge_usage(_find_usage(events)))
            try:
                value = _strict_json_object(response, "Copilot")
                validate_json_schema(value, schema_path)
                return InvocationResult(
                    value,
                    _merge_usage(usages),
                    self.reviewer.spec.model,
                    total_duration,
                    attempt + 1,
                    {
                        "event_types": [
                            event.get("type")
                            for event in all_events
                            if event.get("type")
                        ]
                    },
                )
            except ConvergeError as exc:
                last_error = exc
        assert last_error is not None
        raise AdapterInvocationError(
            str(last_error),
            InvocationResult(
                {},
                _merge_usage(usages),
                self.reviewer.spec.model,
                total_duration,
                self.structured_retries + 1,
                {
                    "event_types": [
                        event.get("type") for event in all_events if event.get("type")
                    ]
                },
            ),
        )


def make_adapter(
    reviewer: Reviewer,
    repo_dir: Path,
    timeout: int,
    *,
    accessible_dirs: tuple[Path, ...] = (),
    claude_max_budget_usd: float | None = None,
    copilot_max_ai_credits: float | None = None,
    codex_reasoning_effort: str = "low",
    structured_retries: int = 1,
) -> ReviewerAdapter:
    if reviewer.spec.provider == "claude":
        return ClaudeAdapter(reviewer, repo_dir, timeout, claude_max_budget_usd)
    if reviewer.spec.provider == "codex":
        return CodexAdapter(reviewer, repo_dir, timeout, codex_reasoning_effort)
    return CopilotAdapter(
        reviewer,
        repo_dir,
        timeout,
        accessible_dirs,
        copilot_max_ai_credits,
        structured_retries,
    )
