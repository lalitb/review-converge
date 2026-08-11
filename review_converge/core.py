from __future__ import annotations

import hashlib
import json
import os
import shlex
import subprocess
import tempfile
from collections.abc import Sequence
from pathlib import Path
from typing import Any


class ConvergeError(RuntimeError):
    """A user-facing operational or artifact validation failure."""


def run(
    argv: Sequence[str],
    *,
    cwd: Path,
    timeout: int,
    stdin: str | None = None,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    try:
        result = subprocess.run(
            list(argv),
            cwd=cwd,
            input=stdin,
            text=True,
            capture_output=True,
            timeout=timeout,
            check=False,
        )
    except FileNotFoundError as exc:
        raise ConvergeError(f"Required command not found: {argv[0]}") from exc
    except subprocess.TimeoutExpired as exc:
        raise ConvergeError(
            f"Command timed out after {timeout}s: {shlex.join(argv)}"
        ) from exc
    if check and result.returncode != 0:
        streams = []
        if result.stderr.strip():
            streams.append(f"stderr:\n{result.stderr.strip()}")
        if result.stdout.strip():
            streams.append(f"stdout:\n{result.stdout.strip()}")
        detail = "\n".join(streams)[-8000:]
        raise ConvergeError(
            f"Command failed ({result.returncode}): {shlex.join(argv)}\n{detail}"
        )
    return result


def atomic_write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary_path = Path(temporary)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as file:
            json.dump(value, file, indent=2, sort_keys=True)
            file.write("\n")
            file.flush()
            os.fsync(file.fileno())
        temporary_path.replace(path)
    finally:
        temporary_path.unlink(missing_ok=True)


def load_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ConvergeError(f"Invalid JSON artifact {path}: {exc}") from exc


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def sha256_file(path: Path) -> str:
    try:
        return sha256_bytes(path.read_bytes())
    except OSError as exc:
        raise ConvergeError(f"Cannot hash {path}: {exc}") from exc


def command_version(command: str, cwd: Path, timeout: int) -> str:
    result = run([command, "--version"], cwd=cwd, timeout=timeout)
    return (result.stdout.strip() or result.stderr.strip()).splitlines()[0]
