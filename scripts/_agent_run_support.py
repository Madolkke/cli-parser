"""Shared helpers for the zero-argument development runners."""

from __future__ import annotations

import getpass
import hashlib
import json
import os
import sys
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, TextIO
from urllib.parse import urlsplit, urlunsplit

MAX_COMMAND_OUTPUTS = 5
MAX_COMMAND_OUTPUT_BYTES = 1024 * 1024
API_KEY_ENVIRONMENT_VARIABLE = "OPENAI_API_KEY"


class ScriptConfigurationError(ValueError):
    """A local script setting or command-output file is invalid."""


def display_path(path: Path, *, project_root: Path) -> str:
    """Return a stable project-relative path when possible."""

    try:
        return str(path.relative_to(project_root))
    except ValueError:
        return str(path)


def sanitize_base_url(base_url: str | None) -> str | None:
    """Remove credentials and query data before displaying or persisting a URL."""

    if base_url is None:
        return None
    try:
        parsed = urlsplit(base_url)
        port = parsed.port
        netloc = parsed.netloc
        if parsed.username is not None or parsed.password is not None:
            hostname = parsed.hostname
            if hostname is None:
                return "[REDACTED]"
            host = f"[{hostname}]" if ":" in hostname else hostname
            netloc = f"{host}:{port}" if port is not None else host
        return urlunsplit((parsed.scheme, netloc, parsed.path, "", ""))
    except ValueError:
        return "[REDACTED]"


def resolve_api_key(
    *,
    environ: Mapping[str, str] | None = None,
    input_stream: TextIO | None = None,
) -> str:
    """Resolve the model API key without ever echoing its value."""

    source = os.environ if environ is None else environ
    value = source.get(API_KEY_ENVIRONMENT_VARIABLE, "").strip()
    if value:
        return value

    try:
        value = getpass.getpass(
            f"{API_KEY_ENVIRONMENT_VARIABLE} (input hidden): ",
            stream=input_stream,
        ).strip()
    except (EOFError, KeyboardInterrupt) as error:
        raise ScriptConfigurationError("API key input was cancelled.") from error
    if not value:
        raise ScriptConfigurationError("API key must not be empty.")
    return value


def load_command_outputs(
    paths: tuple[Path, ...],
    *,
    project_root: Path,
) -> tuple[list[str], list[dict[str, Any]]]:
    """Load strict UTF-8 command-output fixtures and their safe metadata."""

    if not 1 <= len(paths) <= MAX_COMMAND_OUTPUTS:
        raise ScriptConfigurationError(
            f"Configure between 1 and {MAX_COMMAND_OUTPUTS} command-output files.",
        )

    resolved_paths = [path.expanduser().resolve() for path in paths]
    if len(set(resolved_paths)) != len(resolved_paths):
        raise ScriptConfigurationError("Command-output file paths must be unique.")

    outputs: list[str] = []
    file_metadata: list[dict[str, Any]] = []
    for path in resolved_paths:
        shown_path = display_path(path, project_root=project_root)
        if not path.is_file():
            raise ScriptConfigurationError(
                f"Command-output file does not exist: {shown_path}",
            )

        payload = path.read_bytes()
        if not payload:
            raise ScriptConfigurationError(
                f"Command-output file is empty: {shown_path}",
            )
        if len(payload) > MAX_COMMAND_OUTPUT_BYTES:
            raise ScriptConfigurationError(
                f"Command-output file exceeds 1 MiB: {shown_path}",
            )
        try:
            text = payload.decode("utf-8")
        except UnicodeDecodeError as error:
            raise ScriptConfigurationError(
                f"Command-output file is not strict UTF-8: {shown_path}",
            ) from error
        if text.startswith("\ufeff"):
            raise ScriptConfigurationError(
                f"Command-output file contains a UTF-8 BOM: {shown_path}",
            )
        if not text.strip():
            raise ScriptConfigurationError(
                f"Command-output file contains only whitespace: {shown_path}",
            )

        outputs.append(text)
        file_metadata.append(
            {
                "path": shown_path,
                "bytes": len(payload),
                "sha256": hashlib.sha256(payload).hexdigest(),
            },
        )

    return outputs, file_metadata


def new_run_directory(artifact_root: Path) -> Path:
    """Create a unique UTC-stamped artifact directory."""

    run_id = datetime.now(UTC).strftime("%Y%m%dT%H%M%S.%fZ")
    path = artifact_root / run_id
    path.mkdir(parents=True, exist_ok=False)
    return path


def write_json(path: Path, value: Any) -> None:
    """Write deterministic, human-readable UTF-8 JSON."""

    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
        newline="\n",
    )


def flush_laminar(*, error_stream: TextIO | None = None) -> bool:
    """Flush an initialized Laminar SDK without changing runner outcomes."""

    from lmnr import Laminar

    stream = sys.stderr if error_stream is None else error_stream
    if not Laminar.is_initialized():
        return True
    try:
        flushed = Laminar.flush()
    except Exception as error:
        print(
            f"warning: Laminar flush failed ({type(error).__name__})",
            file=stream,
        )
        return False
    if not flushed:
        print("warning: Laminar flush did not complete", file=stream)
        return False
    return True


__all__ = [
    "API_KEY_ENVIRONMENT_VARIABLE",
    "MAX_COMMAND_OUTPUT_BYTES",
    "MAX_COMMAND_OUTPUTS",
    "ScriptConfigurationError",
    "display_path",
    "flush_laminar",
    "load_command_outputs",
    "new_run_directory",
    "resolve_api_key",
    "sanitize_base_url",
    "write_json",
]
