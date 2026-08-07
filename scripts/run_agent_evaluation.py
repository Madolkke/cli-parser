"""Run repository-backed black-box Agent evaluations through Laminar."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import math
import os
import re
import socket
import subprocess
import sys
import threading
import time
import urllib.request
import uuid
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from cli_parser_agent.config import (  # noqa: E402
    tls_ssl_context,
    tls_verification_enabled,
)
from cli_parser_agent.evaluation import (  # noqa: E402
    EvaluationCase,
    HarnessError,
    aggregate_trial_scores,
    attach_human_reviews,
    independent_acceptance,
    issue_taxonomy,
    load_evaluation_manifest,
    project_candidate_trajectory,
    project_human_reviews,
    safe_trial_facts,
    score_executor_output,
    score_executor_output_details,
    select_cases,
    summarize_span_metrics,
)

RUNNER_VERSION = 1
MANIFEST_PATH = PROJECT_ROOT / "evals" / "ttp_generation" / "manifest.json"


class RunnerError(RuntimeError):
    """A bounded evaluation runner error that is safe to display."""


@dataclass(frozen=True)
class EvaluationRuntimeConfig:
    """Environment-derived settings unique to the Laminar evaluation runner."""

    laminar_project_api_key: str
    laminar_base_url: str
    laminar_http_port: int
    laminar_grpc_port: int
    laminar_frontend_port: int
    artifact_root: Path
    telemetry_wait_seconds: float


def _validate_local_key(value: str, name: str) -> str:
    if not value.strip():
        raise RunnerError(f"{name} must not be empty")
    if len(value) < 8 or any(character.isspace() for character in value):
        raise RunnerError(f"{name} is not a valid local key value")
    return value


def _required_environment_value(source: Mapping[str, str], name: str) -> str:
    value = source.get(name, "").strip()
    if not value:
        raise RunnerError(f"{name} must be set for a live evaluation")
    return value


def _environment_port(source: Mapping[str, str], name: str) -> int:
    value = _required_environment_value(source, name)
    if not value.isascii() or not value.isdecimal():
        raise RunnerError(f"{name} must be a decimal integer from 1 to 65535")
    port = int(value, 10)
    if not 1 <= port <= 65_535:
        raise RunnerError(f"{name} must be a decimal integer from 1 to 65535")
    return port


def _environment_positive_float(
    source: Mapping[str, str],
    name: str,
    *,
    default: float,
) -> float:
    value = source.get(name, "").strip()
    if not value:
        return default
    try:
        parsed = float(value)
    except ValueError as error:
        raise RunnerError(f"{name} must be a positive number") from error
    if not math.isfinite(parsed) or parsed <= 0:
        raise RunnerError(f"{name} must be a positive number")
    return parsed


def _runtime_configuration(
    source: Mapping[str, str],
) -> EvaluationRuntimeConfig:
    return EvaluationRuntimeConfig(
        laminar_project_api_key=_validate_local_key(
            _required_environment_value(source, "LMNR_PROJECT_API_KEY"),
            "LMNR_PROJECT_API_KEY",
        ),
        laminar_base_url=_required_environment_value(source, "LMNR_BASE_URL"),
        laminar_http_port=_environment_port(source, "LMNR_HTTP_PORT"),
        laminar_grpc_port=_environment_port(source, "LMNR_GRPC_PORT"),
        laminar_frontend_port=_environment_port(source, "LMNR_FRONTEND_PORT"),
        artifact_root=Path(
            source.get(
                "CLI_PARSER_EVAL_ARTIFACT_ROOT",
                str(PROJECT_ROOT / ".artifacts" / "agent-evals"),
            ),
        ),
        telemetry_wait_seconds=_environment_positive_float(
            source,
            "CLI_PARSER_EVAL_TELEMETRY_WAIT_SECONDS",
            default=60.0,
        ),
    )


def _configuration(
    environ: Mapping[str, str] | None = None,
) -> tuple[Any, Any, EvaluationRuntimeConfig, dict[str, Any]]:
    from cli_parser_agent import GenerationPolicy, TtpGeneratorSettings

    source = os.environ if environ is None else environ
    try:
        settings = TtpGeneratorSettings.from_env(source)
        policy = GenerationPolicy.from_env(source)
    except (TypeError, ValueError) as error:
        raise RunnerError(
            "model or generation configuration is invalid "
            f"({type(error).__name__})"
        ) from None
    runtime = _runtime_configuration(source)
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
            "verify_tls": settings.verify_tls,
            "thinking_enable": settings.thinking_enable,
            "reasoning_effort": settings.reasoning_effort,
        },
        "policy": policy.model_dump(mode="json"),
        "laminar": {
            "base_url": runtime.laminar_base_url,
            "http_port": runtime.laminar_http_port,
            "grpc_port": runtime.laminar_grpc_port,
            "frontend_port": runtime.laminar_frontend_port,
        },
    }
    return settings, policy, runtime, snapshot


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
    review = commands.add_parser(
        "review",
        help="write one bounded HumanEvaluator label to an existing Trace",
    )
    review.add_argument("--trace-id", required=True)
    review.add_argument(
        "--phase",
        choices=sorted(_REVIEW_PHASES),
        default="ttp",
    )
    review.add_argument("--submission", type=_positive_int, required=True)
    review.add_argument("--label", choices=sorted(_REVIEW_LABELS), required=True)
    review.add_argument(
        "--dimension",
        action="append",
        default=[],
        metavar="NAME=VALUE",
        help="bounded quality dimension label; may be repeated",
    )
    review.add_argument("--issue-code", action="append", default=[])
    return parser


def _parse_review_dimensions(values: Sequence[str]) -> dict[str, str]:
    dimensions: dict[str, str] = {}
    for value in values:
        if "=" not in value:
            raise RunnerError("--dimension must use NAME=VALUE")
        name, label = value.split("=", 1)
        if name in dimensions:
            raise RunnerError("--dimension must not repeat a name")
        dimensions[name] = label
    return dimensions


def _command_review(args: argparse.Namespace) -> int:
    runtime = _runtime_configuration(os.environ)
    record_human_review(
        runtime,
        trace_id=args.trace_id,
        phase=args.phase,
        submission_index=args.submission,
        label=args.label,
        dimensions=_parse_review_dimensions(args.dimension),
        issue_codes=args.issue_code,
    )
    print("review recorded")
    return 0


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


def _sql_endpoint(runtime: EvaluationRuntimeConfig) -> str:
    return (
        f"{runtime.laminar_base_url.rstrip('/')}:{runtime.laminar_http_port}"
        "/v1/sql/query"
    )


def _sql_query(
    runtime: EvaluationRuntimeConfig,
    query: str,
    parameters: Mapping[str, Any],
) -> list[dict[str, Any]]:
    payload = json.dumps(
        {"query": query, "parameters": dict(parameters)},
        separators=(",", ":"),
    ).encode("utf-8")
    request = urllib.request.Request(
        _sql_endpoint(runtime),
        data=payload,
        method="POST",
        headers={
            "Authorization": f"Bearer {runtime.laminar_project_api_key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(
            request,
            timeout=20.0,
            context=tls_ssl_context(verify_tls=tls_verification_enabled()),
        ) as response:
            value = json.loads(response.read().decode("utf-8", errors="strict"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise RunnerError(
            f"Laminar SQL request failed ({type(error).__name__})",
        ) from None
    rows = value.get("data") if isinstance(value, dict) else None
    if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
        raise RunnerError("Laminar SQL response has an unexpected shape")
    return rows


def _preflight_laminar(runtime: EvaluationRuntimeConfig) -> None:
    hostname = urlsplit(runtime.laminar_base_url).hostname
    if hostname is None:
        raise RunnerError("LMNR_BASE_URL must be an absolute HTTP(S) URL")
    try:
        with socket.create_connection(
            (hostname, runtime.laminar_grpc_port),
            timeout=5.0,
        ):
            pass
    except OSError as error:
        raise RunnerError(
            f"Laminar gRPC endpoint is unavailable ({type(error).__name__})",
        ) from None
    rows = _sql_query(runtime, "SELECT 1 AS ok", {})
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


def _new_run_directory(artifact_root: Path) -> Path:
    artifact_root.mkdir(parents=True, exist_ok=True)
    stem = datetime.now(UTC).strftime("%Y%m%dT%H%M%S.%fZ")
    for suffix in range(1000):
        candidate = artifact_root / (stem if suffix == 0 else f"{stem}-{suffix:02d}")
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


def _telemetry_for_trace(
    runtime: EvaluationRuntimeConfig,
    trace_id: str,
) -> dict[str, Any]:
    rows = _sql_query(
        runtime,
        """
        SELECT
            countIf(name = 'evaluation') AS evaluation_span_count,
            countIf(name = 'executor') AS executor_span_count,
            countIf(name = 'ttp.generate') AS generation_span_count,
            countIf(name = 'schema.phase') AS schema_phase_count,
            countIf(name = 'ttp.phase') AS ttp_phase_count,
            countIf(name = 'context.fit') AS context_fit_span_count,
            countIf(name = 'agent.round') AS agent_round_span_count,
            countIf(name = 'generation.deadline_cleanup')
                AS deadline_cleanup_span_count,
            countIf(name = 'final.acceptance') AS final_acceptance_span_count,
            countIf(span_type = 'LLM') AS llm_call_count,
            countIf(span_type = 'TOOL') AS tool_span_count,
            sumIf(input_tokens, span_type = 'LLM') AS input_tokens,
            sumIf(output_tokens, span_type = 'LLM') AS output_tokens,
            sumIf(total_cost, span_type = 'LLM') AS total_cost,
            sumIf(duration, span_type = 'LLM') AS llm_duration_seconds,
            sumIf(duration, span_type = 'TOOL') AS tool_duration_seconds,
            maxIf(duration, name = 'ttp.generate') AS generation_duration_seconds,
            sumIf(duration, name = 'schema.phase') AS schema_duration_seconds,
            sumIf(duration, name = 'ttp.phase') AS ttp_duration_seconds,
            sumIf(duration, name = 'context.fit') AS context_fit_duration_seconds,
            sumIf(duration, name = 'agent.round') AS agent_round_duration_seconds,
            sumIf(duration, name = 'generation.deadline_cleanup')
                AS deadline_cleanup_duration_seconds,
            sumIf(duration, name = 'final.acceptance')
                AS final_acceptance_duration_seconds
        FROM spans
        WHERE trace_id = {trace_id:UUID}
          AND start_time > now() - INTERVAL 1 DAY
        """,
        {"trace_id": trace_id},
    )
    if len(rows) != 1:
        return {}
    result = dict(rows[0])
    span_rows = _sql_query(
        runtime,
        """
        SELECT name, span_type, start_time, duration, input_tokens
        FROM spans
        WHERE trace_id = {trace_id:UUID}
          AND start_time > now() - INTERVAL 1 DAY
        ORDER BY start_time
        """,
        {"trace_id": trace_id},
    )
    result.update(summarize_span_metrics(span_rows))
    # Root attributes carry the observed phase funnel.  Keep only booleans and
    # enums in the local projection; the raw root output remains Laminar-only.
    root_rows = _sql_query(
        runtime,
        """
        SELECT attributes, output
        FROM spans
        WHERE trace_id = {trace_id:UUID}
          AND name = 'ttp.generate'
          AND start_time > now() - INTERVAL 1 DAY
        ORDER BY start_time DESC
        LIMIT 1
        """,
        {"trace_id": trace_id},
    )
    root = root_rows[0] if root_rows else {}
    root_attributes = _parse_json_object(root.get("attributes"))
    root_output = _parse_json_object(root.get("output"))
    root_metadata = root_output.get("metadata")
    if not isinstance(root_metadata, Mapping):
        root_metadata = {}
    for name in (
        "schema_frozen",
        "entered_ttp",
        "valid_ttp_candidate",
        "finish_called",
    ):
        value = root_attributes.get(name, root_metadata.get(name))
        if isinstance(value, bool):
            result[name] = value
    result["reported_trace_id"] = root_metadata.get("laminar_trace_id")
    return result


def _candidate_spans_for_trace(
    runtime: EvaluationRuntimeConfig,
    trace_id: str,
) -> list[dict[str, Any]]:
    """Fetch candidate TOOL outputs for post-run diagnostics only."""

    rows = _sql_query(
        runtime,
        """
        SELECT name, span_type, start_time, duration, input, output, attributes
        FROM spans
        WHERE trace_id = {trace_id:UUID}
          AND name IN (
              'submit_result_schema',
              'schema.submit_result_schema',
              'submit_ttp_template',
              'ttp.submit_ttp_template',
              'finish_generation',
              'generation.finish_generation'
          )
          AND start_time > now() - INTERVAL 1 DAY
        ORDER BY start_time
        """,
        {"trace_id": trace_id},
    )
    return [row for row in rows if isinstance(row, Mapping)]


def _human_review_spans_for_trace(
    runtime: EvaluationRuntimeConfig,
    trace_id: str,
) -> list[dict[str, Any]]:
    """Fetch evaluation-only HumanEvaluator spans for one Trace."""

    rows = _sql_query(
        runtime,
        """
        SELECT name, start_time, input, output, attributes
        FROM spans
        WHERE trace_id = {trace_id:UUID}
          AND name = 'template.human_review'
          AND start_time > now() - INTERVAL 1 DAY
        ORDER BY start_time
        """,
        {"trace_id": trace_id},
    )
    return [row for row in rows if isinstance(row, Mapping)]


_REVIEW_LABELS = frozenset({"reasonable", "repairable", "unreasonable"})
_REVIEW_PHASES = frozenset({"schema", "ttp"})
_REVIEW_DIMENSION_RE = re.compile(r"^[a-z][a-z0-9_]{0,31}$")
_REVIEW_VALUE_RE = re.compile(r"^[a-z][a-z0-9_-]{0,31}$")
_ISSUE_CODE_RE = re.compile(r"^[a-z][a-z0-9_]*(?:\.[a-z0-9_]+)*$")


def record_human_review(
    runtime: EvaluationRuntimeConfig,
    *,
    trace_id: str,
    phase: str = "ttp",
    submission_index: int,
    label: str,
    dimensions: Mapping[str, str] | None = None,
    issue_codes: Sequence[str] = (),
) -> None:
    """Write a bounded review label into Laminar without copying a candidate.

    This helper is intentionally evaluation-only.  Reviewers identify the
    candidate by Trace ID and submission index; the template and capture remain
    in the existing Laminar TOOL span and are never placed in the review span's
    local input/output or in the local evaluation summary.
    """

    try:
        trace_uuid = uuid.UUID(trace_id)
    except (ValueError, AttributeError, TypeError) as error:
        raise RunnerError("review trace_id must be a UUID") from error
    if label not in _REVIEW_LABELS:
        raise RunnerError("review label is unsupported")
    if phase not in _REVIEW_PHASES:
        raise RunnerError("review phase is unsupported")
    if not isinstance(submission_index, int) or isinstance(submission_index, bool):
        raise RunnerError("review submission_index must be an integer")
    if submission_index < 1:
        raise RunnerError("review submission_index must be positive")
    safe_dimensions: dict[str, str] = {}
    for key, value in (dimensions or {}).items():
        if (
            not isinstance(key, str)
            or not _REVIEW_DIMENSION_RE.fullmatch(key)
            or not isinstance(value, str)
            or not _REVIEW_VALUE_RE.fullmatch(value)
        ):
            raise RunnerError("review dimensions contain an unsupported value")
        safe_dimensions[key] = value
    safe_issue_codes = [
        code
        for code in issue_codes
        if isinstance(code, str) and _ISSUE_CODE_RE.fullmatch(code)
    ][:32]

    from lmnr import Laminar, LaminarSpanContext
    from lmnr.opentelemetry_lib.tracing.attributes import (
        HUMAN_EVALUATOR_OPTIONS,
        SPAN_TYPE,
    )

    if not Laminar.is_initialized():
        Laminar.initialize(
            project_api_key=runtime.laminar_project_api_key,
            base_url=runtime.laminar_base_url,
            http_port=runtime.laminar_http_port,
            grpc_port=runtime.laminar_grpc_port,
        )
    # Laminar requires a parent span ID as well as a trace ID.  A synthetic
    # remote parent keeps the annotation in the original Trace without adding
    # any candidate content or depending on an SDK-specific span lookup API.
    parent = LaminarSpanContext(
        trace_id=trace_uuid,
        span_id=uuid.UUID(int=uuid.uuid4().int & ((1 << 64) - 1)),
        is_remote=True,
    )
    with Laminar.start_as_current_span(
        "template.human_review",
        input={"phase": phase, "submission_index": submission_index},
        parent_span_context=parent,
        tags=["agent-evaluation", "human-review"],
        metadata={
            "review_label": label,
            "review_phase": phase,
            "review_submission_index": submission_index,
            "review_dimensions": safe_dimensions,
            "review_issue_codes": safe_issue_codes,
        },
    ) as span:
        span.set_attribute(SPAN_TYPE, "HUMAN_EVALUATOR")
        span.set_attribute(
            HUMAN_EVALUATOR_OPTIONS,
            json.dumps(
                [
                    {"label": "reasonable", "value": 1.0},
                    {"label": "repairable", "value": 0.5},
                    {"label": "unreasonable", "value": 0.0},
                ],
            ),
        )
        Laminar.set_span_output(
            {
                "phase": phase,
                "submission_index": submission_index,
                "label": label,
                "dimensions": safe_dimensions,
                "issue_codes": safe_issue_codes,
            },
        )
    Laminar.flush()


def _telemetry_complete(telemetry: Mapping[str, Any]) -> bool:
    base_complete = all(
        int(telemetry.get(name, 0) or 0) >= 1
        for name in (
            "evaluation_span_count",
            "executor_span_count",
            "generation_span_count",
            "schema_phase_count",
            "llm_call_count",
        )
    )
    if not base_complete:
        return False
    if telemetry.get("entered_ttp") is True and int(
        telemetry.get("ttp_phase_count", 0) or 0,
    ) < 1:
        return False
    return not (
        telemetry.get("finish_called") is True
        and int(telemetry.get("final_acceptance_span_count", 0) or 0) < 1
    )


def _strict_pass(candidate_pass: Any) -> bool:
    """Return deterministic correctness without consulting telemetry state."""

    return candidate_pass is True


async def _collect_telemetry(
    runtime: EvaluationRuntimeConfig,
    evaluation_id: str,
    expected_count: int,
) -> tuple[dict[str, dict[str, Any]], bool]:
    deadline = time.monotonic() + runtime.telemetry_wait_seconds
    while True:
        rows = await asyncio.to_thread(
            _sql_query,
            runtime,
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
                    runtime,
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
        "strict_pass",
        "candidate_pass",
        "generation_success",
        "independent_acceptance",
        "records_exact_match",
        "schema_contract_match",
        "input_exact_match_rate",
        "leaf_f1",
        "schema_path_f1",
        "finish_called",
        "first_ttp_passed",
        "elapsed_seconds",
        "agent_rounds",
        "ttp_submissions",
        "input_tokens",
        "output_tokens",
        "total_cost",
        "generation_duration_seconds",
        "schema_duration_seconds",
        "ttp_duration_seconds",
        "context_fit_duration_seconds",
        "agent_round_duration_seconds",
        "deadline_cleanup_duration_seconds",
        "final_acceptance_duration_seconds",
        "explained_duration_seconds",
        "unexplained_duration_seconds",
        "explained_duration_ratio",
        "unexplained_duration_ratio",
        "first_input_tokens",
        "last_input_tokens",
        "max_input_tokens",
        "growth_slope_tokens_per_call",
    )
    for case_id, items in by_case.items():
        issue_codes = Counter(
            code
            for item in items
            for code in item.get("issue_codes", [])
            if isinstance(code, str)
        )
        candidate_trajectories = [
            item.get("candidate_trajectory")
            for item in items
            if isinstance(item.get("candidate_trajectory"), Mapping)
        ]
        candidate_summary = {
            "trial_count": len(candidate_trajectories),
            "submission_count": sum(
                int(item.get("submission_count", 0) or 0)
                for item in candidate_trajectories
            ),
            "accepted_count": sum(
                int(item.get("accepted_count", 0) or 0)
                for item in candidate_trajectories
            ),
            "schema_submission_count": sum(
                int(item.get("schema_submission_count", 0) or 0)
                for item in candidate_trajectories
            ),
            "schema_accepted_count": sum(
                int(item.get("schema_accepted_count", 0) or 0)
                for item in candidate_trajectories
            ),
            "finish_after_first_accepted_count": sum(
                bool(item.get("finish_after_first_accepted"))
                for item in candidate_trajectories
            ),
        }
        review_counts: Counter[str] = Counter()
        review_count = 0
        reviewed_submission_count = 0
        for trajectory in candidate_trajectories:
            review = trajectory.get("human_review")
            if not isinstance(review, Mapping):
                continue
            review_count += int(review.get("review_count", 0) or 0)
            reviewed_submission_count += int(
                review.get("reviewed_submission_count", 0) or 0,
            )
            raw_labels = review.get("label_counts")
            if isinstance(raw_labels, Mapping):
                review_counts.update(
                    {
                        str(label): int(count)
                        for label, count in raw_labels.items()
                        if isinstance(label, str)
                        and isinstance(count, int | float)
                        and count >= 0
                    },
                )
        candidate_summary["human_review_count"] = review_count
        candidate_summary["human_reviewed_submission_count"] = (
            reviewed_submission_count
        )
        candidate_summary["human_review_label_counts"] = dict(
            sorted(review_counts.items()),
        )
        strict_pass_count = sum(
            item.get("strict_pass") is True for item in items
        )
        aggregate = aggregate_trial_scores(items, metric_names=metric_names)
        result[case_id] = {
            "trial_count": len(items),
            "strict_pass_count": strict_pass_count,
            "strict_pass_rate": (
                strict_pass_count / len(items)
            ),
            "strict_pass_wilson_95": dict(
                aggregate.get("binary", {})
                .get("strict_pass", {})
                .get("wilson_95", {})
            ),
            "metrics": aggregate.get("metrics", {}),
            "binary": aggregate.get("binary", {}),
            "issue_codes": dict(sorted(issue_codes.items())),
            "candidate_quality": candidate_summary,
        }
    if trials:
        aggregate = aggregate_trial_scores(trials, metric_names=metric_names)
        total_inputs = sum(
            float(item.get("metrics", {}).get("input_count", 0.0) or 0.0)
            for item in trials
        )
        exact_inputs = sum(
            float(
                item.get("metrics", {}).get("input_exact_match_count", 0.0)
                or 0.0
            )
            for item in trials
        )
        case_rates = [
            float(item.get("metrics", {}).get("input_exact_match_rate", 0.0) or 0.0)
            for item in trials
            if isinstance(item.get("metrics"), Mapping)
        ]
        micro_rate = exact_inputs / total_inputs if total_inputs else 0.0
        macro_rate = sum(case_rates) / len(case_rates) if case_rates else 0.0
        aggregate["input_exact_match"] = {
            "micro": {
                "successes": exact_inputs,
                "observations": total_inputs,
                "rate": micro_rate,
            },
            "macro": {"case_count": len(case_rates), "rate": macro_rate},
        }
        result["__overall__"] = aggregate
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
    from lmnr import HumanEvaluator, Instruments, Laminar, evaluate

    from cli_parser_agent import GenerationRequest, TtpGenerator
    from cli_parser_agent.ttp_generation.agent import PROMPT_VERSION

    manifest = load_evaluation_manifest(PROJECT_ROOT, MANIFEST_PATH)
    cases = select_cases(
        manifest,
        suite=args.suite,
        case_ids=args.case_ids or (),
    )
    settings, policy, runtime, config = _configuration()
    laminar_key = runtime.laminar_project_api_key
    config["prompt_version"] = PROMPT_VERSION
    config_fingerprint = _fingerprint(config)
    await asyncio.to_thread(_preflight_laminar, runtime)

    run_directory = _new_run_directory(runtime.artifact_root)
    group_name = _selection_group(manifest.version, cases)
    evaluation_name = args.name or (
        f"{PROMPT_VERSION} {settings.model_name} "
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
            details = score_executor_output_details(output, target)
            facts["input_diagnostics"] = details.get("inputs", [])
            facts["extra_actual_input_count"] = details.get(
                "extra_actual_input_count",
                0,
            )
            facts["issue_taxonomy"] = details.get(
                "issue_taxonomy",
                issue_taxonomy(()),
            )
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
        evaluators={
            "deterministic": evaluator,
            "template_review": HumanEvaluator(
                options=[
                    {"label": "reasonable", "value": 1.0},
                    {"label": "repairable", "value": 0.5},
                    {"label": "unreasonable", "value": 0.0},
                ],
            ),
        },
        name=evaluation_name,
        group_name=group_name,
        metadata={
            "runner_version": RUNNER_VERSION,
            "manifest_sha256": manifest.sha256,
            "config_fingerprint": config_fingerprint,
            "prompt_version": PROMPT_VERSION,
            "model_name": settings.model_name,
            "case_ids": [case.id for case in cases],
            "trials": args.trials,
        },
        concurrency_limit=args.concurrency,
        project_api_key=laminar_key,
        base_url=runtime.laminar_base_url,
        http_port=runtime.laminar_http_port,
        grpc_port=runtime.laminar_grpc_port,
        frontend_port=runtime.laminar_frontend_port,
        instruments={Instruments.OPENAI},
    )
    if evaluation_result is None:
        raise RunnerError("Laminar evaluation did not return a result")
    Laminar.flush()
    evaluation_id = str(evaluation_result["evaluation_id"])
    telemetry, telemetry_complete = await _collect_telemetry(
        runtime,
        evaluation_id,
        len(datapoints),
    )

    # Candidate outputs are queried only after generation and are projected to
    # bounded structural facts before entering the local summary.  The raw
    # template/capture remains available solely in Laminar for reviewer agents.
    case_by_key = {
        _trial_key(case.id, trial_index): case
        for case in cases
        for trial_index in range(args.trials)
    }
    for key, trace in telemetry.items():
        trace_id = trace.get("trace_id")
        case = case_by_key.get(key)
        if not isinstance(trace_id, str) or case is None:
            continue
        try:
            candidate_rows = await asyncio.to_thread(
                _candidate_spans_for_trace,
                runtime,
                trace_id,
            )
            trace["candidate_trajectory"] = project_candidate_trajectory(
                candidate_rows,
                expected_records=case.target.records,
            )
            review_rows = await asyncio.to_thread(
                _human_review_spans_for_trace,
                runtime,
                trace_id,
            )
            review_projection = project_human_reviews(review_rows)
            trace["candidate_trajectory"] = attach_human_reviews(
                trace["candidate_trajectory"],
                review_projection,
            )
        except RunnerError:
            # Candidate diagnostics are additive.  A SQL projection problem
            # must not erase the deterministic evaluator result.
            trace["candidate_trajectory"] = {
                "submission_count": 0,
                "accepted_count": 0,
                "candidate_available_count": 0,
                "first_accepted_submission": None,
                "last_accepted_submission": None,
                "accepted_indices": [],
                "finish_called": False,
                "finish_after_first_accepted": False,
                "issue_domains": {},
                "candidates": [],
                "query_error": True,
            }

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
                    "fault_domain": None,
                    "issue_codes": [],
                    "last_attempt_present": False,
                    "metrics": {},
                    "reported_trace_id": None,
                },
            )
            trace = telemetry.get(key, {})
            span_metrics = trace.get("spans", {})
            combined_metrics = {**facts["metrics"], **span_metrics}
            token_growth = trace.get("token_growth", {})
            if isinstance(token_growth, Mapping):
                combined_metrics.update(
                    {
                        key: value
                        for key, value in token_growth.items()
                        if isinstance(value, int | float)
                        and not isinstance(value, bool)
                    },
                )
            complete = trace.get("complete") is True
            reported_trace_id = facts["reported_trace_id"] or span_metrics.get(
                "reported_trace_id",
            )
            trace_id_consistent = not reported_trace_id or (
                reported_trace_id == trace.get("trace_id")
            )
            if not trace_id_consistent:
                complete = False
                combined_metrics["trace_id_consistent"] = 0.0
            else:
                combined_metrics["trace_id_consistent"] = 1.0
            trials.append(
                {
                    "case_id": case.id,
                    "trial_index": trial_index,
                    "strict_pass": _strict_pass(facts["candidate_pass"]),
                    "candidate_pass": facts["candidate_pass"],
                    "telemetry_complete": complete,
                    "failure_category": (
                        facts["failure_category"]
                        if complete
                        else "telemetry"
                    ),
                    "termination_reason": facts["termination_reason"],
                    "fault_domain": facts["fault_domain"],
                    "issue_codes": facts["issue_codes"],
                    "exception_type": facts["exception_type"],
                    "last_attempt_present": facts["last_attempt_present"],
                    "trace_id": trace.get("trace_id"),
                    "reported_trace_id": reported_trace_id,
                    "trace_id_consistent": trace_id_consistent,
                    "metrics": combined_metrics,
                    "input_diagnostics": facts.get("input_diagnostics", []),
                    "issue_taxonomy": facts.get(
                        "issue_taxonomy",
                        issue_taxonomy(()),
                    ),
                    "segment_stats": trace.get("segment_stats", {}),
                    "token_growth": trace.get("token_growth", {}),
                    "duration_coverage": {
                        key: trace.get(key, 0.0)
                        for key in (
                            "explained_duration_seconds",
                            "unexplained_duration_seconds",
                            "explained_duration_ratio",
                            "unexplained_duration_ratio",
                        )
                    },
                    "candidate_trajectory": trace.get(
                        "candidate_trajectory",
                        {
                            "submission_count": 0,
                            "accepted_count": 0,
                            "candidate_available_count": 0,
                            "finish_after_first_accepted": False,
                        },
                    ),
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
        if args.command == "review":
            return _command_review(args)
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
