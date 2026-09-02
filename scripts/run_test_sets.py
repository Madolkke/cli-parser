"""Run TOML-registered test sets offline or against the TTP agent."""

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
    DatasetPreflightReport,
    HarnessError,
    aggregate_trial_scores,
    dataset_input_scope_metadata,
    independent_acceptance,
    load_dataset_registry,
    preflight_dataset_registry,
    score_ttp_template_output,
    select_dataset_entries,
    wilson_interval,
)
from cli_parser_agent.ttp_generation.agent.prompt import (  # noqa: E402
    PROMPT_VERSION,
    SCHEMA_SYSTEM_PROMPT,
    TTP_SYSTEM_PROMPT,
)

RUNNER_VERSION = 3
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
    run.add_argument("--mode", choices=("baseline", "ttp-only"), required=True)
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


def _text_digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


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
        # The prompt is the most likely thing to be A/B tested, so it has to be
        # part of the fingerprint or two runs of different prompts compare as
        # identical configurations. Carry both: the version string keeps diffs
        # readable, and the content hashes catch an edit that forgot to bump it
        # (AGENTS.md and PROMPT_VERSION have already drifted apart once).
        "prompt": {
            "version": PROMPT_VERSION,
            "schema_system_sha256": _text_digest(SCHEMA_SYSTEM_PROMPT),
            "ttp_system_sha256": _text_digest(TTP_SYSTEM_PROMPT),
        },
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
        "files": {
            "schema": {"sha256": case.file_sha256["schema"]},
            "template": {"sha256": case.file_sha256["template"]},
            "expected": {"sha256": case.file_sha256["expected"]},
            "inputs": [
                {
                    "name": f"{original_index + 1:03d}.txt",
                    "sha256": item.sha256,
                }
                for original_index, item in zip(
                    original_input_indices,
                    case.inputs,
                    strict=True,
                )
            ],
        },
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
        "registry": {"path": str(registry.path), "sha256": registry.sha256},
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
            "registry": {"path": str(registry.path), "sha256": registry.sha256},
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
                "case": _case_metadata(case, args.input_scope),
                "trial_index": trial_index,
                **payload,
            }
            case_directory = run_directory / "datasets" / case.id / "trials"
            case_directory.mkdir(parents=True, exist_ok=True)
            _run_support.write_json(
                case_directory / f"trial-{trial_index + 1:02d}.json",
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
        "registry": {"path": str(registry.path), "sha256": registry.sha256},
        "config_fingerprint": config_fingerprint,
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
        "registry_sha256": summary["registry"]["sha256"],
        "config_fingerprint": summary["config_fingerprint"],
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
                "registry": {"path": str(registry.path), "sha256": registry.sha256},
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
        return asyncio.run(_run_ttp(args, registry, reports))
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
