from __future__ import annotations

import json
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import __version__
from .core import ConvergeError, atomic_write_json, load_json, sha256_bytes, sha256_file
from .models import InvocationResult, Reviewer

FORMAT_VERSION = 1


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def capture_context(
    repo_dir: Path, output_dir: Path, requested: tuple[str, ...]
) -> list[dict[str, Any]]:
    repo_dir = repo_dir.resolve()
    output_dir = output_dir.resolve()
    destination_root = output_dir / "context"
    captured: list[dict[str, Any]] = []
    seen: set[Path] = set()
    for index, raw_path in enumerate(requested, 1):
        source = (repo_dir / raw_path).resolve()
        if not source.is_relative_to(repo_dir):
            raise ConvergeError(
                f"Context file must be inside the reviewed checkout: {raw_path}"
            )
        if source in seen:
            raise ConvergeError(f"Context file was selected more than once: {raw_path}")
        seen.add(source)
        if not source.is_file():
            raise ConvergeError(
                f"Context file does not exist or is not a file: {raw_path}"
            )
        relative = source.relative_to(repo_dir)
        destination = destination_root / f"{index:02d}" / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, destination)
        captured.append(
            {
                "source": str(relative),
                "artifact": str(destination.relative_to(output_dir)),
                "sha256": sha256_file(destination),
                "size_bytes": destination.stat().st_size,
            }
        )
    return captured


def verify_context(
    repo_dir: Path, output_dir: Path, captured: list[dict[str, Any]]
) -> None:
    repo_dir = repo_dir.resolve()
    output_dir = output_dir.resolve()
    for item in captured:
        source = (repo_dir / item["source"]).resolve()
        artifact = (output_dir / item["artifact"]).resolve()
        expected = item["sha256"]
        if not source.is_file() or sha256_file(source) != expected:
            raise ConvergeError(
                f"Context source changed since snapshot: {item['source']}"
            )
        if not artifact.is_file() or sha256_file(artifact) != expected:
            raise ConvergeError(
                f"Captured context artifact is missing or changed: {item['artifact']}"
            )


def configuration_fingerprint(value: dict[str, Any]) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    return sha256_bytes(encoded)


def create_run_manifest(
    output_dir: Path,
    *,
    config: dict[str, Any],
    reviewers: list[Reviewer],
    final_decider: Reviewer,
    adapter_versions: dict[str, str],
    prompt_hashes: dict[str, str],
    schema_hashes: dict[str, str],
) -> dict[str, Any]:
    manifest = {
        "format_version": FORMAT_VERSION,
        "cli_version": __version__,
        "created_at": utc_now(),
        "updated_at": utc_now(),
        "configuration": config,
        "configuration_sha256": configuration_fingerprint(config),
        "reviewers": [
            {
                "slot": reviewer.slot,
                **reviewer.spec.to_json(),
                "cli_version": adapter_versions[reviewer.slot],
            }
            for reviewer in reviewers
        ],
        "final_decider": {
            "slot": final_decider.slot,
            **final_decider.spec.to_json(),
            "cli_version": adapter_versions[final_decider.slot],
        },
        "prompt_sha256": prompt_hashes,
        "schema_sha256": schema_hashes,
        "stages": {
            "snapshot": "complete",
            "initial": {},
            "rounds": {},
            "final": "pending",
        },
    }
    atomic_write_json(output_dir / "run.json", manifest)
    atomic_write_json(
        output_dir / "usage.json",
        {"format_version": 1, "invocations": [], "totals": {}},
    )
    return manifest


def load_run_manifest(output_dir: Path) -> dict[str, Any]:
    manifest = load_json(output_dir / "run.json")
    if manifest.get("format_version") != FORMAT_VERSION:
        raise ConvergeError(
            f"Unsupported run artifact format: {manifest.get('format_version')!r}"
        )
    return manifest


def update_stage(output_dir: Path, path: tuple[str, ...], value: Any) -> None:
    manifest = load_run_manifest(output_dir)
    target = manifest["stages"]
    for key in path[:-1]:
        target = target.setdefault(key, {})
    target[path[-1]] = value
    manifest["updated_at"] = utc_now()
    atomic_write_json(output_dir / "run.json", manifest)


def artifact_descriptor(output_dir: Path, path: Path) -> dict[str, str]:
    resolved = path.resolve()
    if not resolved.is_relative_to(output_dir.resolve()):
        raise ConvergeError(f"Artifact is outside the run directory: {path}")
    return {
        "artifact": str(resolved.relative_to(output_dir.resolve())),
        "sha256": sha256_file(resolved),
    }


def verify_recorded_artifacts(output_dir: Path, manifest: dict[str, Any]) -> None:
    def visit(value: Any) -> None:
        if isinstance(value, dict):
            if set(value) == {"artifact", "sha256"}:
                path = (output_dir / value["artifact"]).resolve()
                if not path.is_relative_to(output_dir.resolve()):
                    raise ConvergeError(
                        f"Recorded artifact escapes run directory: {value['artifact']}"
                    )
                if not path.is_file() or sha256_file(path) != value["sha256"]:
                    raise ConvergeError(
                        f"Recorded artifact is missing or changed: {value['artifact']}"
                    )
                return
            for child in value.values():
                visit(child)
        elif isinstance(value, list):
            for child in value:
                visit(child)

    visit(manifest.get("stages", {}))


def record_invocation(
    output_dir: Path,
    *,
    stage: str,
    reviewer: Reviewer,
    cli_version: str,
    result: InvocationResult,
    outcome: str = "completed",
    error: str | None = None,
) -> None:
    ledger = load_json(output_dir / "usage.json")
    entry = {
        "stage": stage,
        "outcome": outcome,
        "reviewer": reviewer.slot,
        "provider": reviewer.spec.provider,
        "model_requested": reviewer.spec.model,
        "model_resolved": result.model_resolved,
        "cli_version": cli_version,
        "attempts": result.attempts,
        "duration_seconds": round(result.duration_seconds, 3),
        "provider_metadata": result.raw_metadata,
        **result.usage.to_json(),
    }
    if error:
        entry["error"] = error
    ledger["invocations"].append(entry)
    totals: dict[str, int | float | None] = {}
    for field in (
        "input_tokens",
        "output_tokens",
        "cached_input_tokens",
        "cost_usd",
        "ai_credits",
        "duration_seconds",
        "attempts",
    ):
        values = [
            item[field] for item in ledger["invocations"] if item.get(field) is not None
        ]
        totals[field] = round(sum(values), 6) if values else None
    totals["invocation_count"] = len(ledger["invocations"])
    ledger["totals"] = totals
    atomic_write_json(output_dir / "usage.json", ledger)
