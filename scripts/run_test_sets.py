"""Run the canonical four-part test sets offline or against the TTP agent."""

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
PROJECT_ROOT = SCRIPT_DIRECTORY.parent
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
    load_test_set_manifest,
    score_ttp_template_output,
    select_test_sets,
)
from cli_parser_agent.ttp_generation.validation import (  # noqa: E402
    validate_ttp_template,
)

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
        description="Run canonical four-part CLI parser test sets.",
    )
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("list", "preflight"):
        command = commands.add_parser(name)
        command.add_argument("--manifest", type=Path, required=True)
    run = commands.add_parser("run")
    run.add_argument("--manifest", type=Path, required=True)
    run.add_argument("--mode", choices=("baseline", "ttp-only"), required=True)
    selection = run.add_mutually_exclusive_group(required=True)
    selection.add_argument("--suite")
    selection.add_argument("--case", dest="case_ids", action="append")
    run.add_argument("--trials", type=_trials, default=1)
    run.add_argument("--concurrency", type=_concurrency, default=1)
    return parser


def _fingerprint(value: Any) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _configuration() -> tuple[
    TtpGeneratorSettings,
    GenerationPolicy,
    Path,
    dict[str, Any],
]:
    try:
        settings = TtpGeneratorSettings.from_env()
        policy = GenerationPolicy.from_env()
    except (TypeError, ValueError) as error:
        raise ScriptConfigurationError(
            f"model or generation configuration is invalid ({type(error).__name__})",
        ) from None
    artifact_root = _run_support.environment_path(
        "CLI_PARSER_TEST_SET_ARTIFACT_ROOT",
        PROJECT_ROOT / ".artifacts" / "test-set-evaluation",
    )
    configuration = {
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
    return settings, policy, artifact_root, configuration


def _case_metadata(case: Any) -> dict[str, Any]:
    return {
        "id": case.id,
        "command": case.command,
        "path": case.path,
        "suites": list(case.suites),
        "tags": list(case.tags),
        "files": {
            "schema": {"sha256": case.file_sha256["schema"]},
            "template": {"sha256": case.file_sha256["template"]},
            "expected": {"sha256": case.file_sha256["expected"]},
            "inputs": [
                {"name": f"{index:03d}.txt", "sha256": item.sha256}
                for index, item in enumerate(case.inputs, start=1)
            ],
        },
    }


async def _run_ttp_trial(case: Any, settings: Any, policy: Any) -> dict[str, Any]:
    try:
        request = TemplateRequest(
            command_outputs=[item.text for item in case.inputs],
            result_schema=case.schema,
        )
        result = await TtpGenerator(
            settings=settings,
            policy=policy,
        ).generate_from_schema(request)
        acceptance = await asyncio.to_thread(
            independent_acceptance,
            result,
            request.command_outputs,
            policy,
        )
        result_payload = result.model_dump(mode="json")
        score = score_ttp_template_output(
            {
                "generation_result": result_payload,
                "independent_acceptance": acceptance,
            },
            case.expected_records,
        )
        return {
            "generation_result": result_payload,
            "independent_acceptance": acceptance,
            "score": score,
            "exception_type": None,
        }
    except asyncio.CancelledError:
        raise
    except Exception as error:
        return {
            "generation_result": None,
            "independent_acceptance": None,
            "score": score_ttp_template_output({}, case.expected_records),
            "exception_type": type(error).__name__,
        }


def _run_baseline(cases: Sequence[Any]) -> int:
    passed = 0
    for case in cases:
        validation = validate_ttp_template(
            case.template,
            [item.text for item in case.inputs],
            case.schema,
            timeout_seconds=20.0,
            max_result_bytes=8 * 1024 * 1024,
        )
        ok = not validation.issues and validation.records == list(case.expected_records)
        print(f"{case.id}: {'PASS' if ok else 'FAIL'} inputs={len(case.inputs)}")
        passed += int(ok)
    print(f"baseline: {passed}/{len(cases)} cases passed")
    return 0 if passed == len(cases) else 1


async def _run_ttp(args: argparse.Namespace, cases: Sequence[Any]) -> int:
    settings, policy, artifact_root, configuration = _configuration()
    run_directory = _run_support.new_run_directory(artifact_root)
    (run_directory / "cases").mkdir()
    config_fingerprint = _fingerprint(configuration)
    semaphore = asyncio.Semaphore(args.concurrency)

    async def execute(case: Any, trial_index: int) -> dict[str, Any]:
        async with semaphore:
            started_at = datetime.now(UTC).isoformat()
            payload = await _run_ttp_trial(case, settings, policy)
            finished_at = datetime.now(UTC).isoformat()
            document = {
                "runner_version": RUNNER_VERSION,
                "mode": "ttp-only",
                "started_at": started_at,
                "finished_at": finished_at,
                "config_fingerprint": config_fingerprint,
                "case": _case_metadata(case),
                "trial_index": trial_index,
                **payload,
            }
            _run_support.write_json(
                run_directory / "cases" / f"{case.id}.trial-{trial_index + 1:02d}.json",
                document,
            )
            metrics = payload["score"]["metrics"]
            result_payload = payload["generation_result"]
            trace_id = (
                result_payload.get("metadata", {}).get("laminar_trace_id")
                if isinstance(result_payload, Mapping)
                else None
            )
            return {
                "case_id": case.id,
                "trial_index": trial_index,
                "strict_pass": metrics["candidate_pass"] == 1.0,
                "metrics": metrics,
                "exception_type": payload["exception_type"],
                "trace_id": trace_id,
            }

    print(
        f"running TTP-only evaluation: cases={len(cases)} trials={args.trials} "
        f"concurrency={args.concurrency}",
        flush=True,
    )
    trials = await asyncio.gather(
        *(
            execute(case, trial_index)
            for case in cases
            for trial_index in range(args.trials)
        ),
    )
    summary = {
        "runner_version": RUNNER_VERSION,
        "mode": "ttp-only",
        "status": (
            "success" if all(item["strict_pass"] for item in trials) else "failed"
        ),
        "manifest": {
            "path": str(args.manifest.resolve()),
        },
        "config_fingerprint": config_fingerprint,
        "configuration": configuration,
        "case_count": len(cases),
        "trial_count": len(trials),
        "strict_pass_count": sum(item["strict_pass"] for item in trials),
        "metrics": aggregate_trial_scores(trials),
        "cases": {
            case.id: aggregate_trial_scores(
                [item for item in trials if item["case_id"] == case.id],
            )
            for case in cases
        },
        "trials": trials,
    }
    summary_path = run_directory / "summary.json"
    _run_support.write_json(summary_path, summary)
    print(f"status: {summary['status']}")
    print(f"summary_json: {summary_path}")
    return 0 if summary["status"] == "success" else 1


def _list_cases(args: argparse.Namespace) -> int:
    manifest = load_test_set_manifest(args.manifest)
    for case in manifest.cases:
        print(
            json.dumps(
                {
                    "id": case.id,
                    "command": case.command,
                    "suites": list(case.suites),
                    "tags": list(case.tags),
                    "input_count": len(case.inputs),
                    "path": case.path,
                },
                ensure_ascii=False,
                sort_keys=True,
            ),
        )
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    try:
        if args.command == "list":
            return _list_cases(args)
        manifest = load_test_set_manifest(args.manifest)
        if args.command == "preflight":
            print(
                f"preflight passed: cases={len(manifest.cases)} "
                f"sha256={manifest.sha256}",
            )
            return 0
        cases = select_test_sets(
            manifest,
            suite=args.suite,
            case_ids=args.case_ids or (),
        )
        if args.mode == "baseline":
            return _run_baseline(cases)
        return asyncio.run(_run_ttp(args, cases))
    except (HarnessError, ScriptConfigurationError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("cancelled", file=sys.stderr)
        return 130
    finally:
        if args.command == "run" and getattr(args, "mode", None) == "ttp-only":
            _run_support.flush_laminar()


if __name__ == "__main__":
    raise SystemExit(main())
