from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .core import ConvergeError

REVIEWER_SLOTS = ("r1", "r2")
PROVIDERS = frozenset({"claude", "codex", "copilot"})


@dataclass(frozen=True)
class ReviewerSpec:
    provider: str
    model: str | None = None

    @classmethod
    def parse(cls, value: str) -> ReviewerSpec:
        provider, separator, model = value.partition(":")
        provider = provider.strip().lower()
        model = model.strip() if separator else None
        if provider not in PROVIDERS:
            raise ConvergeError(
                f"Unknown reviewer provider {provider!r}; choose claude, codex, or copilot"
            )
        if separator and not model:
            raise ConvergeError(f"Reviewer model is empty: {value}")
        if provider == "copilot" and (not model or model.lower() == "auto"):
            raise ConvergeError(
                "Copilot reviewers require an explicit model; 'auto' is not allowed"
            )
        return cls(provider, model)

    @property
    def display_name(self) -> str:
        return f"{self.provider}:{self.model}" if self.model else self.provider

    def to_json(self) -> dict[str, str | None]:
        return {"provider": self.provider, "model_requested": self.model}


@dataclass(frozen=True)
class Reviewer:
    slot: str
    spec: ReviewerSpec

    def __post_init__(self) -> None:
        if self.slot not in (*REVIEWER_SLOTS, "decider"):
            raise ConvergeError(f"Invalid reviewer slot: {self.slot}")


@dataclass(frozen=True)
class Snapshot:
    source: str
    repo: str
    target: str
    pr: int | None
    head_sha: str
    merge_base_sha: str
    base_tip_sha: str
    base_ref: str
    head_ref: str
    directory: Path
    worktree_fingerprint: str | None = None


@dataclass
class Usage:
    input_tokens: int | None = None
    output_tokens: int | None = None
    cached_input_tokens: int | None = None
    cost_usd: float | None = None
    ai_credits: float | None = None

    def to_json(self) -> dict[str, int | float | None]:
        return {
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "cached_input_tokens": self.cached_input_tokens,
            "cost_usd": self.cost_usd,
            "ai_credits": self.ai_credits,
        }


@dataclass
class InvocationResult:
    value: dict[str, Any]
    usage: Usage = field(default_factory=Usage)
    model_resolved: str | None = None
    duration_seconds: float = 0.0
    attempts: int = 1
    raw_metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ExecutionResult:
    directory: Path
    verdict: str | None
    final_findings: tuple[dict[str, Any], ...] = ()
