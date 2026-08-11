from __future__ import annotations

import sys
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

from .core import ConvergeError
from .models import ReviewerSpec

if sys.version_info >= (3, 11):
    import tomllib
else:  # pragma: no cover - exercised by the Python 3.10 CI job
    import tomli as tomllib


@dataclass(frozen=True)
class Settings:
    reviewers: tuple[ReviewerSpec, ReviewerSpec] = (
        ReviewerSpec("claude", "opus"),
        ReviewerSpec("codex", "gpt-5.6-sol"),
    )
    final_decider: ReviewerSpec = field(
        default_factory=lambda: ReviewerSpec("codex", "gpt-5.6-sol")
    )
    rounds: int = 3
    timeout: int = 1800
    context_files: tuple[str, ...] = ()
    fail_on: str | None = None
    claude_max_budget_usd: float | None = None
    copilot_max_ai_credits: float | None = None
    codex_reasoning_effort: str = "low"
    structured_retries: int = 1


ALLOWED_KEYS = frozenset(
    {
        "reviewers",
        "final_decider",
        "rounds",
        "timeout",
        "context_files",
        "fail_on",
        "claude_max_budget_usd",
        "copilot_max_ai_credits",
        "codex_reasoning_effort",
        "structured_retries",
    }
)


def _validate(settings: Settings) -> Settings:
    if len(settings.reviewers) != 2:
        raise ConvergeError("Exactly two reviewers are required")
    if settings.reviewers[0] == settings.reviewers[1]:
        raise ConvergeError("The two reviewer specifications must be distinct")
    if not 0 <= settings.rounds <= 10:
        raise ConvergeError("rounds must be between 0 and 10")
    if settings.timeout <= 0:
        raise ConvergeError("timeout must be positive")
    if settings.structured_retries not in (0, 1):
        raise ConvergeError("structured_retries must be 0 or 1")
    if settings.codex_reasoning_effort not in (
        "minimal",
        "low",
        "medium",
        "high",
        "xhigh",
    ):
        raise ConvergeError(
            "codex_reasoning_effort must be minimal, low, medium, high, or xhigh"
        )
    if settings.fail_on not in (
        None,
        "request_changes",
        "comment",
        "blocker",
        "high",
        "medium",
        "low",
        "any-finding",
    ):
        raise ConvergeError(f"Unsupported fail_on value: {settings.fail_on}")
    if (
        settings.claude_max_budget_usd is not None
        and settings.claude_max_budget_usd <= 0
    ):
        raise ConvergeError("claude_max_budget_usd must be positive")
    if (
        settings.copilot_max_ai_credits is not None
        and settings.copilot_max_ai_credits < 30
    ):
        raise ConvergeError("copilot_max_ai_credits must be at least 30")
    return settings


def load_settings(path: Path | None) -> Settings:
    if path is None:
        return Settings()
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise ConvergeError(f"Invalid configuration {path}: {exc}") from exc
    unknown = sorted(set(data) - ALLOWED_KEYS)
    if unknown:
        raise ConvergeError(f"Unknown configuration keys: {', '.join(unknown)}")
    values: dict[str, Any] = dict(data)
    if "reviewers" in values:
        reviewers = values["reviewers"]
        if not isinstance(reviewers, list) or not all(
            isinstance(v, str) for v in reviewers
        ):
            raise ConvergeError(
                "reviewers must be an array of provider[:model] strings"
            )
        values["reviewers"] = tuple(ReviewerSpec.parse(v) for v in reviewers)
    if "final_decider" in values:
        if not isinstance(values["final_decider"], str):
            raise ConvergeError("final_decider must be a provider[:model] string")
        values["final_decider"] = ReviewerSpec.parse(values["final_decider"])
    if "context_files" in values:
        files = values["context_files"]
        if not isinstance(files, list) or not all(isinstance(v, str) for v in files):
            raise ConvergeError("context_files must be an array of paths")
        values["context_files"] = tuple(files)
    try:
        return _validate(replace(Settings(), **values))
    except TypeError as exc:
        raise ConvergeError(f"Invalid configuration values: {exc}") from exc


def override_settings(settings: Settings, **values: Any) -> Settings:
    supplied = {key: value for key, value in values.items() if value is not None}
    if "reviewers" in supplied:
        supplied["reviewers"] = tuple(
            ReviewerSpec.parse(value) for value in supplied["reviewers"]
        )
    if "final_decider" in supplied:
        supplied["final_decider"] = ReviewerSpec.parse(supplied["final_decider"])
    if "context_files" in supplied:
        supplied["context_files"] = tuple(supplied["context_files"])
    return _validate(replace(settings, **supplied))
