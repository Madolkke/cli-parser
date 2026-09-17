"""Run TOML-registered test sets offline or against the TTP agent."""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import re
import subprocess
import sys
import time
from collections.abc import Mapping, Sequence
from copy import deepcopy
from datetime import UTC, datetime
from itertools import combinations
from pathlib import Path
from typing import Any

SCRIPT_DIRECTORY = Path(__file__).resolve().parent
PROJECT_ROOT = SCRIPT_DIRECTORY.parent
if str(SCRIPT_DIRECTORY) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIRECTORY))

import _agent_run_support as _run_support  # noqa: E402
from agentscope import event as agent_events  # noqa: E402

from cli_parser_agent import (  # noqa: E402
    GenerationPolicy,
    GenerationRequest,
    TemplateRequest,
    TtpGenerator,
    TtpGeneratorSettings,
)
from cli_parser_agent.evaluation import (  # noqa: E402
    SCHEMA_METRICS_VERSION,
    DatasetPreflightReport,
    HarnessError,
    aggregate_trial_scores,
    dataset_input_scope_metadata,
    independent_acceptance,
    load_dataset_registry,
    preflight_dataset_registry,
    project_execution_facts,
    safe_trial_facts,
    schema_consistency_overview,
    schema_contract_consistency,
    schema_proposal_metrics,
    schema_repeat_consistency,
    score_ttp_template_output,
    select_dataset_entries,
    summarize_schema_review,
    wilson_interval,
)
from cli_parser_agent.evaluation_parse_review import (  # noqa: E402
    summarize_parse_review,
)
from cli_parser_agent.ttp_generation.agent import PROMPT_VERSION  # noqa: E402

RUNNER_VERSION = 5
BASELINE_VERSION = 1
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


def _dataset_id(value: str) -> int:
    return _positive_int(value)


def _tolerance(value: str) -> int:
    try:
        parsed = int(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("must be an integer") from error
    if parsed < 0:
        raise argparse.ArgumentTypeError("must be non-negative")
    return parsed


def _add_selection_arguments(command: argparse.ArgumentParser) -> None:
    command.add_argument("--dataset", dest="dataset_names", action="append")
    command.add_argument(
        "--dataset-id", dest="dataset_ids", type=_dataset_id, action="append"
    )
    command.add_argument("--tag", dest="tags", action="append")


def _add_input_scope_argument(command: argparse.ArgumentParser) -> None:
    command.add_argument(
        "--input-scope",
        choices=("default", "full"),
        default="default",
        help="evaluate only the registered default input or every input",
    )


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run canonical four-part CLI parser test sets.",
    )
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("list", "preflight"):
        command = commands.add_parser(name)
        command.add_argument("--registry", type=Path, required=True)
        _add_selection_arguments(command)
        _add_input_scope_argument(command)
    run = commands.add_parser("run")
    run.add_argument("--registry", type=Path, required=True)
    run.add_argument(
        "--mode",
        choices=("baseline", "ttp-only", "schema-only", "end-to-end"),
        required=True,
    )
    _add_selection_arguments(run)
    _add_input_scope_argument(run)
    run.add_argument("--trials", type=_trials, default=1)
    run.add_argument("--concurrency", type=_concurrency, default=1)
    run.add_argument(
        "--baseline",
        type=Path,
        default=None,
        help="compare per-case candidate_pass against this frozen baseline",
    )
    run.add_argument(
        "--regression-tolerance",
        type=_tolerance,
        default=0,
        help="per-case candidate_pass drop tolerated before reporting regressed",
    )
    run.add_argument(
        "--write-baseline",
        type=Path,
        default=None,
        help="write this run's numeric projection to PATH for use as a baseline",
    )
    run.add_argument(
        "--trace-rounds",
        action="store_true",
        help="record safe per-round facts to trial-NN.rounds.jsonl for loop diagnosis",
    )
    review = commands.add_parser("schema-review")
    review.add_argument("--run-directory", type=Path, required=True)
    review.add_argument("--review-file", type=Path, required=True)
    parse_review = commands.add_parser("parse-review")
    parse_review.add_argument("--run-directory", type=Path, required=True)
    parse_review.add_argument("--review-file", type=Path, required=True)
    parse_review.add_argument("--schema-review-file", type=Path, required=True)
    return parser


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
            "model_max_retries": settings.model_max_retries,
            "verify_tls": settings.verify_tls,
            "thinking_enable": settings.thinking_enable,
            "reasoning_effort": settings.reasoning_effort,
            "extra_body_configured": settings.extra_body is not None,
        },
        "policy": policy.model_dump(mode="json"),
        "prompt": {"version": PROMPT_VERSION},
    }
    return settings, policy, artifact_root, configuration


def _case_metadata(case: Any, input_scope: str) -> dict[str, Any]:
    original_input_indices = (
        case.original_input_indices
        if case.original_input_indices
        else tuple(range(len(case.inputs)))
    )
    return {
        "id": case.id,
        "command": case.command,
        "path": case.path,
        "suites": list(case.suites),
        "tags": list(case.tags),
        "input_scope": input_scope,
        "selected_input_count": len(case.inputs),
        "selected_input": case.inputs[0].path if len(case.inputs) == 1 else None,
        "selected_inputs": [
            {
                "input_index": original_index,
                "display_number": original_index + 1,
                "path": item.path,
            }
            for original_index, item in zip(
                original_input_indices,
                case.inputs,
                strict=True,
            )
        ],
    }


def _selected_reports(
    registry: Any,
    entries: Sequence[Any],
    input_scope: str,
) -> tuple[DatasetPreflightReport, ...]:
    reports = preflight_dataset_registry(registry, input_scope=input_scope)
    selected_names = {entry.name for entry in entries}
    return tuple(report for report in reports if report.dataset.name in selected_names)


def _stage_counts(reports: Sequence[DatasetPreflightReport]) -> dict[str, int]:
    return {
        "inputs_only_count": sum(
            report.dataset.stage == "inputs-only" for report in reports
        ),
        "template_count": sum(report.dataset.stage == "template" for report in reports),
        "complete_count": sum(report.dataset.stage == "complete" for report in reports),
        "pending_count": sum(report.status == "pending" for report in reports),
        "failed_count": sum(report.status == "failed" for report in reports),
    }


def _selection_metadata(
    reports: Sequence[DatasetPreflightReport],
) -> list[dict[str, Any]]:
    return [
        {
            "dataset": report.dataset.name,
            "input_scope": report.input_scope,
            "default_input": report.dataset.default_input,
            "selected_input": (
                report.dataset.inputs[report.selected_input_indices[0]].file
                if len(report.selected_input_indices) == 1
                else None
            ),
            "selected_input_count": len(report.selected_input_indices),
            "selected_inputs": [
                {
                    "input_index": index,
                    "display_number": index + 1,
                    "file": report.dataset.inputs[index].file,
                }
                for index in report.selected_input_indices
            ],
        }
        for report in reports
    ]


def _write_preflight_artifacts(
    artifact_root: Path,
    registry: Any,
    reports: Sequence[DatasetPreflightReport],
) -> Path:
    run_directory = _run_support.new_run_directory(artifact_root)
    datasets_directory = run_directory / "datasets"
    datasets_directory.mkdir()
    for report in reports:
        dataset_directory = datasets_directory / report.dataset.name
        dataset_directory.mkdir()
        _run_support.write_json(dataset_directory / "preflight.json", report.as_dict())
    return run_directory


async def _run_ttp_trial(
    case: Any,
    settings: Any,
    policy: Any,
    tracer: _RoundTracer | None = None,
) -> dict[str, Any]:
    if tracer is None:
        tracer = _RoundTracer(record_rows=False)
    try:
        request = TemplateRequest(
            command_outputs=[item.text for item in case.inputs],
            result_schema=case.schema,
        )
        result = await TtpGenerator(
            settings=settings,
            policy=policy,
        ).generate_from_schema(request, observer=tracer)
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
                "execution_facts": tracer.execution_facts,
            },
            case.expected_records,
        )
        return {
            "generation_result": result_payload,
            "independent_acceptance": acceptance,
            "execution_facts": tracer.execution_facts,
            "score": score,
            "exception_type": None,
        }
    except asyncio.CancelledError:
        raise
    except Exception as error:
        return {
            "generation_result": None,
            "independent_acceptance": None,
            "execution_facts": tracer.execution_facts,
            "score": score_ttp_template_output(
                {"execution_facts": tracer.execution_facts},
                case.expected_records,
            ),
            "exception_type": type(error).__name__,
        }


def _run_baseline(
    registry: Any,
    reports: Sequence[DatasetPreflightReport],
    artifact_root: Path,
    input_scope: str,
) -> int:
    counts = _stage_counts(reports)
    if counts["failed_count"]:
        run_directory = _write_preflight_artifacts(artifact_root, registry, reports)
        print(json.dumps({**counts, "status": "preflight_failed"}, sort_keys=True))
        print(f"preflight_artifacts: {run_directory}")
        return 2
    passed = 0
    complete_total = 0
    template_total = 0
    template_passed = 0
    for report in reports:
        entry = report.dataset
        if entry.stage == "template":
            if report.status == "pending":
                print(f"{entry.name}: PENDING")
                continue
            template_total += 1
            template_passed += 1
            print(
                f"{entry.name}: PASS "
                f"template_smoke_inputs={report.template_inputs_passed}",
            )
        elif entry.stage == "complete":
            if report.status == "pending":
                print(f"{entry.name}: PENDING")
                continue
            complete_total += 1
            ok = report.status == "passed" and report.baseline_exact is True
            print(
                f"{entry.name}: {'PASS' if ok else 'FAIL'} "
                f"baseline inputs={len(report.selected_input_indices)}"
            )
            passed += int(ok)
    run_directory = _write_preflight_artifacts(artifact_root, registry, reports)
    summary = {
        "runner_version": RUNNER_VERSION,
        "mode": "baseline",
        "input_scope": input_scope,
        "selected_inputs": _selection_metadata(reports),
        "registry": {"path": str(registry.path)},
        **counts,
        "runnable_count": template_total + complete_total,
        "template_smoke_pass_rate": (
            template_passed / template_total if template_total else None
        ),
        "baseline_exact_pass_rate": passed / complete_total if complete_total else None,
        "status": "success",
    }
    _run_support.write_json(run_directory / "summary.json", summary)
    print(f"baseline: {passed}/{complete_total} complete cases passed")
    print(f"template smoke: {template_passed}/{template_total} cases passed")
    print(f"pending: {counts['pending_count']}")
    print(f"summary_json: {run_directory / 'summary.json'}")
    return 0


def _safe_trial_projection(payload: Mapping[str, Any]) -> dict[str, Any]:
    score = payload["score"]
    result = payload.get("generation_result")
    metadata = result.get("metadata") if isinstance(result, Mapping) else None
    trace_id = (
        metadata.get("laminar_trace_id") if isinstance(metadata, Mapping) else None
    )
    return {
        **safe_trial_facts(payload, score["metrics"]),
        "execution_facts": project_execution_facts(payload.get("execution_facts")),
        "score": score,
        "trace_id": trace_id,
    }


def _git_state() -> dict[str, Any]:
    try:
        revision = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=PROJECT_ROOT,
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
        dirty = bool(
            subprocess.run(
                ["git", "status", "--porcelain"],
                cwd=PROJECT_ROOT,
                capture_output=True,
                text=True,
                check=True,
            ).stdout.strip()
        )
    except (OSError, subprocess.CalledProcessError):
        return {"revision": None, "dirty": None}
    return {"revision": revision, "dirty": dirty}


async def _run_ttp(
    args: argparse.Namespace,
    registry: Any,
    reports: Sequence[DatasetPreflightReport],
) -> int:
    cases = tuple(report.case for report in reports if report.case is not None)
    if not cases:
        artifact_root = _run_support.environment_path(
            "CLI_PARSER_TEST_SET_ARTIFACT_ROOT",
            PROJECT_ROOT / ".artifacts" / "test-set-evaluation",
        )
        run_directory = _write_preflight_artifacts(artifact_root, registry, reports)
        counts = _stage_counts(reports)
        summary = {
            "runner_version": RUNNER_VERSION,
            "mode": "ttp-only",
            "input_scope": args.input_scope,
            "selected_inputs": _selection_metadata(reports),
            "registry": {"path": str(registry.path)},
            **counts,
            "runnable_count": 0,
            "case_count": 0,
            "trial_count": 0,
            "status": "success",
        }
        _run_support.write_json(run_directory / "summary.json", summary)
        print("runnable_count: 0")
        print(f"summary_json: {run_directory / 'summary.json'}")
        return 0
    settings, policy, artifact_root, configuration = _configuration()
    run_directory = _run_support.new_run_directory(artifact_root)
    (run_directory / "datasets").mkdir()
    for report in reports:
        dataset_directory = run_directory / "datasets" / report.dataset.name
        dataset_directory.mkdir()
        _run_support.write_json(dataset_directory / "preflight.json", report.as_dict())
    git_state = _git_state()
    semaphore = asyncio.Semaphore(args.concurrency)

    async def execute(case: Any, trial_index: int) -> dict[str, Any]:
        async with semaphore:
            started_at = datetime.now(UTC).isoformat()
            tracer = _RoundTracer(record_rows=args.trace_rounds)
            payload = await _run_ttp_trial(case, settings, policy, tracer)
            finished_at = datetime.now(UTC).isoformat()
            document = {
                "runner_version": RUNNER_VERSION,
                "mode": "ttp-only",
                "started_at": started_at,
                "finished_at": finished_at,
                "git": git_state,
                "case": _case_metadata(case, args.input_scope),
                "trial_index": trial_index,
                "trial_id": f"{run_directory.name}/{case.id}/{trial_index + 1}",
                **_safe_trial_projection(payload),
            }
            case_directory = run_directory / "datasets" / case.id / "trials"
            case_directory.mkdir(parents=True, exist_ok=True)
            _run_support.write_json(
                case_directory / f"trial-{trial_index + 1:02d}.json",
                document,
            )
            if args.trace_rounds:
                trace_path = (
                    case_directory / f"trial-{trial_index + 1:02d}.rounds.jsonl"
                )
                with trace_path.open("w", encoding="utf-8", newline="\n") as handle:
                    for row in tracer.rows:
                        handle.write(
                            json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n",
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
                "trial_id": document["trial_id"],
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
    counts = _stage_counts(reports)
    case_pass_counts = _case_pass_counts(trials)
    baseline_document: Mapping[str, Any] | None = None
    if args.baseline is not None:
        baseline_document = _read_baseline(args.baseline)
        comparison = _compare_to_baseline(
            baseline_document,
            case_pass_counts,
            args.regression_tolerance,
        )
        status = "regressed" if comparison["regressed_cases"] else "passed"
    else:
        # Recording a run is not a failure. The old rule called every run since
        # the project began "failed", because it demanded that all trials pass.
        comparison = None
        status = "recorded"
    summary = {
        "runner_version": RUNNER_VERSION,
        "mode": "ttp-only",
        "captured_at": datetime.now(UTC).isoformat(),
        "input_scope": args.input_scope,
        "selected_inputs": _selection_metadata(reports),
        "status": status,
        "registry": {"path": str(registry.path)},
        "git": git_state,
        "configuration": configuration,
        "case_count": len(cases),
        "runnable_count": len(cases),
        "trial_count": len(trials),
        **counts,
        "pending": [
            report.dataset.name for report in reports if report.status == "pending"
        ],
        "strict_pass_count": sum(item["strict_pass"] for item in trials),
        "input_exact_match_micro": _input_exact_match_micro(trials),
        "case_pass_counts": case_pass_counts,
        "baseline_comparison": comparison,
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
    if args.write_baseline is not None:
        args.write_baseline.parent.mkdir(parents=True, exist_ok=True)
        _run_support.write_json(args.write_baseline, _baseline_document(summary))
        print(f"baseline_json: {args.write_baseline}")
    micro = summary["input_exact_match_micro"]
    print(
        f"candidate_pass: {summary['strict_pass_count']}/{summary['trial_count']}  "
        f"input_exact_match_micro: {micro['successes']:.0f}"
        f"/{micro['observations']:.0f}",
    )
    if comparison is not None:
        for record in comparison["regressed_cases"]:
            print(
                f"REGRESSED {record['case_id']}: "
                f"{record['baseline']} -> {record['current']}",
            )
        for record in comparison["improved_cases"]:
            print(
                f"improved  {record['case_id']}: "
                f"{record['baseline']} -> {record['current']}",
            )
        for case_id in comparison["missing_cases"]:
            print(f"missing from this run: {case_id}")
        for case_id in comparison["unknown_cases"]:
            print(f"not in baseline: {case_id}")
    print(f"status: {summary['status']}")
    print(f"summary_json: {summary_path}")
    return 1 if status == "regressed" else 0


class _FallbackSnapshot:
    """Only this private channel retains names; never serialize the snapshot."""

    __slots__ = ("names", "nodes", "fallback_nodes")

    def __init__(self, names, nodes, fallback_nodes):
        self.names = names
        self.nodes = nodes
        self.fallback_nodes = fallback_nodes


def _accepted_plan_fallback(value):
    from pydantic import ValidationError

    from cli_parser_agent.ttp_generation.schema_plan import SchemaPlan

    submitted = value.get("input")
    if not isinstance(submitted, Mapping):
        return None
    try:
        plan = SchemaPlan.model_validate(submitted.get("plan"))
    except ValidationError:
        return None
    names = [
        node.fallback_name for node in plan.nodes if node.fallback_name is not None
    ]
    return _FallbackSnapshot(frozenset(names), len(plan.nodes), len(names))


def _accepted_draft_fallback(value):
    """Observe final field names in memory; anonymous array items are not fields."""
    submitted = value.get("input")
    draft = submitted.get("draft") if isinstance(submitted, Mapping) else None
    if not isinstance(draft, Mapping) or type(draft.get("version")) is not int:
        return None
    if draft["version"] != 1 or not isinstance(draft.get("fields"), list):
        return None
    nodes = 0
    names = []
    pending = [(dict(type="object", fields=draft["fields"]), 1)]
    seen = set()
    while pending:
        node, depth = pending.pop()
        if not isinstance(node, Mapping) or depth > 16 or id(node) in seen:
            return None
        seen.add(id(node))
        node_type = node.get("type")
        if node_type == "object":
            fields = node.get("fields")
            if not isinstance(fields, list):
                return None
            nodes += len(fields)
            if nodes > 256:
                return None
            for item in fields:
                if not isinstance(item, Mapping):
                    return None
                name = item.get("name")
                if not isinstance(name, Mapping):
                    return None
                if "fallback" in name:
                    if not isinstance(name["fallback"], str):
                        return None
                    names.append(name["fallback"])
                elif not isinstance(name.get("source"), list):
                    return None
                pending.append((item.get("node"), depth + 1))
        elif node_type == "array":
            pending.append((node.get("items"), depth + 1))
        elif node_type not in {"string", "integer", "number", "boolean"}:
            return None
    return _FallbackSnapshot(frozenset(names), nodes, len(names))


_DRAFT_REJECTION_CODES = (
    "schema_draft.invalid_shape",
    "schema_draft.too_large",
    "schema_draft.invalid_sources",
    "schema_draft.invalid_reference",
    "schema_draft.ambiguous_reference",
    "schema_draft.partial_word",
    "schema_draft.source_order",
    "schema_draft.reference_limit",
    "schema_draft.invalid_name",
    "schema_draft.unnecessary_fallback",
    "schema_draft.name_collision",
    "schema_draft.invalid_attribute",
    "schema_draft.depth_exceeded",
    "schema_draft.property_limit",
    "schema_draft.other_rejection",
)


class _SchemaTracer:
    """Project observations; optionally retain only a frozen schema in memory."""

    def __init__(self, *, record_rows=False, capture_frozen=False):
        self.rounds = _RoundTracer(record_rows=record_rows)
        self.submissions = []
        self.input_tokens = []
        self.output_tokens = []
        self.capture_frozen = capture_frozen
        self.frozen_schema = None
        self.first_frozen_seconds = None
        self.confirmations = 0
        self.compiler_facts = []
        self._pending_fallback = None
        self.frozen_fallback = None
        self.sampling_observations = []
        self.prepared_input_chars = []
        self.protocol_categories = []
        self.protocol_boundaries = []
        self.reasoning_recovery_count = 0
        self.reasoning_recovery_rounds = []
        self._last_schema_submission = 0

    def _observe_source_and_protocol(self, event):
        value = event.value
        if not isinstance(value, Mapping):
            return
        if event.name == "cli_parser.phase.sampling_completed":
            outputs = value.get("sampled_outputs")
            if (
                isinstance(outputs, list)
                and outputs
                and all(
                    isinstance(item, Mapping)
                    and type(item.get("original_char_count")) is int
                    and item["original_char_count"] >= 0
                    and type(item.get("text")) is str
                    and type(item.get("truncated")) is bool
                    for item in outputs
                )
            ):
                self.sampling_observations.append(
                    {
                        "input_count": len(outputs),
                        "original_chars": sum(
                            item["original_char_count"] for item in outputs
                        ),
                        "sampled_chars": sum(len(item["text"]) for item in outputs),
                        "truncated_inputs": sum(item["truncated"] for item in outputs),
                        "input_fits": value.get("input_fits")
                        if type(value.get("input_fits")) is bool
                        else None,
                    }
                )
        elif event.name == "cli_parser.phase.input_prepared":
            message = value.get("message")
            content = message.get("content") if isinstance(message, Mapping) else None
            if isinstance(content, list) and all(
                isinstance(block, Mapping)
                and block.get("type") == "text"
                and type(block.get("text")) is str
                for block in content
            ):
                self.prepared_input_chars.append(
                    sum(len(block["text"]) for block in content)
                )
        elif event.name == "cli_parser.protocol.repair":
            category = value.get("category")
            if isinstance(category, str) and category in {
                "no_tool",
                "wrong_tool",
                "framework_arguments",
                "product_arguments",
            }:
                self.protocol_categories.append(category)
        elif event.name == "cli_parser.protocol.boundary":
            category = value.get("category")
            if isinstance(category, str) and category in {
                "arguments_valid",
                "business_rejected",
                "execution_error",
            }:
                self.protocol_boundaries.append(category)
        elif event.name == "cli_parser.schema.reasoning_recovery":
            # This event carries fixed protocol facts only. Do not accept
            # future free-text extensions into persisted evaluation artifacts.
            if (
                set(value)
                == {
                    "runtime_policy",
                    "mode",
                    "reason",
                    "consecutive_responses",
                    "after_round_index",
                    "after_attempt_index",
                }
                and value["runtime_policy"] == "schema-reasoning-recovery-v1"
                and value["mode"] == "thinking_disabled"
                and value["reason"] == "consecutive_reasoning_only_length"
                and type(value["consecutive_responses"]) is int
                and value["consecutive_responses"] == 3
                and all(
                    type(value[key]) is int and value[key] > 0
                    for key in ("after_round_index", "after_attempt_index")
                )
            ):
                self.reasoning_recovery_count += 1
                if len(self.reasoning_recovery_rounds) < 32:
                    self.reasoning_recovery_rounds.append(
                        {
                            key: value[key]
                            for key in (
                                "after_round_index",
                                "after_attempt_index",
                                "consecutive_responses",
                            )
                        }
                    )

    def __call__(self, event):
        self.rounds(event)
        metadata = getattr(event, "metadata", None) or {}
        if metadata.get("phase") != "schema":
            return
        if type(event) is agent_events.CustomEvent:
            self._observe_source_and_protocol(event)
        if (
            type(event) is agent_events.ToolCallStartEvent
            and event.tool_call_name == "submit_schema_plan"
        ):
            # Framework-rejected replacement attempts emit no product result.
            self._pending_fallback = None
        if type(event) is agent_events.ModelCallEndEvent:
            for field in ("input_tokens", "output_tokens"):
                value = getattr(event, field, None)
                if (
                    isinstance(value, int)
                    and not isinstance(value, bool)
                    and value >= 0
                ):
                    getattr(self, field).append(value)
        if (
            type(event) is not agent_events.CustomEvent
            or event.name != "cli_parser.tool.result"
        ):
            return
        value = event.value
        if not isinstance(value, Mapping) or value.get("tool_name") not in {
            "submit_result_schema",
            "submit_schema_plan",
            "submit_schema_draft",
            "confirm_schema_plan",
        }:
            return
        if value["tool_name"] == "submit_schema_plan":
            self._pending_fallback = None
        output = value.get("output")
        if not isinstance(output, Mapping) or not isinstance(
            output.get("accepted"), bool
        ):
            return
        if value["tool_name"] == "submit_schema_plan" and output["accepted"]:
            snapshot = _accepted_plan_fallback(value)
            if output.get("frozen") is True:
                if self.frozen_fallback is None:
                    self.frozen_fallback = snapshot
            else:
                self._pending_fallback = snapshot
        elif (
            value["tool_name"] == "submit_schema_draft"
            and output["accepted"]
            and output.get("frozen") is True
            and self.frozen_fallback is None
        ):
            self.frozen_fallback = _accepted_draft_fallback(value)
        elif (
            value["tool_name"] == "confirm_schema_plan"
            and output["accepted"]
            and output.get("frozen") is True
        ):
            if self.frozen_fallback is None:
                self.frozen_fallback = self._pending_fallback
            self._pending_fallback = None
        categories = set()
        draft_categories = set()
        issues = output.get("issues", [])
        for issue in issues if isinstance(issues, list) else []:
            code = issue.get("code") if isinstance(issue, Mapping) else None
            if value["tool_name"] == "submit_schema_draft":
                draft_categories.add(
                    code
                    if isinstance(code, str) and code in _DRAFT_REJECTION_CODES
                    else "schema_draft.other_rejection"
                )
            categories.add(
                code
                if isinstance(code, str)
                and code
                in {
                    "schema.python_keyword_property_name",
                    "schema.invalid_property_name",
                    "schema.reserved_scalar_field_name",
                }
                else "schema.other_rejection"
            )
        elapsed = metadata.get("elapsed_seconds")
        elapsed = (
            elapsed
            if (
                isinstance(elapsed, (int, float))
                and not isinstance(elapsed, bool)
                and math.isfinite(elapsed)
                and elapsed >= 0
            )
            else None
        )
        if output.get("frozen") is True and output["accepted"]:
            if self.first_frozen_seconds is None:
                self.first_frozen_seconds = elapsed
            if self.capture_frozen and self.frozen_schema is None:
                candidate = (
                    value.get("input", {}).get("result_schema")
                    if value["tool_name"] == "submit_result_schema"
                    and isinstance(value.get("input"), Mapping)
                    else output.get("compiled_schema")
                )
                if isinstance(candidate, dict):
                    self.frozen_schema = deepcopy(candidate)
        if value["tool_name"] == "confirm_schema_plan":
            self.confirmations += 1
            return
        compiler_facts = output.get("facts")
        if value["tool_name"] in {
            "submit_schema_plan",
            "submit_schema_draft",
        } and isinstance(compiler_facts, Mapping):
            self.compiler_facts.append(
                {
                    key: item
                    for key, item in compiler_facts.items()
                    if key
                    in {
                        "nodes",
                        "references",
                        "fallback_names",
                        "evidence_incomplete_nodes",
                        "required_fields",
                        "property_count",
                        "source_name_count",
                        "fallback_name_count",
                        "reference_count",
                        "draft_bytes",
                        "source_rejection_count",
                        "name_conflict_count",
                        "fallback_unlabeled_count",
                        "fallback_ambiguous_source_count",
                        "fallback_invalid_name_count",
                        "fallback_conflict_count",
                        "fallback_split_component_count",
                    }
                    and type(item) is int
                    and item >= 0
                }
            )
        # The counter is cumulative; argument rejection can repeat its prior
        # value. Legacy observations lacking a counter retain their old meaning.
        submission = output.get("schema_submission")
        if type(submission) is int:
            if submission <= self._last_schema_submission:
                return
            self._last_schema_submission = submission
        elif value["tool_name"] == "submit_schema_draft":
            return
        self.submissions.append(
            {
                "accepted": output["accepted"],
                "categories": sorted(categories),
                "draft_categories": sorted(draft_categories),
                "draft_submission": value["tool_name"] == "submit_schema_draft",
                "elapsed_seconds": elapsed,
            }
        )

    def facts(self):
        sampling = (
            self.sampling_observations[-1] if self.sampling_observations else None
        )
        prepared = self.prepared_input_chars[-1] if self.prepared_input_chars else None
        return {
            "observed_submissions": len(self.submissions),
            "first_submission_accepted": self.submissions[0]["accepted"]
            if self.submissions
            else None,
            "first_frozen_seconds": self.first_frozen_seconds,
            "observed_confirmations": self.confirmations,
            "compiler_facts": self.compiler_facts,
            "source_display": {
                "sampling_observations": len(self.sampling_observations),
                "input_prepared_observations": len(self.prepared_input_chars),
                "original_chars": sampling["original_chars"] if sampling else None,
                "sampled_chars": sampling["sampled_chars"] if sampling else None,
                "truncated_inputs": sampling["truncated_inputs"] if sampling else None,
                "input_count": sampling["input_count"] if sampling else None,
                "input_fits": sampling["input_fits"] if sampling else None,
                "prepared_message_chars": prepared,
                # Includes task framing and source display: not a tokenizer
                # estimate or a pure numbering-only overhead measurement.
                "prepared_minus_sampled_chars": prepared - sampling["sampled_chars"]
                if prepared is not None and sampling is not None
                else None,
            },
            "protocol": {
                "repair_observations": len(self.protocol_categories),
                "category_counts": {
                    key: self.protocol_categories.count(key)
                    for key in sorted(set(self.protocol_categories))
                },
                "boundary_observations": len(self.protocol_boundaries),
                "boundary_counts": {
                    key: self.protocol_boundaries.count(key)
                    for key in sorted(set(self.protocol_boundaries))
                },
            },
            "reasoning_recovery": {
                # Absence also covers legacy observers and missing telemetry;
                # it does not prove that the recovery was never activated.
                "status": "observed"
                if self.reasoning_recovery_count
                else "unavailable",
                "activation_count": self.reasoning_recovery_count or None,
                "rounds": list(self.reasoning_recovery_rounds),
                "runtime_policy": "schema-reasoning-recovery-v1"
                if self.reasoning_recovery_count
                else None,
                "mode": "thinking_disabled" if self.reasoning_recovery_count else None,
                "reason": "consecutive_reasoning_only_length"
                if self.reasoning_recovery_count
                else None,
            },
            # AgentScope's ModelCallEndEvent is a framework finish reason;
            # it does not expose the supplier's `length` finish_reason.
            "provider_finish_reason_observations": 0,
            "provider_length_count": None,
            "draft_rejection_counts": {
                code: sum(
                    not s["accepted"] and code in s["draft_categories"]
                    for s in self.submissions
                )
                if any(s["draft_submission"] for s in self.submissions)
                else None
                for code in _DRAFT_REJECTION_CODES
            },
            "rejection_counts": {
                code: sum(
                    not s["accepted"] and code in s["categories"]
                    for s in self.submissions
                )
                if self.submissions
                else None
                for code in (
                    "schema.python_keyword_property_name",
                    "schema.invalid_property_name",
                    "schema.reserved_scalar_field_name",
                    "schema.other_rejection",
                )
            },
            "input_tokens": sum(self.input_tokens) if self.input_tokens else None,
            "output_tokens": sum(self.output_tokens) if self.output_tokens else None,
            "usage_observations": len(self.input_tokens),
            "reasoning_tokens": None,
        }


async def _run_schema_trial(case, settings, policy, tracer):
    from cli_parser_agent.ttp_generation.validation import validate_result_schema

    started = time.monotonic()
    schema = None
    document = {
        "generation_success": False,
        "proposal_revalidated": None,
        "exception_type": None,
    }
    try:
        result = await TtpGenerator(settings=settings, policy=policy).propose_schema(
            GenerationRequest(command_outputs=[item.text for item in case.inputs]),
            observer=tracer,
        )
        document["generation_success"] = result.status == "success"
        metadata = result.metadata
        document.update(
            {
                "trace_id": metadata.laminar_trace_id,
                "schema_submissions": metadata.schema_submissions,
                "schema_agent_rounds": metadata.schema_agent_rounds,
                "termination_reason": metadata.termination_reason,
            }
        )
        if result.proposal is not None:
            issues = validate_result_schema(result.proposal.result_schema)
            document["proposal_revalidated"] = not issues
            if not issues:
                schema = result.proposal.result_schema
                document["structure"] = schema_proposal_metrics(schema, case.schema)
    except asyncio.CancelledError:
        raise
    except Exception as error:
        document["exception_type"] = (
            type(error).__name__
            if type(error)
            in {ValueError, TypeError, RuntimeError, OSError, TimeoutError}
            else "Exception"
        )
    document["elapsed_seconds"] = time.monotonic() - started
    document["observations"] = tracer.facts()
    document["observations"]["submission_observation_complete"] = (
        document.get("schema_submissions") == len(tracer.submissions)
        if "schema_submissions" in document
        else None
    )
    return document, schema


async def _run_end_to_end_trial(case, settings, policy, tracer):
    """Score generated contracts without applying the reference field layout."""
    from cli_parser_agent.ttp_generation.validation import validate_result_schema

    started = time.monotonic()
    schema = None
    document = {
        "generation_success": False,
        "schema_generation_success": False,
        "proposal_revalidated": None,
        "independent_acceptance": None,
        "execution_facts": {},
        "exception_type": None,
    }
    try:
        request = GenerationRequest(command_outputs=[item.text for item in case.inputs])
        result = await TtpGenerator(settings=settings, policy=policy).generate(
            request, observer=tracer
        )
        document["generation_success"] = result.status == "success"
        metadata = result.metadata
        for field in (
            "schema_submissions",
            "schema_agent_rounds",
            "ttp_submissions",
            "ttp_test_calls",
            "ttp_agent_rounds",
            "agent_rounds",
            "termination_reason",
            "input_tokens_total",
            "output_tokens_total",
            "model_calls_observed",
        ):
            document[field] = getattr(metadata, field, None)
        document["trace_id"] = metadata.laminar_trace_id
        candidate = (
            result.artifact.result_schema
            if result.status == "success" and result.artifact is not None
            else tracer.frozen_schema
        )
        if candidate is not None:
            document["schema_generation_success"] = True
            issues = validate_result_schema(candidate)
            document["proposal_revalidated"] = not issues
            if not issues:
                schema = candidate
                document["structure"] = schema_proposal_metrics(schema, case.schema)
        acceptance = await asyncio.to_thread(
            independent_acceptance, result, request.command_outputs, policy
        )
        document["independent_acceptance"] = acceptance
    except asyncio.CancelledError:
        raise
    except Exception as error:
        document["exception_type"] = (
            type(error).__name__
            if type(error)
            in {ValueError, TypeError, RuntimeError, OSError, TimeoutError}
            else "Exception"
        )
    # A frozen contract remains evaluable even if the later TTP path raises.
    if schema is None and tracer.frozen_schema is not None:
        document["schema_generation_success"] = True
        issues = validate_result_schema(tracer.frozen_schema)
        document["proposal_revalidated"] = not issues
        if not issues:
            schema = tracer.frozen_schema
            document["structure"] = schema_proposal_metrics(schema, case.schema)
    document["elapsed_seconds"] = time.monotonic() - started
    document["observations"] = tracer.facts()
    document["observations"]["submission_observation_complete"] = (
        document.get("schema_submissions") == len(tracer.submissions)
        if "schema_submissions" in document
        else None
    )
    document["execution_facts"] = tracer.rounds.execution_facts
    return document, schema


def _schema_succeeded(document):
    return document.get("schema_generation_success", document["generation_success"])


def _fallback_trial_metrics(snapshot, *, applicable):
    return {
        "status": "not_applicable"
        if not applicable
        else "observed"
        if snapshot is not None
        else "unobserved",
        "business_nodes": snapshot.nodes if snapshot is not None else None,
        "fallback_nodes": snapshot.fallback_nodes if snapshot is not None else None,
        "fallback_ratio": snapshot.fallback_nodes / snapshot.nodes
        if snapshot is not None and snapshot.nodes
        else None,
    }


def _fallback_repeat_consistency(results):
    """Name-set differences are not evidence that the same field was renamed."""
    valid = [
        (document["trial_id"], snapshot)
        for document, schema, snapshot in results
        if _schema_succeeded(document)
        and document["proposal_revalidated"] is True
        and schema is not None
    ]
    observed = [(tid, snapshot) for tid, snapshot in valid if snapshot is not None]
    pairs = []
    for (left_id, left), (right_id, right) in combinations(observed, 2):
        intersection = len(left.names & right.names)
        union = len(left.names | right.names)
        pairs.append(
            {
                "left_trial_id": left_id,
                "right_trial_id": right_id,
                "name_sets_equal": left.names == right.names,
                "name_set_jaccard": intersection / union if union else 1.0,
                "intersection_count": intersection,
                "union_count": union,
                "left_only_count": len(left.names - right.names),
                "right_only_count": len(right.names - left.names),
                "symmetric_difference_count": len(left.names ^ right.names),
            }
        )
    return {
        "valid_schema_count": len(valid),
        "fallback_observation_count": len(observed),
        "evaluated_pair_count": len(pairs),
        "name_sets_equal_pair_count": sum(pair["name_sets_equal"] for pair in pairs),
        "name_sets_equal_rate": sum(pair["name_sets_equal"] for pair in pairs)
        / len(pairs)
        if pairs
        else None,
        "name_set_jaccard_mean": sum(pair["name_set_jaccard"] for pair in pairs)
        / len(pairs)
        if pairs
        else None,
        "pairs": pairs,
    }


async def _run_schema(args, registry, reports):
    """Run the default product over one shared in-memory input snapshot."""
    cases = tuple(report.case for report in reports if report.case is not None)
    settings, policy, artifact_root, configuration = _configuration()
    git_state = _git_state()
    semaphore = asyncio.Semaphore(args.concurrency)
    run_directory = _write_preflight_artifacts(artifact_root, registry, reports)
    print(
        f"running {args.mode}: cases={len(cases)} trials={args.trials} "
        f"concurrency={args.concurrency}",
        flush=True,
    )
    tasks = [
        asyncio.create_task(
            _execute_schema_trial(
                args, case, index, settings, policy, run_directory, git_state, semaphore
            )
        )
        for case in cases
        for index in range(args.trials)
    ]
    try:
        results = await asyncio.gather(*tasks)
    except BaseException:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        raise
    _write_schema_summary(
        args, registry, reports, cases, results, run_directory, git_state, configuration
    )
    return 0


async def _execute_schema_trial(
    args, case, index, settings, policy, run_directory, git_state, semaphore
):
    async with semaphore:
        started_at = datetime.now(UTC).isoformat()
        tracer = _SchemaTracer(
            record_rows=args.trace_rounds, capture_frozen=args.mode == "end-to-end"
        )
        trial_runner = (
            _run_end_to_end_trial if args.mode == "end-to-end" else _run_schema_trial
        )
        document, schema = await trial_runner(case, settings, policy, tracer)
        fallback = (
            tracer.frozen_fallback
            if _schema_succeeded(document)
            and document["proposal_revalidated"] is True
            and schema is not None
            else None
        )
        document["fallback_naming"] = _fallback_trial_metrics(
            fallback, applicable=fallback is not None
        )
        document.update(
            {
                "runner_version": RUNNER_VERSION,
                "mode": args.mode,
                "schema_metrics_version": SCHEMA_METRICS_VERSION,
                "case_id": case.id,
                "case": _case_metadata(case, args.input_scope),
                "trial_index": index,
                "trial_id": f"{run_directory.name}/{case.id}/{index + 1}",
                "started_at": started_at,
                "finished_at": datetime.now(UTC).isoformat(),
                "git": git_state,
            }
        )
        directory = run_directory / "datasets" / case.id / "trials"
        directory.mkdir(parents=True, exist_ok=True)
        _run_support.write_json(directory / f"trial-{index + 1:02d}.json", document)
        if args.trace_rounds:
            with (directory / f"trial-{index + 1:02d}.rounds.jsonl").open(
                "w", encoding="utf-8"
            ) as handle:
                for row in tracer.rounds.rows:
                    handle.write(json.dumps(row, ensure_ascii=False) + "\n")
        print(
            f"{case.id} trial={index + 1}: "
            f"schema_success={document['generation_success']}",
            flush=True,
        )
        return document, schema, fallback


def _write_schema_summary(
    args, registry, reports, cases, results, run_directory, git_state, configuration
):
    trials = [document for document, _, _ in results]
    consistency = {
        case.id: schema_repeat_consistency(
            [
                schema
                for document, schema, _ in results
                if document["case_id"] == case.id and schema is not None
            ]
        )
        for case in cases
    }
    contracts = {
        case.id: schema_contract_consistency(
            [
                (document["trial_id"], schema)
                for document, schema, _ in results
                if document["case_id"] == case.id
                and _schema_succeeded(document)
                and document["proposal_revalidated"] is True
                and schema is not None
            ]
        )
        for case in cases
    }
    summary = {
        "schema_metrics_version": SCHEMA_METRICS_VERSION,
        "run_id": run_directory.name,
        "planned_trials_per_case": args.trials,
        "contract_consistency": contracts,
        "fallback_naming_consistency": {
            case.id: _fallback_repeat_consistency(
                [result for result in results if result[0]["case_id"] == case.id]
            )
            for case in cases
        },
        "consistency_overview": schema_consistency_overview(contracts),
        "parseability": "executed_pending_review"
        if args.mode == "end-to-end"
        else "not_tested",
        "runner_version": RUNNER_VERSION,
        "mode": args.mode,
        "status": "recorded",
        "captured_at": datetime.now(UTC).isoformat(),
        "input_scope": args.input_scope,
        "selected_inputs": _selection_metadata(reports),
        "registry": {"path": str(registry.path)},
        "git": git_state,
        "configuration": configuration,
        "case_count": len(cases),
        "trial_count": len(trials),
        "generation_success_count": sum(t["generation_success"] for t in trials),
        "generation_success_rate": sum(t["generation_success"] for t in trials)
        / len(trials)
        if trials
        else None,
        "proposal_revalidated_count": sum(
            t["proposal_revalidated"] is True for t in trials
        ),
        "proposal_revalidation_observations": sum(
            t["proposal_revalidated"] is not None for t in trials
        ),
        "cases": consistency,
        "trials": trials,
    }
    if args.mode == "end-to-end":
        summary["schema_generation_success_count"] = sum(
            _schema_succeeded(t) for t in trials
        )
        summary["independent_acceptance_count"] = sum(
            isinstance(t["independent_acceptance"], Mapping)
            and t["independent_acceptance"].get("valid") is True
            for t in trials
        )
    _run_support.write_json(run_directory / "summary.json", summary)
    print(f"summary_json: {run_directory / 'summary.json'}", flush=True)
    return summary


class _RoundTracer:
    """Always collect execution facts; optionally retain allowlisted event rows."""

    _EVENT_TYPES = frozenset(
        {
            agent_events.ReplyStartEvent,
            agent_events.ReplyEndEvent,
            agent_events.ModelCallStartEvent,
            agent_events.ModelCallEndEvent,
            agent_events.TextBlockStartEvent,
            agent_events.TextBlockEndEvent,
            agent_events.ThinkingBlockStartEvent,
            agent_events.ThinkingBlockEndEvent,
            agent_events.DataBlockStartEvent,
            agent_events.DataBlockEndEvent,
            agent_events.ToolCallStartEvent,
            agent_events.ToolCallEndEvent,
            agent_events.ToolResultStartEvent,
            agent_events.ToolResultEndEvent,
            agent_events.ExceedMaxItersEvent,
        }
    )
    _TOOL_NAMES = frozenset(
        {
            "submit_result_schema",
            "submit_schema_plan",
            "submit_schema_draft",
            "confirm_schema_plan",
            "submit_ttp_template",
            "test_ttp_template",
            "finish_generation",
        }
    )
    _FINISHED_REASONS = frozenset({"completed", "interrupted", "exceed_max_iters"})
    _VALUE_FIELDS = frozenset(
        {
            "submission_index",
            "template_chars",
            "compacted_interactions",
            "retained_interactions",
            "compacted_input_chars",
            "compacted_result_chars",
            "remaining_seconds",
            "retry_number",
            "max_retries",
            "status",
            "phase_completed",
            "agent_rounds",
            "termination_reason",
            "exception_type",
            "valid",
            "reason",
            "reply_id",
            "model_call_event_id",
            "event",
            "phase",
            "round_index",
            "attempt_index",
            "request_attempt_index",
            "is_retry",
            "outcome",
            "error_category",
            "elapsed_seconds",
            "category",
            "consecutive_failures",
            "repair_limit",
            "stopped",
        }
    )
    _EVENT_NAMES = frozenset(
        {
            "cli_parser.generation.execution_facts",
            "cli_parser.generation.cancelled",
            "cli_parser.generation.exception",
            "cli_parser.phase.completed",
            "cli_parser.final_validation.started",
            "cli_parser.final_validation.completed",
            "cli_parser.ttp.history_compacted",
            "cli_parser.round.skipped",
            "cli_parser.model.output_discarded",
            "cli_parser.no_tool.retry",
            "cli_parser.ttp.submission",
            "cli_parser.model.attempt",
            "cli_parser.protocol.repair",
            "cli_parser.protocol.boundary",
        }
    )

    def __init__(self, *, record_rows: bool = True) -> None:
        self.rows: list[dict[str, Any]] = []
        self.execution_facts: dict[str, bool] = {}
        self.record_rows = record_rows

    def __call__(self, event: Any) -> None:
        metadata = getattr(event, "metadata", None) or {}
        if metadata.get("sensitive") is not False:
            return
        is_custom = type(event) is agent_events.CustomEvent
        if not is_custom and type(event) not in self._EVENT_TYPES:
            return
        custom_name = event.name if is_custom else None
        value = getattr(event, "value", None)
        if custom_name == "cli_parser.generation.execution_facts":
            self.execution_facts = project_execution_facts(value)
            value = self.execution_facts
        if not self.record_rows:
            return
        if custom_name is not None and custom_name not in self._EVENT_NAMES:
            return
        row: dict[str, Any] = {
            "sequence": metadata.get("sequence"),
            "phase": metadata.get("phase"),
            "elapsed_seconds": metadata.get("elapsed_seconds"),
            "event": type(event).__name__,
        }
        reason = getattr(event, "finished_reason", None)
        if isinstance(reason, str) and reason in self._FINISHED_REASONS:
            row["finished_reason"] = reason
        for field in ("input_tokens", "output_tokens"):
            field_value = getattr(event, field, None)
            if isinstance(field_value, int) and not isinstance(field_value, bool):
                row[field] = field_value
        name = getattr(event, "tool_call_name", None)
        if name is not None:
            row["name"] = (
                name
                if isinstance(name, str) and name in self._TOOL_NAMES
                else "unknown_tool"
            )
        elif custom_name is not None:
            row["name"] = custom_name
        if isinstance(value, Mapping):
            row["value"] = {
                key: item
                for key, item in value.items()
                if (key in self._VALUE_FIELDS or key in self.execution_facts)
                and (
                    item is None
                    or isinstance(item, bool)
                    or isinstance(item, int | float)
                    and math.isfinite(item)
                    or isinstance(item, str)
                    and len(item) <= 128
                    and re.fullmatch(r"[A-Za-z0-9_.:-]+", item) is not None
                )
            }
        self.rows.append(row)


def _input_exact_match_micro(trials: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Pool per-input exact matches across every trial.

    aggregate_trial_scores averages input_exact_match_rate across trials, which
    macro-weights cases with few inputs equally against cases with many. Pooling
    the raw counts instead scores each *input*, so a full run observes one point
    per input rather than one per trial -- roughly triple the sample, for free,
    and still a strict exact match with no partial credit.
    """
    successes = sum(
        float(trial["metrics"].get("input_exact_match_count", 0.0)) for trial in trials
    )
    observations = sum(
        float(trial["metrics"].get("input_count", 0.0)) for trial in trials
    )
    low, high = wilson_interval(successes, observations)
    return {
        "successes": successes,
        "observations": observations,
        "rate": (successes / observations) if observations else 0.0,
        "wilson_95": {"lower": low, "upper": high},
    }


def _read_baseline(path: Path) -> Mapping[str, Any]:
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except OSError:
        raise ScriptConfigurationError(f"baseline {path} could not be read") from None
    except ValueError:
        raise ScriptConfigurationError(f"baseline {path} is not valid JSON") from None
    if not isinstance(document, Mapping):
        raise ScriptConfigurationError(f"baseline {path} must be a JSON object")
    version = document.get("baseline_version")
    if version != BASELINE_VERSION:
        raise ScriptConfigurationError(
            f"baseline {path} has version {version!r}, expected {BASELINE_VERSION}",
        )
    return document


def _case_pass_counts(
    trials: Sequence[Mapping[str, Any]],
) -> dict[str, dict[str, int]]:
    """Per-case candidate_pass successes out of attempted trials."""
    counts: dict[str, dict[str, int]] = {}
    for trial in trials:
        entry = counts.setdefault(
            str(trial["case_id"]),
            {"candidate_pass_successes": 0, "trials": 0},
        )
        entry["trials"] += 1
        entry["candidate_pass_successes"] += int(bool(trial["strict_pass"]))
    return counts


def _compare_to_baseline(
    baseline: Mapping[str, Any],
    case_counts: Mapping[str, Mapping[str, int]],
    tolerance: int,
) -> dict[str, Any]:
    """Compare per-case pass counts, not the aggregate rate.

    The aggregate cannot move detectably on this corpus -- 24 observations are
    8 clusters, and five of eight cases land on 0/3 or 3/3 -- but a single case
    going 3/3 to 1/3 is real and attributable, so that is what gates.
    """
    baseline_cases = baseline.get("cases")
    if not isinstance(baseline_cases, Mapping):
        raise ScriptConfigurationError("baseline does not contain a cases mapping")
    regressed: list[dict[str, Any]] = []
    improved: list[dict[str, Any]] = []
    missing = sorted(set(baseline_cases) - set(case_counts))
    unknown = sorted(set(case_counts) - set(baseline_cases))
    for case_id, current in sorted(case_counts.items()):
        previous = baseline_cases.get(case_id)
        if not isinstance(previous, Mapping):
            continue
        before = int(previous.get("candidate_pass_successes", 0))
        before_trials = int(previous.get("trials", 0)) or 1
        after = int(current["candidate_pass_successes"])
        after_trials = int(current["trials"]) or 1
        # Rates, because a baseline frozen at 5 trials is routinely compared
        # against a 3-trial run. Express the gap back in trials so the
        # tolerance stays readable as "how many trials may drop".
        delta = (after / after_trials) - (before / before_trials)
        record = {
            "case_id": case_id,
            "baseline": f"{before}/{before_trials}",
            "current": f"{after}/{after_trials}",
            "delta_rate": delta,
        }
        if -delta * after_trials > tolerance:
            regressed.append(record)
        elif delta > 0:
            improved.append(record)
    return {
        "regressed_cases": regressed,
        "improved_cases": improved,
        "missing_cases": missing,
        "unknown_cases": unknown,
    }


def _baseline_document(summary: Mapping[str, Any]) -> dict[str, Any]:
    """Project a run into a committable, numbers-only baseline.

    Deliberately carries no template, records, raw input, or model text, so it
    can live in git while .artifacts/ stays ignored.
    """
    return {
        "baseline_version": BASELINE_VERSION,
        "captured_at": summary["captured_at"],
        "runner_version": summary["runner_version"],
        "configuration": summary["configuration"],
        "input_scope": summary["input_scope"],
        "trial_count": summary["trial_count"],
        "cases": summary["case_pass_counts"],
        "overall": {
            "candidate_pass": {
                "successes": summary["strict_pass_count"],
                "observations": summary["trial_count"],
            },
            "input_exact_match_micro": summary["input_exact_match_micro"],
        },
    }


def _list_cases(args: argparse.Namespace) -> int:
    registry = load_dataset_registry(args.registry)
    entries = select_dataset_entries(
        registry,
        names=args.dataset_names or (),
        ids=args.dataset_ids or (),
        tags=args.tags or (),
    )
    for entry in entries:
        selection = dataset_input_scope_metadata(entry, args.input_scope)
        selected_input_indices = selection["selected_input_indices"]
        scope_eligible = bool(selected_input_indices)
        print(
            json.dumps(
                {
                    "id": entry.id,
                    "name": entry.name,
                    "command": entry.command,
                    "platform": entry.platform,
                    "tags": list(entry.tags),
                    "input_count": len(entry.inputs),
                    "input_scope": args.input_scope,
                    "default_input": entry.default_input,
                    "selected_input": selection["selected_input"],
                    "selected_input_count": len(selected_input_indices),
                    "selected_inputs": list(selection["selected_inputs"]),
                    "stage": entry.stage,
                    "present_files": list(entry.present_files),
                    "missing_files": list(entry.missing_files),
                    "registry_errors": list(entry.registry_errors),
                    "eligible": {
                        "baseline": entry.stage in {"template", "complete"}
                        and not entry.missing_files
                        and not entry.registry_errors
                        and scope_eligible,
                        "ttp_only": entry.stage == "complete"
                        and not entry.missing_files
                        and not entry.registry_errors
                        and scope_eligible,
                    },
                },
                ensure_ascii=False,
                sort_keys=True,
            ),
        )
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    try:
        if args.command in {"schema-review", "parse-review"}:

            def unique_review_keys(pairs):
                node = {}
                for key, value in pairs:
                    if key in node:
                        raise ScriptConfigurationError("duplicate review key")
                    node[key] = value
                return node

            try:
                summary = json.loads(
                    (args.run_directory / "summary.json").read_text(encoding="utf-8")
                )
                review = json.loads(
                    args.review_file.read_text(encoding="utf-8"),
                    object_pairs_hook=unique_review_keys,
                )
                schema_review = (
                    json.loads(
                        args.schema_review_file.read_text(encoding="utf-8"),
                        object_pairs_hook=unique_review_keys,
                    )
                    if args.command == "parse-review"
                    else None
                )
            except (OSError, ValueError):
                raise ScriptConfigurationError(
                    "invalid schema review input file"
                ) from None
            output = (
                summarize_parse_review(summary, review, schema_review)
                if args.command == "parse-review"
                else summarize_schema_review(summary, review)
            )
            destination = args.run_directory / f"{args.command}-summary.json"
            sources = [args.review_file, args.run_directory / "summary.json"]
            if args.command == "parse-review":
                sources.append(args.schema_review_file)
            if any(destination.resolve() == p.resolve() for p in sources):
                raise ScriptConfigurationError(
                    "review source cannot be the output file"
                )
            _run_support.write_json(destination, output)
            print(f"review_summary_json: {destination}")
            return 0
        if getattr(args, "mode", None) in {"schema-only", "end-to-end"} and (
            args.baseline is not None
            or args.write_baseline is not None
            or args.regression_tolerance
        ):
            raise ScriptConfigurationError(
                "generated-schema modes do not support TTP baseline options"
            )
        if args.command == "list":
            return _list_cases(args)
        registry = load_dataset_registry(args.registry)
        entries = select_dataset_entries(
            registry,
            names=args.dataset_names or (),
            ids=args.dataset_ids or (),
            tags=args.tags or (),
        )
        reports = _selected_reports(registry, entries, args.input_scope)
        artifact_root = _run_support.environment_path(
            "CLI_PARSER_TEST_SET_ARTIFACT_ROOT",
            PROJECT_ROOT / ".artifacts" / "test-set-evaluation",
        )
        if args.command == "preflight":
            run_directory = _write_preflight_artifacts(artifact_root, registry, reports)
            counts = _stage_counts(reports)
            summary = {
                "runner_version": RUNNER_VERSION,
                "mode": "preflight",
                "input_scope": args.input_scope,
                "selected_inputs": _selection_metadata(reports),
                "registry": {"path": str(registry.path)},
                **counts,
                "runnable_count": sum(
                    report.status == "passed" and report.dataset.stage != "inputs-only"
                    for report in reports
                ),
                "status": "failed" if counts["failed_count"] else "passed",
            }
            _run_support.write_json(run_directory / "summary.json", summary)
            print(json.dumps(summary, ensure_ascii=False, sort_keys=True))
            return 2 if counts["failed_count"] else 0
        if args.mode == "baseline":
            return _run_baseline(
                registry,
                reports,
                artifact_root,
                args.input_scope,
            )
        if any(report.status == "failed" for report in reports):
            run_directory = _write_preflight_artifacts(artifact_root, registry, reports)
            print(f"preflight failed; artifacts: {run_directory}", file=sys.stderr)
            return 2
        if args.mode in {"schema-only", "end-to-end"}:
            return asyncio.run(_run_schema(args, registry, reports))
        return asyncio.run(_run_ttp(args, registry, reports))
    except (HarnessError, ScriptConfigurationError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("cancelled", file=sys.stderr)
        return 130
    finally:
        if args.command == "run" and getattr(args, "mode", None) in {
            "ttp-only",
            "schema-only",
            "end-to-end",
        }:
            _run_support.flush_laminar()


if __name__ == "__main__":
    raise SystemExit(main())
