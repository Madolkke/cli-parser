"""Run repository-backed black-box Agent evaluations through Laminar.

Edit the configuration constants below before a live run. The two key values are
deliberately kept as unusable placeholders in version control. A local operator may
replace them in this file, but must never stage or commit the populated file.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import math
import socket
import subprocess
import sys
import threading
import time
import urllib.request
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from cli_parser_agent.evaluation import (  # noqa: E402
    EvaluationCase,
    HarnessError,
    independent_acceptance,
    load_evaluation_manifest,
    safe_trial_facts,
    score_executor_output,
    select_cases,
)

# Local-only secrets. Replace these placeholders locally and never stage the file.
OPENAI_API_KEY = "REPLACE_WITH_LOCAL_KEY"
LMNR_PROJECT_API_KEY = "REPLACE_WITH_LOCAL_KEY"

# Model configuration.
MODEL_NAME = "deepseek-v4-pro"
MODEL_BASE_URL = "https://api.deepseek.com"
STREAM = False
TEMPERATURE = 0.0
PARALLEL_TOOL_CALLS = False
MAX_TOKENS = 8_192
CONTEXT_SIZE = 128_000
MODEL_MAX_RETRIES = 2
MODEL_TIMEOUT_SECONDS = 120.0

# Accuracy-first generation policy. Audited safety ceilings retain library defaults.
TOTAL_TIMEOUT_SECONDS = 1_800.0
MAX_AGENT_ROUNDS = 24
MAX_TTP_SUBMISSIONS = 16
MAX_SCHEMA_NO_TOOL_RETRIES = 3
MAX_TTP_NO_TOOL_RETRIES = 3
TTP_VALIDATION_TIMEOUT_SECONDS = 20.0

# Self-hosted Laminar configuration.
LMNR_BASE_URL = "http://127.0.0.1"
LMNR_HTTP_PORT = 8000
LMNR_GRPC_PORT = 8001
LMNR_FRONTEND_PORT = 5667

RUNNER_VERSION = 1
MANIFEST_PATH = PROJECT_ROOT / "evals" / "ttp_generation" / "manifest.json"
ARTIFACT_ROOT = PROJECT_ROOT / ".artifacts" / "agent-evals"
TELEMETRY_WAIT_SECONDS = 60.0
_PLACEHOLDER = "REPLACE_WITH_LOCAL_KEY"


class RunnerError(RuntimeError):
    """A bounded evaluation runner error that is safe to display."""


def _validate_local_key(value: str, name: str) -> str:
    if value == _PLACEHOLDER or not value.strip():
        raise RunnerError(f"{name} still contains the version-control placeholder")
    if len(value) < 8 or any(character.isspace() for character in value):
        raise RunnerError(f"{name} is not a valid local key literal")
    return value


def _configuration() -> tuple[Any, Any, dict[str, Any]]:
    from cli_parser_agent import GenerationPolicy, TtpGeneratorSettings

    settings = TtpGeneratorSettings(
        api_key=_validate_local_key(OPENAI_API_KEY, "OPENAI_API_KEY"),
        model_name=MODEL_NAME,
        base_url=MODEL_BASE_URL,
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
    snapshot = {
        "model": {
            "name": settings.model_name,
            "base_url": settings.base_url,
            "stream": settings.stream,
            "temperature": settings.temperature,
            "parallel_tool_calls": settings.parallel_tool_calls,
            "max_tokens": settings.max_tokens,
            "context_size": settings.context_size,
            "model_max_retries": settings.model_max_retries,
            "model_timeout_seconds": settings.model_timeout_seconds,
        },
        "policy": policy.model_dump(mode="json"),
        "laminar": {
            "base_url": LMNR_BASE_URL,
            "http_port": LMNR_HTTP_PORT,
            "grpc_port": LMNR_GRPC_PORT,
            "frontend_port": LMNR_FRONTEND_PORT,
        },
    }
    return settings, policy, snapshot


def _fingerprint(value: Any) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


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
        description="Preflight and run black-box TTP Agent evaluations.",
    )
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("list", help="list versioned evaluation cases")
    commands.add_parser("preflight", help="validate definitions without networking")
    run = commands.add_parser("run", help="run selected cases through Laminar")
    selection = run.add_mutually_exclusive_group(required=True)
    selection.add_argument("--suite")
    selection.add_argument("--case", dest="case_ids", action="append")
    run.add_argument("--trials", type=_trials, default=1)
    run.add_argument("--concurrency", type=_concurrency, default=1)
    run.add_argument("--name")
    return parser


def _command_list() -> int:
    manifest = load_evaluation_manifest(PROJECT_ROOT, MANIFEST_PATH)
    print("case_id\tsamples\tsuites\ttags\tcommand")
    for case in manifest.cases:
        print(
            f"{case.id}\t{len(case.inputs)}\t{','.join(case.suites)}\t"
            f"{','.join(case.tags)}\t{case.command}",
        )
    print(
        f"cases={len(manifest.cases)} "
        f"samples={sum(len(case.inputs) for case in manifest.cases)}",
    )
    return 0


def _command_preflight() -> int:
    manifest = load_evaluation_manifest(PROJECT_ROOT, MANIFEST_PATH)
    print(
        "preflight ok: "
        f"cases={len(manifest.cases)} "
        f"samples={sum(len(case.inputs) for case in manifest.cases)} "
        f"manifest_sha256={manifest.sha256}",
    )
    return 0


def _sql_endpoint() -> str:
    return f"{LMNR_BASE_URL.rstrip('/')}:{LMNR_HTTP_PORT}/v1/sql/query"


def _sql_query(query: str, parameters: Mapping[str, Any]) -> list[dict[str, Any]]:
    key = _validate_local_key(LMNR_PROJECT_API_KEY, "LMNR_PROJECT_API_KEY")
    payload = json.dumps(
        {"query": query, "parameters": dict(parameters)},
        separators=(",", ":"),
    ).encode("utf-8")
    request = urllib.request.Request(
        _sql_endpoint(),
        data=payload,
        method="POST",
        headers={
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=20.0) as response:
            value = json.loads(response.read().decode("utf-8", errors="strict"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise RunnerError(
            f"Laminar SQL request failed ({type(error).__name__})",
        ) from None
    rows = value.get("data") if isinstance(value, dict) else None
    if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
        raise RunnerError("Laminar SQL response has an unexpected shape")
    return rows


def _preflight_laminar() -> None:
    _validate_local_key(LMNR_PROJECT_API_KEY, "LMNR_PROJECT_API_KEY")
    try:
        with socket.create_connection(
            ("127.0.0.1", LMNR_GRPC_PORT),
            timeout=5.0,
        ):
            pass
    except OSError as error:
        raise RunnerError(
            f"Laminar gRPC endpoint is unavailable ({type(error).__name__})",
        ) from None
    rows = _sql_query("SELECT 1 AS ok", {})
    if rows != [{"ok": 1}]:
        raise RunnerError("Laminar SQL preflight did not return the expected result")


def _git_facts() -> dict[str, Any]:
    def run(*arguments: str) -> str | None:
        try:
            return subprocess.run(
                ["git", *arguments],
                cwd=PROJECT_ROOT,
                check=True,
                capture_output=True,
                text=True,
                timeout=10.0,
            ).stdout.strip()
        except (OSError, subprocess.SubprocessError):
            return None

    revision = run("rev-parse", "HEAD")
    status = run("status", "--porcelain")
    return {
        "revision": revision,
        "dirty": None if status is None else bool(status),
    }


def _new_run_directory() -> Path:
    ARTIFACT_ROOT.mkdir(parents=True, exist_ok=True)
    stem = datetime.now(UTC).strftime("%Y%m%dT%H%M%S.%fZ")
    for suffix in range(1000):
        candidate = ARTIFACT_ROOT / (stem if suffix == 0 else f"{stem}-{suffix:02d}")
        try:
            candidate.mkdir()
        except FileExistsError:
            continue
        return candidate
    raise RunnerError("could not allocate a unique evaluation artifact directory")


def _write_json(path: Path, value: Any) -> None:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        indent=2,
        sort_keys=True,
        allow_nan=False,
    )
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(f"{encoded}\n", encoding="utf-8", newline="\n")
    temporary.replace(path)


def _selection_group(manifest_version: int, cases: Sequence[EvaluationCase]) -> str:
    selection = _fingerprint([case.id for case in cases])[:12]
    return f"ttp-generation-v{manifest_version}-{selection}"


def _trial_key(case_id: str, trial_index: int) -> str:
    return f"{case_id}#{trial_index}"


def _materialize_datapoints(
    cases: Sequence[EvaluationCase],
    *,
    trials: int,
    config_fingerprint: str,
) -> list[Any]:
    from lmnr.sdk.types import Datapoint

    datapoints: list[Any] = []
    for case in cases:
        for trial_index in range(trials):
            key = _trial_key(case.id, trial_index)
            datapoints.append(
                Datapoint(
                    data={
                        "case_id": case.id,
                        "trial_index": trial_index,
                        "trial_key": key,
                        "command_outputs": [item.text for item in case.inputs],
                    },
                    target=case.target.as_datapoint_target(),
                    metadata={
                        "case_id": case.id,
                        "trial_index": trial_index,
                        "trial_key": key,
                        "suites": list(case.suites),
                        "tags": list(case.tags),
                        "input_sha256": [item.sha256 for item in case.inputs],
                        "target_sha256": case.target.sha256,
                        "config_fingerprint": config_fingerprint,
                    },
                ),
            )
    return datapoints


def _parse_json_object(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if not isinstance(value, str):
        return {}
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _telemetry_for_trace(trace_id: str) -> dict[str, Any]:
    rows = _sql_query(
        """
        SELECT
            countIf(name = 'evaluation') AS evaluation_span_count,
            countIf(name = 'executor') AS executor_span_count,
            countIf(name = 'ttp.generate') AS generation_span_count,
            countIf(name = 'schema.phase') AS schema_phase_count,
            countIf(name = 'ttp.phase') AS ttp_phase_count,
            countIf(span_type = 'LLM') AS llm_call_count,
            countIf(span_type = 'TOOL') AS tool_span_count,
            sumIf(input_tokens, span_type = 'LLM') AS input_tokens,
            sumIf(output_tokens, span_type = 'LLM') AS output_tokens,
            sumIf(total_cost, span_type = 'LLM') AS total_cost,
            sumIf(duration, span_type = 'LLM') AS llm_duration_seconds,
            sumIf(duration, span_type = 'TOOL') AS tool_duration_seconds,
            maxIf(duration, name = 'ttp.generate') AS generation_duration_seconds,
            sumIf(duration, name = 'schema.phase') AS schema_duration_seconds,
            sumIf(duration, name = 'ttp.phase') AS ttp_duration_seconds
        FROM spans
        WHERE trace_id = {trace_id:UUID}
          AND start_time > now() - INTERVAL 1 DAY
        """,
        {"trace_id": trace_id},
    )
    if len(rows) != 1:
        return {}
    return rows[0]


def _telemetry_complete(telemetry: Mapping[str, Any]) -> bool:
    return all(
        int(telemetry.get(name, 0) or 0) >= 1
        for name in (
            "evaluation_span_count",
            "executor_span_count",
            "generation_span_count",
            "schema_phase_count",
            "llm_call_count",
        )
    )


async def _collect_telemetry(
    evaluation_id: str,
    expected_count: int,
) -> tuple[dict[str, dict[str, Any]], bool]:
    deadline = time.monotonic() + TELEMETRY_WAIT_SECONDS
    while True:
        rows = await asyncio.to_thread(
            _sql_query,
            """
            SELECT trace_id, metadata, scores
            FROM evaluation_datapoints
            WHERE evaluation_id = {evaluation_id:UUID}
              AND updated_at > now() - INTERVAL 1 DAY
            ORDER BY index
            """,
            {"evaluation_id": evaluation_id},
        )
        by_trial: dict[str, dict[str, Any]] = {}
        for row in rows:
            metadata = _parse_json_object(row.get("metadata"))
            key = metadata.get("trial_key")
            trace_id = row.get("trace_id")
            if isinstance(key, str) and isinstance(trace_id, str):
                by_trial[key] = {
                    "trace_id": trace_id,
                    "laminar_scores": _parse_json_object(row.get("scores")),
                }
        if len(by_trial) == expected_count:
            all_complete = True
            for entry in by_trial.values():
                telemetry = await asyncio.to_thread(
                    _telemetry_for_trace,
                    entry["trace_id"],
                )
                entry["spans"] = telemetry
                entry["complete"] = _telemetry_complete(telemetry)
                all_complete = all_complete and entry["complete"]
            if all_complete:
                return by_trial, True
        if time.monotonic() >= deadline:
            return by_trial, False
        await asyncio.sleep(1.0)


def _percentile(values: Sequence[float], percentile: float) -> float:
    ordered = sorted(values)
    if not ordered:
        return 0.0
    index = max(0, math.ceil(percentile * len(ordered)) - 1)
    return ordered[index]


def _aggregate_trials(trials: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    by_case: dict[str, list[Mapping[str, Any]]] = {}
    for trial in trials:
        by_case.setdefault(str(trial["case_id"]), []).append(trial)
    result: dict[str, Any] = {}
    metric_names = (
        "elapsed_seconds",
        "agent_rounds",
        "ttp_submissions",
        "input_tokens",
        "output_tokens",
        "total_cost",
        "generation_duration_seconds",
    )
    for case_id, items in by_case.items():
        metrics: dict[str, dict[str, float]] = {}
        for name in metric_names:
            values = [
                float(item["metrics"].get(name, 0.0) or 0.0)
                for item in items
            ]
            metrics[name] = {
                "mean": sum(values) / len(values),
                "p50": _percentile(values, 0.50),
                "p95": _percentile(values, 0.95),
            }
        result[case_id] = {
            "trial_count": len(items),
            "strict_pass_count": sum(item["strict_pass"] is True for item in items),
            "strict_pass_rate": (
                sum(item["strict_pass"] is True for item in items) / len(items)
            ),
            "metrics": metrics,
        }
    return result


def _build_local_summary(
    trials: Sequence[Mapping[str, Any]],
    *,
    evaluation_id: str,
    evaluation_url: str,
    config_fingerprint: str,
    telemetry_complete: bool,
) -> dict[str, Any]:
    all_passed = all(trial.get("strict_pass") is True for trial in trials)
    return {
        "status": (
            "telemetry_incomplete"
            if not telemetry_complete
            else ("success" if all_passed else "failed")
        ),
        "evaluation": {
            "id": evaluation_id,
            "url": evaluation_url,
        },
        "config_fingerprint": config_fingerprint,
        "git": _git_facts(),
        "trial_count": len(trials),
        "strict_pass_count": sum(
            trial.get("strict_pass") is True for trial in trials
        ),
        "telemetry_complete": telemetry_complete,
        "trials": list(trials),
        "cases": _aggregate_trials(trials),
    }


async def _run_evaluation(args: argparse.Namespace) -> int:
    from lmnr import Instruments, Laminar, evaluate

    from cli_parser_agent import GenerationRequest, TtpGenerator
    from cli_parser_agent.ttp_generation.agent import PROMPT_VERSION

    manifest = load_evaluation_manifest(PROJECT_ROOT, MANIFEST_PATH)
    cases = select_cases(
        manifest,
        suite=args.suite,
        case_ids=args.case_ids or (),
    )
    settings, policy, config = _configuration()
    laminar_key = _validate_local_key(LMNR_PROJECT_API_KEY, "LMNR_PROJECT_API_KEY")
    config["prompt_version"] = PROMPT_VERSION
    config_fingerprint = _fingerprint(config)
    await asyncio.to_thread(_preflight_laminar)

    run_directory = _new_run_directory()
    group_name = _selection_group(manifest.version, cases)
    evaluation_name = args.name or (
        f"{PROMPT_VERSION} {MODEL_NAME} "
        f"{datetime.now(UTC).strftime('%Y-%m-%dT%H:%M:%SZ')}"
    )
    datapoints = _materialize_datapoints(
        cases,
        trials=args.trials,
        config_fingerprint=config_fingerprint,
    )
    generator = TtpGenerator(settings=settings, policy=policy)
    trial_facts: dict[str, dict[str, Any]] = {}
    facts_lock = threading.Lock()

    async def executor(data: Any) -> dict[str, Any]:
        if not isinstance(data, Mapping):
            return {"exception_type": "InvalidDatapoint"}
        case_id = str(data.get("case_id", ""))
        trial_index = int(data.get("trial_index", 0))
        key = str(data.get("trial_key", _trial_key(case_id, trial_index)))
        outputs = data.get("command_outputs")
        Laminar.set_trace_metadata(
            {
                "benchmark.case_id": case_id,
                "benchmark.trial_index": trial_index,
                "benchmark.config_fingerprint": config_fingerprint,
            },
        )
        try:
            request = GenerationRequest(command_outputs=outputs)
            result = await generator.generate(request)
            acceptance = await asyncio.to_thread(
                independent_acceptance,
                result,
                request.command_outputs,
                policy,
            )
            return {
                "case_id": case_id,
                "trial_index": trial_index,
                "trial_key": key,
                "generation_result": result.model_dump(mode="json"),
                "independent_acceptance": acceptance,
            }
        except asyncio.CancelledError:
            raise
        except Exception as error:
            return {
                "case_id": case_id,
                "trial_index": trial_index,
                "trial_key": key,
                "generation_result": None,
                "independent_acceptance": None,
                "exception_type": type(error).__name__,
            }

    def evaluator(output: Any, target: Any) -> dict[str, float]:
        scores = score_executor_output(output, target)
        if isinstance(output, Mapping):
            key = str(output.get("trial_key", ""))
            facts = safe_trial_facts(output, scores)
            result = output.get("generation_result")
            metadata = result.get("metadata") if isinstance(result, Mapping) else None
            facts["reported_trace_id"] = (
                metadata.get("laminar_trace_id")
                if isinstance(metadata, Mapping)
                else None
            )
            with facts_lock:
                trial_facts[key] = facts
        return scores

    print(
        f"running evaluation: cases={len(cases)} trials={args.trials} "
        f"datapoints={len(datapoints)} concurrency={args.concurrency}",
        flush=True,
    )
    evaluation_result = await evaluate(
        data=datapoints,
        executor=executor,
        evaluators={"deterministic": evaluator},
        name=evaluation_name,
        group_name=group_name,
        metadata={
            "runner_version": RUNNER_VERSION,
            "manifest_sha256": manifest.sha256,
            "config_fingerprint": config_fingerprint,
            "prompt_version": PROMPT_VERSION,
            "model_name": MODEL_NAME,
            "case_ids": [case.id for case in cases],
            "trials": args.trials,
        },
        concurrency_limit=args.concurrency,
        project_api_key=laminar_key,
        base_url=LMNR_BASE_URL,
        http_port=LMNR_HTTP_PORT,
        grpc_port=LMNR_GRPC_PORT,
        frontend_port=LMNR_FRONTEND_PORT,
        instruments={Instruments.OPENAI},
    )
    if evaluation_result is None:
        raise RunnerError("Laminar evaluation did not return a result")
    Laminar.flush()
    evaluation_id = str(evaluation_result["evaluation_id"])
    telemetry, telemetry_complete = await _collect_telemetry(
        evaluation_id,
        len(datapoints),
    )

    trials: list[dict[str, Any]] = []
    for case in cases:
        for trial_index in range(args.trials):
            key = _trial_key(case.id, trial_index)
            facts = trial_facts.get(
                key,
                {
                    "candidate_pass": False,
                    "failure_category": "runner",
                    "exception_type": "MissingEvaluatorResult",
                    "termination_reason": "missing_result",
                    "issue_codes": [],
                    "last_attempt_present": False,
                    "metrics": {},
                    "reported_trace_id": None,
                },
            )
            trace = telemetry.get(key, {})
            span_metrics = trace.get("spans", {})
            combined_metrics = {**facts["metrics"], **span_metrics}
            complete = trace.get("complete") is True
            trials.append(
                {
                    "case_id": case.id,
                    "trial_index": trial_index,
                    "strict_pass": facts["candidate_pass"] is True and complete,
                    "candidate_pass": facts["candidate_pass"],
                    "telemetry_complete": complete,
                    "failure_category": (
                        facts["failure_category"]
                        if complete
                        else "telemetry"
                    ),
                    "termination_reason": facts["termination_reason"],
                    "issue_codes": facts["issue_codes"],
                    "exception_type": facts["exception_type"],
                    "last_attempt_present": facts["last_attempt_present"],
                    "trace_id": trace.get("trace_id"),
                    "reported_trace_id": facts["reported_trace_id"],
                    "metrics": combined_metrics,
                },
            )

    all_passed = all(trial["strict_pass"] is True for trial in trials)
    summary = _build_local_summary(
        trials,
        evaluation_id=evaluation_id,
        evaluation_url=evaluation_result["url"],
        config_fingerprint=config_fingerprint,
        telemetry_complete=telemetry_complete,
    )
    result_path = run_directory / "summary.json"
    _write_json(result_path, summary)
    print(f"status: {summary['status']}")
    print(f"evaluation_id: {evaluation_id}")
    print(f"evaluation_url: {evaluation_result['url']}")
    print(f"summary_json: {result_path}")
    if not telemetry_complete:
        return 2
    return 0 if all_passed else 1


def _command_run(args: argparse.Namespace) -> int:
    from lmnr import Laminar

    try:
        return asyncio.run(_run_evaluation(args))
    finally:
        if Laminar.is_initialized():
            try:
                Laminar.flush()
            except Exception as error:
                print(
                    f"warning: Laminar flush failed ({type(error).__name__})",
                    file=sys.stderr,
                )


def main(argv: Sequence[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    try:
        if args.command == "list":
            return _command_list()
        if args.command == "preflight":
            return _command_preflight()
        return _command_run(args)
    except (HarnessError, RunnerError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("cancelled", file=sys.stderr)
        return 130
    except Exception as error:
        print(
            f"error: evaluation runner stopped unexpectedly ({type(error).__name__})",
            file=sys.stderr,
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
