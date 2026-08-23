"""Serve the local WebUI without CLI arguments.

Binds the loopback interface only.  This is a single-user development and
operator surface, not a deployed service: there is no authentication, and runs
are stored as plain files under ``data/``.
"""

from __future__ import annotations

import sys
from collections.abc import Mapping
from pathlib import Path

SCRIPT_DIRECTORY = Path(__file__).resolve().parent
if str(SCRIPT_DIRECTORY) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIRECTORY))

import _agent_run_support as _run_support  # noqa: E402

from cli_parser_agent import (  # noqa: E402
    GenerationPolicy,
    TtpGenerator,
    TtpGeneratorSettings,
)
from cli_parser_agent.webui.app import create_app  # noqa: E402
from cli_parser_agent.webui.store import RunStore  # noqa: E402

ScriptConfigurationError = _run_support.ScriptConfigurationError

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8080


def _port(environ: Mapping[str, str]) -> int:
    raw = environ.get("CLI_PARSER_WEBUI_PORT", "").strip()
    if not raw:
        return DEFAULT_PORT
    if not raw.isascii() or not raw.isdecimal():
        raise ScriptConfigurationError(
            "CLI_PARSER_WEBUI_PORT must be a decimal integer from 1 to 65535.",
        )
    port = int(raw, 10)
    if not 1 <= port <= 65_535:
        raise ScriptConfigurationError(
            "CLI_PARSER_WEBUI_PORT must be a decimal integer from 1 to 65535.",
        )
    return port


def _host(environ: Mapping[str, str]) -> str:
    return environ.get("CLI_PARSER_WEBUI_HOST", "").strip() or DEFAULT_HOST


def main() -> int:
    import os

    import uvicorn

    environ = os.environ
    try:
        # Fail before binding a port when the model configuration is unusable.
        settings = TtpGeneratorSettings.from_env(environ)
        policy = GenerationPolicy.from_env(environ)
        host = _host(environ)
        port = _port(environ)
        data_root = _run_support.environment_path(
            "CLI_PARSER_WEBUI_DATA_ROOT",
            PROJECT_ROOT / "data",
            environ=environ,
        )
    except ScriptConfigurationError as error:
        print(f"configuration error: {error}", file=sys.stderr)
        return 2
    except (TypeError, ValueError) as error:
        print(
            f"configuration error: model settings are invalid ({type(error).__name__})",
            file=sys.stderr,
        )
        return 2

    app = create_app(
        store=RunStore(data_root),
        generator=TtpGenerator(settings=settings, policy=policy),
    )
    print(f"model: {settings.model_name}")
    print(f"base_url: {_run_support.sanitize_base_url(settings.base_url)}")
    print(f"data: {_run_support.display_path(data_root, project_root=PROJECT_ROOT)}")
    print(f"serving on http://{host}:{port}", flush=True)
    try:
        uvicorn.run(app, host=host, port=port, log_level="warning")
    except KeyboardInterrupt:
        print("stopped", file=sys.stderr)
        return 130
    finally:
        _run_support.flush_laminar()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
