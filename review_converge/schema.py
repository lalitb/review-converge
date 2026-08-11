from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

from .core import ConvergeError, atomic_write_json, load_json


def generate_schemas(
    source_dir: Path, output_dir: Path, reviewer_ids: list[str]
) -> dict[str, Path]:
    generated = output_dir / "schemas"
    generated.mkdir(parents=True, exist_ok=True)
    result: dict[str, Path] = {}
    for name in ("review", "reconciliation", "final"):
        schema = copy.deepcopy(load_json(source_dir / f"{name}.json"))
        # Claude Code's schema validator accepts this schema vocabulary but
        # rejects the Draft 2020-12 meta-schema URI itself. The declaration is
        # annotation-only here, so omit it from portable per-run schemas.
        schema.pop("$schema", None)
        if name in ("review", "reconciliation"):
            schema["properties"]["reviewer"] = {"enum": reviewer_ids}
        path = generated / f"{name}.json"
        atomic_write_json(path, schema)
        result[name] = path
    return result


def validate_json_schema(value: Any, schema_path: Path) -> None:
    try:
        import jsonschema
    except ImportError as exc:
        raise ConvergeError(
            "Copilot structured output validation requires: pip install 'review-converge[copilot]'"
        ) from exc
    try:
        jsonschema.validate(value, json.loads(schema_path.read_text(encoding="utf-8")))
    except (
        json.JSONDecodeError,
        jsonschema.ValidationError,
        jsonschema.SchemaError,
    ) as exc:
        raise ConvergeError(
            f"Structured output failed JSON Schema validation: {exc}"
        ) from exc


def require_json_schema() -> None:
    """Fail before an invocation when the optional validator is unavailable."""
    try:
        import jsonschema  # noqa: F401
    except ImportError as exc:
        raise ConvergeError(
            "Copilot structured output validation requires: "
            "pip install 'review-converge[copilot]'"
        ) from exc
