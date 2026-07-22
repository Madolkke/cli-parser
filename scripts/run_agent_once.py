"""Run one configured TTP generation request without command-line arguments.

Edit the constants in the configuration section to select another model, policy,
or set of command-output text files. The API key is deliberately excluded from
source control: it is read from ``OPENAI_API_KEY`` or requested with hidden input.
"""

from __future__ import annotations

import asyncio
import getpass
import hashlib
import json
import os
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from cli_parser_agent import (
    GenerationPolicy,
    GenerationRequest,
    TtpGenerator,
    TtpGeneratorSettings,
)

# Configuration: edit these values, then run this file without arguments.
PROJECT_ROOT = Path(__file__).resolve().parents[1]
MODEL_NAME = "deepseek-v4-pro"
BASE_URL = "https://api.deepseek.com"

COMMAND_OUTPUT_FILES = (
    PROJECT_ROOT / "testdata/real_command_outputs/ntc_templates/cisco_ios/"
    "show_interfaces_status/cisco_ios_show_interfaces_status.raw",
    PROJECT_ROOT / "testdata/real_command_outputs/ntc_templates/cisco_ios/"
    "show_interfaces_status/cisco_ios_show_interfaces_status2.raw",
    PROJECT_ROOT / "testdata/real_command_outputs/ntc_templates/cisco_ios/"
    "show_interfaces_status/cisco_ios_show_interfaces_status_pvlan.raw",
)

ARTIFACT_ROOT = PROJECT_ROOT / ".artifacts" / "agent-once"
# Accuracy-first settings for this no-argument development runner.
TOTAL_TIMEOUT_SECONDS = 1_800.0
MAX_AGENT_ROUNDS = 24
MAX_TTP_SUBMISSIONS = 16
MAX_SCHEMA_NO_TOOL_RETRIES = 3
MAX_TTP_NO_TOOL_RETRIES = 3
TTP_VALIDATION_TIMEOUT_SECONDS = 20.0

STREAM = False
TEMPERATURE = 0.0
PARALLEL_TOOL_CALLS = False
MAX_TOKENS = 8_192
CONTEXT_SIZE = 128_000
MODEL_MAX_RETRIES = 2
MODEL_TIMEOUT_SECONDS = 120.0

MAX_COMMAND_OUTPUTS = 5
MAX_COMMAND_OUTPUT_BYTES = 1024 * 1024
API_KEY_ENVIRONMENT_VARIABLE = "OPENAI_API_KEY"


class ScriptConfigurationError(ValueError):
    """A local script setting or command-output file is invalid."""


def _display_path(path: Path) -> str:
    try:
        return str(path.relative_to(PROJECT_ROOT))
    except ValueError:
        return str(path)


def _resolve_api_key() -> str:
    value = os.getenv(API_KEY_ENVIRONMENT_VARIABLE, "").strip()
    if value:
        return value

    try:
        value = getpass.getpass(
            f"{API_KEY_ENVIRONMENT_VARIABLE} (input hidden): ",
        ).strip()
    except (EOFError, KeyboardInterrupt) as error:
        raise ScriptConfigurationError("API key input was cancelled.") from error
    if not value:
        raise ScriptConfigurationError("API key must not be empty.")
    return value


def _load_command_outputs(
    paths: tuple[Path, ...] = COMMAND_OUTPUT_FILES,
) -> tuple[list[str], list[dict[str, Any]]]:
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
        if not path.is_file():
            raise ScriptConfigurationError(
                f"Command-output file does not exist: {_display_path(path)}",
            )

        payload = path.read_bytes()
        if not payload:
            raise ScriptConfigurationError(
                f"Command-output file is empty: {_display_path(path)}",
            )
        if len(payload) > MAX_COMMAND_OUTPUT_BYTES:
            raise ScriptConfigurationError(
                "Command-output file exceeds 1 MiB: " + _display_path(path),
            )
        try:
            text = payload.decode("utf-8")
        except UnicodeDecodeError as error:
            raise ScriptConfigurationError(
                "Command-output file is not strict UTF-8: " + _display_path(path),
            ) from error
        if text.startswith("\ufeff"):
            raise ScriptConfigurationError(
                "Command-output file contains a UTF-8 BOM: " + _display_path(path),
            )
        if not text.strip():
            raise ScriptConfigurationError(
                "Command-output file contains only whitespace: " + _display_path(path),
            )

        outputs.append(text)
        file_metadata.append(
            {
                "path": _display_path(path),
                "bytes": len(payload),
                "sha256": hashlib.sha256(payload).hexdigest(),
            },
        )

    return outputs, file_metadata


def _new_run_directory() -> Path:
    run_id = datetime.now(UTC).strftime("%Y%m%dT%H%M%S.%fZ")
    path = ARTIFACT_ROOT / run_id
    path.mkdir(parents=True, exist_ok=False)
    return path


def _write_json(path: Path, value: Any) -> None:
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
        newline="\n",
    )


def _print_result_summary(result: Any, result_path: Path) -> None:
    metadata = result.metadata
    print(f"status: {result.status}")
    print(f"laminar_trace_id: {metadata.laminar_trace_id}")
    print(f"termination_reason: {metadata.termination_reason}")
    print(f"elapsed_seconds: {metadata.elapsed_seconds:.3f}")
    print(f"agent_rounds: {metadata.agent_rounds}")
    print(f"schema_agent_rounds: {metadata.schema_agent_rounds}")
    print(f"ttp_agent_rounds: {metadata.ttp_agent_rounds}")
    print(f"schema_sampled_char_count: {metadata.schema_sampled_char_count}")
    print(f"ttp_sampled_char_count: {metadata.ttp_sampled_char_count}")
    print(f"tool_call_starts: {metadata.tool_call_starts}")
    print(f"tool_result_errors: {metadata.tool_result_errors}")
    print(f"schema_submissions: {metadata.schema_submissions}")
    print(f"ttp_submissions: {metadata.ttp_submissions}")
    print(f"schema_no_tool_responses: {metadata.schema_no_tool_responses}")
    print(f"ttp_no_tool_responses: {metadata.ttp_no_tool_responses}")
    print(f"schema_no_tool_retries: {metadata.schema_no_tool_retries}")
    print(f"ttp_no_tool_retries: {metadata.ttp_no_tool_retries}")
    print(f"first_ttp_passed: {metadata.first_ttp_passed}")
    if result.issues:
        print("issues:")
        for issue in result.issues:
            location = f" path={issue.path}" if issue.path else ""
            output = (
                f" output_index={issue.output_index}"
                if issue.output_index is not None
                else ""
            )
            print(
                f"  - [{issue.stage}] {issue.code}{location}{output}: {issue.message}",
            )
    print(f"result_json: {result_path}")


async def _run() -> int:
    command_outputs, input_metadata = _load_command_outputs()
    api_key = _resolve_api_key()
    settings = TtpGeneratorSettings(
        api_key=api_key,
        model_name=MODEL_NAME,
        base_url=BASE_URL,
        stream=STREAM,
        temperature=TEMPERATURE,
        parallel_tool_calls=PARALLEL_TOOL_CALLS,
        max_tokens=MAX_TOKENS,
        context_size=CONTEXT_SIZE,
        model_max_retries=MODEL_MAX_RETRIES,
        model_timeout_seconds=MODEL_TIMEOUT_SECONDS,
    )
    policy = GenerationPolicy(
        total_timeout_seconds=TOTAL_TIMEOUT_SECONDS,
        max_agent_rounds=MAX_AGENT_ROUNDS,
        max_ttp_submissions=MAX_TTP_SUBMISSIONS,
        max_schema_no_tool_retries=MAX_SCHEMA_NO_TOOL_RETRIES,
        max_ttp_no_tool_retries=MAX_TTP_NO_TOOL_RETRIES,
        ttp_validation_timeout_seconds=TTP_VALIDATION_TIMEOUT_SECONDS,
    )

    print(f"model: {MODEL_NAME}")
    print(f"base_url: {BASE_URL}")
    print(f"command_outputs: {len(command_outputs)}")
    for index, item in enumerate(input_metadata):
        print(f"  [{index}] {item['path']} ({item['bytes']} bytes)")
    print("running agent...", flush=True)

    started_at = datetime.now(UTC).isoformat()
    result = await TtpGenerator(settings=settings, policy=policy).generate(
        GenerationRequest(command_outputs=command_outputs),
    )
    finished_at = datetime.now(UTC).isoformat()

    run_directory = _new_run_directory()
    result_path = run_directory / "result.json"
    _write_json(
        result_path,
        {
            "script_version": 1,
            "started_at": started_at,
            "finished_at": finished_at,
            "model": {
                "name": MODEL_NAME,
                "base_url": BASE_URL,
            },
            "input_files": input_metadata,
            "generation_result": result.model_dump(mode="json"),
        },
    )
    _print_result_summary(result, result_path)
    return 0 if result.status == "success" else 1


def _flush_laminar() -> None:
    from lmnr import Laminar

    if not Laminar.is_initialized():
        return
    try:
        flushed = Laminar.flush()
    except Exception as error:
        print(
            f"warning: Laminar flush failed ({type(error).__name__})",
            file=sys.stderr,
        )
        return
    if not flushed:
        print("warning: Laminar flush did not complete", file=sys.stderr)


def main() -> int:
    try:
        return asyncio.run(_run())
    except ScriptConfigurationError as error:
        print(f"configuration error: {error}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("cancelled", file=sys.stderr)
        return 130
    finally:
        _flush_laminar()


if __name__ == "__main__":
    raise SystemExit(main())
