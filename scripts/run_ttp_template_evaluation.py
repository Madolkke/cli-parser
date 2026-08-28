"""Run scored TTP-only evaluations against caller-owned manifest fixtures."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import sys
from collections.abc import Mapping, Sequence
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
from cli_parser_agent.evaluation import (  # noqa: E402
    HarnessError,
    aggregate_trial_scores,
    independent_acceptance,
    load_ttp_template_manifest,
    score_ttp_template_output,
    select_ttp_template_cases,
)

PROJECT_ROOT = Path(__file__).resolve().parents[1]
RUNNER_VERSION = 1
ScriptConfigurationError = _run_support.ScriptConfigurationError


def _positive_int(value: str) -> int:
    try:
        parsed = int(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("must be an integer") from error
    if parsed < 1:
        raise argparse.ArgumentTypeError("must be positive")
    return parsed


def _trials(value: str) -> int:
    parsed = _positive_int(value)
    if parsed > 10:
        raise argparse.ArgumentTypeError("must be between 1 and 10")
    return parsed


def _concurrency(value: str) -> int:
    parsed = _positive_int(value)
    if parsed > 4:
        raise argparse.ArgumentTypeError("must be between 1 and 4")
    return parsed


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run scored TTP-only evaluations from an external manifest.",
    )
    commands = parser.add_subparsers(dest="command", required=True)
    list_command = commands.add_parser("list", help="list preflighted TTP-only cases")
    list_command.add_argument("--manifest", type=Path, required=True)
    preflight = commands.add_parser(
        "preflight",
        help="validate a manifest without networking",
    )
    preflight.add_argument("--manifest", type=Path, required=True)
    run = commands.add_parser("run", help="run selected TTP-only cases")
    run.add_argument("--manifest", type=Path, required=True)
    selection = run.add_mutually_exclusive_group(required=True)
    selection.add_argument("--suite")
    selection.add_argument("--case", dest="case_ids", action="append")
    run.add_argument("--trials", type=_trials, default=1)
    run.add_argument("--concurrency", type=_concurrency, default=1)
    return parser


def _fingerprint(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8"),
    ).hexdigest()


def _configuration(
) -> tuple[TtpGeneratorSettings, GenerationPolicy, Path, dict[str, Any]]:
    try:
        settings = TtpGeneratorSettings.from_env()
        policy = GenerationPolicy.from_env()
    except (TypeError, ValueError) as error:
        raise ScriptConfigurationError(
            f"model or generation configuration is invalid ({type(error).__name__})",
        ) from None
    artifact_root = _run_support.environment_path(
        "CLI_PARSER_TTP_TEMPLATE_EVAL_ARTIFACT_ROOT",
        PROJECT_ROOT / ".artifacts" / "ttp-template-evaluation",
    )
    snapshot = {
        "model": {
            "name": settings.model_name,
            "base_url": _run_support.sanitize_base_url(settings.base_url),
            "stream": settings.stream,
            "temperature": settings.temperature,
            "parallel_tool_calls": settings.parallel_tool_calls,
            "max_tokens": settings.max_tokens,
            "context_size": settings.context_size,
            "model_timeout_seconds": settings.model_timeout_seconds,
            "thinking_enable": settings.thinking_enable,
            "reasoning_effort": settings.reasoning_effort,
            "extra_body_configured": settings.extra_body is not None,
        },
        "policy": policy.model_dump(mode="json"),
    }
    return settings, policy, artifact_root, snapshot


def _case_file_name(case_id: str, trial_index: int) -> str:
    return f"{case_id}.trial-{trial_index + 1:02d}.json"


def _case_metadata(case: Any) -> dict[str, Any]:
    return {
        "case_id": case.id,
        "suites": list(case.suites),
        "tags": list(case.tags),
        "schema": {"path": case.schema_path, "sha256": case.schema_sha256},
        "inputs": [
            {"path": item.path, "sha256": item.sha256}
            for item in case.inputs
        ],
        "expected_records": {
            "path": case.target.path,
            "sha256": case.target.sha256,
        },
    }


async def _run_trial(case: Any, settings: Any, policy: Any) -> dict[str, Any]:
    """Run one independent generator and retain its complete local result."""

    try:
        request = TemplateRequest(
            command_outputs=[item.text for item in case.inputs],
            result_schema=case.schema,
        )
        generator = TtpGenerator(settings=settings, policy=policy)
        result = await generator.generate_from_schema(request)
        acceptance = await asyncio.to_thread(
            independent_acceptance,
            result,
            request.command_outputs,
            policy,
        )
        result_payload = result.model_dump(mode="json")
        scored = score_ttp_template_output(
            {
                "generation_result": result_payload,
                "independent_acceptance": acceptance,
            },
            case.target.records,
        )
        return {
            "generation_result": result_payload,
            "independent_acceptance": acceptance,
            "score": scored,
            "exception_type": None,
        }
    except asyncio.CancelledError:
        raise
    except Exception as error:
        scored = score_ttp_template_output({}, case.target.records)
        return {
            "generation_result": None,
            "independent_acceptance": None,
            "score": scored,
            "exception_type": type(error).__name__,
        }


async def _run(args: argparse.Namespace) -> int:
    manifest = load_ttp_template_manifest(args.manifest)
    cases = select_ttp_template_cases(
        manifest,
        suite=args.suite,
        case_ids=args.case_ids or (),
    )
    settings, policy, artifact_root, configuration = _configuration()
    run_directory = _run_support.new_run_directory(artifact_root)
    (run_directory / "cases").mkdir()
    config_fingerprint = _fingerprint(configuration)
    semaphore = asyncio.Semaphore(args.concurrency)

    async def execute(case: Any, trial_index: int) -> dict[str, Any]:
        async with semaphore:
            started_at = datetime.now(UTC).isoformat()
            payload = await _run_trial(case, settings, policy)
            finished_at = datetime.now(UTC).isoformat()
            metrics = payload["score"]["metrics"]
            document = {
                "runner_version": RUNNER_VERSION,
                "mode": "ttp_template_only",
                "started_at": started_at,
                "finished_at": finished_at,
                "config_fingerprint": config_fingerprint,
                "case": _case_metadata(case),
                "trial_index": trial_index,
                **payload,
            }
            _run_support.write_json(
                run_directory / "cases" / _case_file_name(case.id, trial_index),
                document,
            )
            return {
                "case_id": case.id,
                "trial_index": trial_index,
                "strict_pass": metrics["candidate_pass"] == 1.0,
                "metrics": metrics,
                "exception_type": payload["exception_type"],
                "trace_id": (
                    payload["generation_result"].get("metadata", {}).get(
                        "laminar_trace_id",
                    )
                    if isinstance(payload["generation_result"], Mapping)
                    else None
                ),
            }

    print(
        f"running TTP-only evaluation: cases={len(cases)} trials={args.trials} "
        f"concurrency={args.concurrency}",
        flush=True,
    )
    tasks = [
        execute(case, trial_index)
        for case in cases
        for trial_index in range(args.trials)
    ]
    trials = await asyncio.gather(*tasks)
    case_summary = {
        case.id: aggregate_trial_scores(
            [trial for trial in trials if trial["case_id"] == case.id],
        )
        for case in cases
    }
    all_passed = all(trial["strict_pass"] for trial in trials)
    summary = {
        "runner_version": RUNNER_VERSION,
        "mode": "ttp_template_only",
        "status": "success" if all_passed else "failed",
        "manifest": {
            "path": str(manifest.path),
            "sha256": manifest.sha256,
            "version": manifest.version,
        },
        "config_fingerprint": config_fingerprint,
        "configuration": configuration,
        "case_count": len(cases),
        "trial_count": len(trials),
        "strict_pass_count": sum(trial["strict_pass"] for trial in trials),
        "metrics": aggregate_trial_scores(trials),
        "cases": case_summary,
        "trials": trials,
    }
    summary_path = run_directory / "summary.json"
    _run_support.write_json(summary_path, summary)
    print(f"status: {summary['status']}")
    print(f"summary_json: {summary_path}")
    return 0 if all_passed else 1


def _command_list(args: argparse.Namespace) -> int:
    manifest = load_ttp_template_manifest(args.manifest)
    for case in manifest.cases:
        print(
            json.dumps(
                {
                    "id": case.id,
                    "suites": list(case.suites),
                    "tags": list(case.tags),
                    "input_count": len(case.inputs),
                    "schema": case.schema_path,
                },
                ensure_ascii=False,
                sort_keys=True,
            ),
        )
    return 0


def _command_preflight(args: argparse.Namespace) -> int:
    manifest = load_ttp_template_manifest(args.manifest)
    print(
        f"preflight passed: cases={len(manifest.cases)} sha256={manifest.sha256}",
    )
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    try:
        if args.command == "list":
            return _command_list(args)
        if args.command == "preflight":
            return _command_preflight(args)
        return asyncio.run(_run(args))
    except (HarnessError, ScriptConfigurationError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("cancelled", file=sys.stderr)
        return 130
    except Exception as error:
        print(
            "error: TTP template evaluation stopped unexpectedly "
            f"({type(error).__name__})",
            file=sys.stderr,
        )
        return 2
    finally:
        _run_support.flush_laminar()


if __name__ == "__main__":
    raise SystemExit(main())
