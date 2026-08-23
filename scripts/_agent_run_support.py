"""Shared helpers for the zero-argument development runners."""

from __future__ import annotations

import hashlib
import os
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Any, TextIO
from urllib.parse import urlsplit, urlunsplit

MAX_COMMAND_OUTPUTS = 5
MAX_COMMAND_OUTPUT_BYTES = 1024 * 1024

_PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

# The run-directory and JSON writers live in the package so ``src`` never has
# to import from ``scripts``; the runners re-export them for their own use.
from cli_parser_agent.webui.store import new_run_id  # noqa: E402
from cli_parser_agent.webui.store import write_json as _write_json  # noqa: E402


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


def required_path_list(
    name: str,
    *,
    environ: Mapping[str, str] | None = None,
) -> tuple[Path, ...]:
    """Read one to five path entries separated by the platform path separator."""

    source = os.environ if environ is None else environ
    raw_value = source.get(name, "")
    values = tuple(
        Path(value.strip())
        for value in raw_value.split(os.pathsep)
        if value.strip()
    )
    if not values:
        raise ScriptConfigurationError(
            f"{name} must contain 1 to {MAX_COMMAND_OUTPUTS} file paths "
            f"separated by {os.pathsep!r}.",
        )
    if len(values) > MAX_COMMAND_OUTPUTS:
        raise ScriptConfigurationError(
            f"{name} must contain at most {MAX_COMMAND_OUTPUTS} file paths.",
        )
    return values


def environment_path(
    name: str,
    default: Path,
    *,
    environ: Mapping[str, str] | None = None,
) -> Path:
    """Read an optional filesystem path, retaining a documented default."""

    source = os.environ if environ is None else environ
    value = source.get(name, "").strip()
    return default if not value else Path(value)


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

    path = artifact_root / new_run_id()
    path.mkdir(parents=True, exist_ok=False)
    return path


def write_json(path: Path, value: Any) -> None:
    """Write deterministic, human-readable UTF-8 JSON."""

    _write_json(path, value)


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
    "MAX_COMMAND_OUTPUT_BYTES",
    "MAX_COMMAND_OUTPUTS",
    "ScriptConfigurationError",
    "display_path",
    "environment_path",
    "flush_laminar",
    "load_command_outputs",
    "new_run_directory",
    "required_path_list",
    "sanitize_base_url",
    "write_json",
]
