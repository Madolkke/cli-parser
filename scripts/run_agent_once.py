"""Run one environment-configured TTP generation request without CLI arguments."""

from __future__ import annotations

import asyncio
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
    GenerationRequest,
    TtpGenerator,
    TtpGeneratorSettings,
)

ScriptConfigurationError = _run_support.ScriptConfigurationError

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _configuration(
    environ: Mapping[str, str] | None = None,
) -> tuple[TtpGeneratorSettings, GenerationPolicy, tuple[Path, ...], Path]:
    """Read all development-run settings from the supplied environment."""

    settings = TtpGeneratorSettings.from_env(environ)
    policy = GenerationPolicy.from_env(environ)
    command_output_files = _run_support.required_path_list(
        "CLI_PARSER_ONCE_INPUT_FILES",
        environ=environ,
    )
    artifact_root = _run_support.environment_path(
        "CLI_PARSER_ONCE_ARTIFACT_ROOT",
        PROJECT_ROOT / ".artifacts" / "agent-once",
        environ=environ,
    )
    return settings, policy, command_output_files, artifact_root


def _display_path(path: Path) -> str:
    return _run_support.display_path(path, project_root=PROJECT_ROOT)


def _load_command_outputs(
    paths: tuple[Path, ...],
) -> tuple[list[str], list[dict[str, Any]]]:
    return _run_support.load_command_outputs(paths, project_root=PROJECT_ROOT)


def _new_run_directory(artifact_root: Path) -> Path:
    return _run_support.new_run_directory(artifact_root)


def _write_json(path: Path, value: Any) -> None:
    _run_support.write_json(path, value)


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
    settings, policy, command_output_files, artifact_root = _configuration()
    command_outputs, input_metadata = _load_command_outputs(command_output_files)

    print(f"model: {settings.model_name}")
    print(f"base_url: {_run_support.sanitize_base_url(settings.base_url)}")
    print(f"command_outputs: {len(command_outputs)}")
    for index, item in enumerate(input_metadata):
        print(f"  [{index}] {item['path']} ({item['bytes']} bytes)")
    print("running agent...", flush=True)

    started_at = datetime.now(UTC).isoformat()
    result = await TtpGenerator(settings=settings, policy=policy).generate(
        GenerationRequest(command_outputs=command_outputs),
    )
    finished_at = datetime.now(UTC).isoformat()

    run_directory = _new_run_directory(artifact_root)
    result_path = run_directory / "result.json"
    _write_json(
        result_path,
        {
            "script_version": 1,
            "started_at": started_at,
            "finished_at": finished_at,
            "model": {
                "name": settings.model_name,
                "base_url": _run_support.sanitize_base_url(settings.base_url),
            },
            "input_files": input_metadata,
            "generation_result": result.model_dump(mode="json"),
        },
    )
    _print_result_summary(result, result_path)
    return 0 if result.status == "success" else 1


def _flush_laminar() -> None:
    _run_support.flush_laminar()


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
