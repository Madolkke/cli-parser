"""Run one environment-configured TTP-only request against a supplied schema."""

from __future__ import annotations

import asyncio
import json
import sys
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

SCRIPT_DIRECTORY = Path(__file__).resolve().parent
if str(SCRIPT_DIRECTORY) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIRECTORY))

import _agent_run_support as _run_support  # noqa: E402

from cli_parser_agent import (  # noqa: E402
    GenerationPolicy,
    TemplateRequest,
    TtpGenerator,
    TtpGeneratorSettings,
)

ScriptConfigurationError = _run_support.ScriptConfigurationError

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _required_schema_path(
    name: str,
    *,
    environ: Mapping[str, str] | None = None,
) -> Path:
    """Read the one required schema path, reusing the shared list parser."""

    paths = _run_support.required_path_list(name, environ=environ)
    if len(paths) != 1:
        raise ScriptConfigurationError(f"{name} must contain exactly one file path.")
    return paths[0]


def _configuration(
    environ: Mapping[str, str] | None = None,
) -> tuple[TtpGeneratorSettings, GenerationPolicy, tuple[Path, ...], Path, Path]:
    """Read all development-run settings from the supplied environment."""

    settings = TtpGeneratorSettings.from_env(environ)
    policy = GenerationPolicy.from_env(environ)
    command_output_files = _run_support.required_path_list(
        "CLI_PARSER_ONCE_INPUT_FILES",
        environ=environ,
    )
    schema_file = _required_schema_path(
        "CLI_PARSER_TTP_ONCE_SCHEMA_FILE",
        environ=environ,
    )
    artifact_root = _run_support.environment_path(
        "CLI_PARSER_TTP_ONCE_ARTIFACT_ROOT",
        PROJECT_ROOT / ".artifacts" / "ttp-phase-once",
        environ=environ,
    )
    return settings, policy, command_output_files, schema_file, artifact_root


def _load_schema(path: Path) -> dict[str, Any]:
    """Load a strict UTF-8 JSON schema document from disk."""

    display = _run_support.display_path(path, project_root=PROJECT_ROOT)
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as error:
        raise ScriptConfigurationError(
            f"Could not read schema file {display}: {error}",
        ) from error
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as error:
        raise ScriptConfigurationError(
            f"Schema file {display} is not valid JSON: {error}",
        ) from error
    if not isinstance(value, dict):
        raise ScriptConfigurationError(
            f"Schema file {display} must contain a JSON object.",
        )
    return value


def _print_result_summary(result: Any, result_path: Path) -> None:
    metadata = result.metadata
    print(f"status: {result.status}")
    print(f"laminar_trace_id: {metadata.laminar_trace_id}")
    print(f"termination_reason: {metadata.termination_reason}")
    print(f"elapsed_seconds: {metadata.elapsed_seconds:.3f}")
    print(f"agent_rounds: {metadata.agent_rounds}")
    print(f"ttp_agent_rounds: {metadata.ttp_agent_rounds}")
    print(f"ttp_sampled_char_count: {metadata.ttp_sampled_char_count}")
    print(f"tool_call_starts: {metadata.tool_call_starts}")
    print(f"tool_result_errors: {metadata.tool_result_errors}")
    print(f"ttp_submissions: {metadata.ttp_submissions}")
    print(f"ttp_no_tool_responses: {metadata.ttp_no_tool_responses}")
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
    (
        settings,
        policy,
        command_output_files,
        schema_file,
        artifact_root,
    ) = _configuration()
    command_outputs, input_metadata = _run_support.load_command_outputs(
        command_output_files,
        project_root=PROJECT_ROOT,
    )
    result_schema = _load_schema(schema_file)

    schema_display = _run_support.display_path(schema_file, project_root=PROJECT_ROOT)
    print(f"model: {settings.model_name}")
    print(f"base_url: {_run_support.sanitize_base_url(settings.base_url)}")
    print(f"schema_file: {schema_display}")
    print(f"command_outputs: {len(command_outputs)}")
    for index, item in enumerate(input_metadata):
        print(f"  [{index}] {item['path']} ({item['bytes']} bytes)")
    print("running ttp phase...", flush=True)

    started_at = datetime.now(UTC).isoformat()
    result = await TtpGenerator(
        settings=settings,
        policy=policy,
    ).generate_from_schema(
        TemplateRequest(
            command_outputs=command_outputs,
            result_schema=result_schema,
        ),
    )
    finished_at = datetime.now(UTC).isoformat()

    run_directory = _run_support.new_run_directory(artifact_root)
    result_path = run_directory / "result.json"
    _run_support.write_json(
        result_path,
        {
            "script_version": 1,
            "mode": "template_only",
            "started_at": started_at,
            "finished_at": finished_at,
            "model": {
                "name": settings.model_name,
                "base_url": _run_support.sanitize_base_url(settings.base_url),
            },
            "input_files": input_metadata,
            "schema_file": schema_display,
            "result_schema": result_schema,
            "generation_result": result.model_dump(mode="json"),
        },
    )
    _print_result_summary(result, result_path)
    return 0 if result.status == "success" else 1


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
        _run_support.flush_laminar()


if __name__ == "__main__":
    raise SystemExit(main())
