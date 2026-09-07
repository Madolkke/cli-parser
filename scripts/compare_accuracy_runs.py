"""Compare completed TTP-only runs without exporting source or Trace bodies.

Usage: uv run python scripts/compare_accuracy_runs.py RUN_DIR RUN_DIR [...]
Add --laminar to query bounded aggregate telemetry using LMNR environment vars.
Strict scores always come from local independent evaluation results.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import re
from collections import Counter
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit
from uuid import UUID

import httpx

MAX_BYTES = 8 * 1024 * 1024
MAX_TRIALS = 1_000
FUNNEL = (
    "valid_ttp_candidate",
    "finish_called",
    "finish_succeeded",
    "generation_success",
    "independent_acceptance",
)
LOCAL_METRICS = (
    "elapsed_seconds",
    "ttp_submissions",
    "ttp_test_calls",
    "model_attempts_observed",
    "input_tokens_total",
    "output_tokens_total",
    "stream_model_call_elapsed_seconds",
    "ttp_history_compaction_events",
)
LLM_METRICS = (
    "llm_calls",
    "input_tokens",
    "output_tokens",
    "reasoning_tokens",
    "reasoning_observed_calls",
    "llm_seconds",
    "recorded_cost_usd",
    "context_fit_calls",
    "context_fit_seconds",
)
TOOL_METRICS = (
    "submissions",
    "accepted_submissions",
    "schema_error_submissions",
    "worker_error_submissions",
    "system_exit_submissions",
    "incompatible_pipe_submissions",
    "tests",
    "successful_tests",
    "worker_error_tests",
    "system_exit_tests",
    "incompatible_pipe_tests",
)
LEGACY_HASH_FIELDS = {
    "model": {"extra_body_sha256"},
    "prompt": {"schema_system_sha256", "ttp_system_sha256"},
}


def _number(value: Any) -> float | None:
    if type(value) not in (int, float) or not math.isfinite(value):
        return None
    return float(value) if 0 <= value <= 1e12 else None


def _time(value: Any) -> datetime:
    if not isinstance(value, str) or len(value) > 40:
        raise ValueError("Invalid timestamp; value omitted")
    try:
        result = datetime.fromisoformat(value)
    except ValueError:
        raise ValueError("Invalid timestamp; value omitted") from None
    if result.tzinfo is None:
        raise ValueError("Timestamp must include a timezone")
    return result.astimezone(UTC)


def _uuid(value: Any) -> str:
    try:
        return str(UUID(value))
    except (ValueError, TypeError, AttributeError):
        raise ValueError("Invalid Trace ID; value omitted") from None


def _object(path: Path) -> dict[str, Any]:
    if path.stat().st_size > MAX_BYTES:
        raise ValueError("Local input exceeds byte limit")
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("Local JSON must contain an object")
    return value


def _first_submission(path: Path) -> float | None:
    if not path.is_file():
        return None
    if path.stat().st_size > MAX_BYTES:
        raise ValueError("Local event input exceeds byte limit")
    values = []
    for line in path.read_text(encoding="utf-8").splitlines():
        row = json.loads(line)
        if not isinstance(row, dict):
            raise ValueError("Local event must contain an object")
        if (
            row.get("event") == "ToolResultStartEvent"
            and row.get("name") == "submit_ttp_template"
        ):
            elapsed = _number(row.get("elapsed_seconds"))
            if elapsed is not None:
                values.append(elapsed)
    return min(values, default=None)


def load_run(directory: Path) -> dict[str, Any]:
    """Read only metrics and identities; discard all other trial fields."""
    summary = _object(directory / "summary.json")
    if summary.get("mode") != "ttp-only" or summary.get("runner_version") != 5:
        raise ValueError("Comparison requires runner_version 5 TTP-only runs")
    paths = sorted(directory.glob("datasets/*/trials/trial-*.json"))
    if not 0 < len(paths) <= MAX_TRIALS or summary.get("trial_count") != len(paths):
        raise ValueError("Run is incomplete or exceeds the trial limit")
    trials = []
    identities: set[tuple[str, int]] = set()
    for path in paths:
        raw = _object(path)
        case = raw.get("case", {}).get("id")
        if not isinstance(case, str) or not re.fullmatch(
            r"[A-Za-z0-9_.-]{1,160}", case
        ):
            raise ValueError("Invalid case identity; value omitted")
        index = raw.get("trial_index")
        if type(index) is not int or not 0 <= index < MAX_TRIALS:
            raise ValueError("Invalid trial index")
        if (case, index) in identities:
            raise ValueError("Duplicate trial identity")
        identities.add((case, index))
        strict = raw.get("candidate_pass")
        if type(strict) is not bool:
            raise ValueError("Trial is missing its strict independent score")
        metrics = raw.get("metrics", {})
        if not isinstance(metrics, Mapping):
            raise ValueError("Invalid metrics object")
        facts = raw.get("execution_facts", {})
        facts = facts if isinstance(facts, Mapping) else {}
        row = {
            "case": case,
            "trial_index": index,
            "strict": strict,
            "started_at": _time(raw.get("started_at")).isoformat(),
            "finished_at": _time(raw.get("finished_at")).isoformat(),
            "trace_id": _uuid(raw["trace_id"]) if raw.get("trace_id") else None,
            "first_submission_seconds": _first_submission(
                path.with_suffix(".rounds.jsonl"),
            ),
            "metrics": {key: _number(metrics.get(key)) for key in LOCAL_METRICS},
            "funnel": {},
        }
        if _time(row["finished_at"]) < _time(row["started_at"]):
            raise ValueError("Trial finished before it started")
        for key in FUNNEL:
            metric = _number(metrics.get(key))
            row["funnel"][key] = (
                facts[key]
                if type(facts.get(key)) is bool
                else bool(metric)
                if metric in (0, 1)
                else None
            )
        trials.append(row)
    if summary.get("strict_pass_count") != sum(row["strict"] for row in trials):
        raise ValueError("Summary strict count disagrees with trial results")
    return {"trials": trials, "summary": summary}


def _aggregate(values: Sequence[float | None]) -> dict[str, Any]:
    observed = [value for value in values if value is not None]
    return {
        "observations": len(observed),
        "sum": sum(observed) if observed else None,
        "mean": sum(observed) / len(observed) if observed else None,
    }


def _rate(successes: int, observations: int) -> dict[str, Any]:
    return {
        "successes": successes,
        "observations": observations,
        "rate": successes / observations if observations else None,
    }


def summarize(trials: Sequence[dict[str, Any]]) -> dict[str, Any]:
    result = {
        "trials": len(trials),
        "strict": _rate(sum(row["strict"] for row in trials), len(trials)),
        "funnel": {
            key: _rate(
                sum(row["funnel"][key] is True for row in trials),
                sum(row["funnel"][key] is not None for row in trials),
            )
            for key in FUNNEL
        },
        "local_metrics": {
            key: _aggregate([row["metrics"][key] for row in trials])
            for key in LOCAL_METRICS
        },
        "first_submission_seconds": _aggregate(
            [row["first_submission_seconds"] for row in trials],
        ),
        "first_valid_candidate_seconds": _aggregate(
            [row.get("first_valid_candidate_seconds") for row in trials],
        ),
        "wall_seconds": (
            max(_time(row["finished_at"]) for row in trials)
            - min(_time(row["started_at"]) for row in trials)
        ).total_seconds(),
        "laminar": None,
    }
    observed = [row["laminar"] for row in trials if "laminar" in row]
    if observed:
        telemetry = {
            "observed_traces": len(observed),
            "llm_observed_traces": sum(bool(row.get("llm_calls")) for row in observed),
            "tool_observed_traces": sum("submissions" in row for row in observed),
            **{
                key: _aggregate([row.get(key) for row in observed])["sum"]
                for key in (*LLM_METRICS, *TOOL_METRICS)
            },
        }
        submissions = int(telemetry["submissions"] or 0)
        telemetry["submission_rates"] = {
            key: _rate(int(telemetry[key] or 0), submissions)
            for key in TOOL_METRICS
            if key.endswith("_submissions")
        }
        tests = int(telemetry["tests"] or 0)
        telemetry["test_rates"] = {
            key: _rate(int(telemetry[key] or 0), tests)
            for key in TOOL_METRICS
            if key.endswith("_tests")
        }
        telemetry["reasoning_output_ratio"] = (
            telemetry["reasoning_tokens"] / telemetry["output_tokens"]
            if telemetry["output_tokens"]
            and telemetry["reasoning_tokens"] is not None
            and telemetry["reasoning_observed_calls"] == telemetry["llm_calls"]
            else None
        )
        telemetry["reasoning_observation_rate"] = (
            telemetry["reasoning_observed_calls"] / telemetry["llm_calls"]
            if telemetry["llm_calls"]
            else None
        )
        telemetry["observed_traces_missing_llm"] = sum(
            not row.get("llm_calls") for row in observed
        )
        result["laminar"] = telemetry
    return result


def _queries() -> tuple[str, str]:
    where = (
        "WHERE start_time >= {since:DateTime} AND start_time <= {until:DateTime} "
        "AND trace_id IN {ids:Array(UUID)} "
    )
    llm = (
        """
        SELECT trace_id, countIf(span_type = 'LLM') AS llm_calls,
          sumIf(input_tokens, span_type = 'LLM') AS input_tokens,
          sumIf(output_tokens, span_type = 'LLM') AS output_tokens,
          sumIf(JSONExtractInt(attributes, 'gen_ai.usage.reasoning_tokens'),
            span_type = 'LLM') AS reasoning_tokens,
          countIf(span_type = 'LLM' AND
            JSONHas(attributes, 'gen_ai.usage.reasoning_tokens'))
            AS reasoning_observed_calls,
          sumIf(end_time-start_time, span_type = 'LLM') AS llm_seconds,
          sumIf(total_cost, span_type = 'LLM') AS recorded_cost_usd,
          countIf(name = 'context.fit') AS context_fit_calls,
          sumIf(end_time-start_time, name = 'context.fit') AS context_fit_seconds
        FROM spans
    """
        + where
        + "AND (span_type = 'LLM' OR name = 'context.fit') "
        "GROUP BY trace_id LIMIT 1001"
    )
    submit = "name = 'submit_ttp_template'"
    issue = "JSONExtractArrayRaw(output, 'issues')"
    select = [
        f"countIf({submit}) AS submissions",
        f"countIf({submit} AND JSONExtractBool(output, 'accepted')) "
        "AS accepted_submissions",
        "countIf(name = 'test_ttp_template') AS tests",
        "countIf(name = 'test_ttp_template' AND JSONExtractBool(output, 'accepted')) "
        "AS successful_tests",
        f"toUnixTimestamp64Milli(minIf(toDateTime64(end_time, 3), {submit} "
        "AND JSONExtractBool(output, 'accepted'))) / 1000 "
        "AS first_accepted_end_seconds",
    ]
    for tool, code, key in (
        (submit, "schema.record_mismatch", "schema_error_submissions"),
        (submit, "ttp.worker_error", "worker_error_submissions"),
        (submit, "ttp.incompatible_argument_pipe", "incompatible_pipe_submissions"),
        ("name = 'test_ttp_template'", "ttp.worker_error", "worker_error_tests"),
        (
            "name = 'test_ttp_template'",
            "ttp.incompatible_argument_pipe",
            "incompatible_pipe_tests",
        ),
    ):
        select.append(
            f"countIf({tool} AND arrayExists(i -> "
            f"JSONExtractString(i, 'code') = '{code}', {issue})) AS {key}",
        )
    for tool, key in (
        (submit, "system_exit_submissions"),
        ("name = 'test_ttp_template'", "system_exit_tests"),
    ):
        select.append(
            f"countIf({tool} AND arrayExists(i -> JSONExtractString(i, 'code') = "
            "'ttp.worker_error' AND JSONExtractString(i, 'details', 'exception_type') "
            f"= 'SystemExit', {issue})) AS {key}",
        )
    tools = (
        "SELECT trace_id, "
        + ", ".join(select)
        + " FROM spans "
        + where
        + "AND span_type = 'TOOL' AND name IN "
        "('submit_ttp_template', 'test_ttp_template') GROUP BY trace_id LIMIT 1001"
    )
    return llm, tools


def collect_laminar(
    trials: Sequence[dict[str, Any]],
    *,
    transport: httpx.BaseTransport | None = None,
) -> None:
    """Attach aggregate numbers only; never request a raw JSON/text column."""
    traced = {row["trace_id"]: row for row in trials if row["trace_id"] is not None}
    if len(traced) != sum(row["trace_id"] is not None for row in trials):
        raise ValueError("Duplicate Trace IDs across requested runs")
    if not traced:
        return
    base = urlsplit(os.environ.get("LMNR_BASE_URL", "https://api.lmnr.ai"))
    if base.scheme not in {"http", "https"} or not base.hostname or base.username:
        raise ValueError("Invalid Laminar endpoint configuration")
    netloc = base.netloc
    if "LMNR_HTTP_PORT" in os.environ:
        port = int(os.environ["LMNR_HTTP_PORT"])
        if not 1 <= port <= 65535:
            raise ValueError("Invalid Laminar port")
        host = f"[{base.hostname}]" if ":" in base.hostname else base.hostname
        netloc = f"{host}:{port}"
    url = urlunsplit((base.scheme, netloc, "/v1/sql/query", "", ""))
    key = os.environ.get("LMNR_PROJECT_API_KEY")
    if not key:
        raise ValueError("--laminar requires LMNR_PROJECT_API_KEY")
    for offset in range(0, len(traced), MAX_TRIALS):
        ids = list(traced)[offset : offset + MAX_TRIALS]
        rows = [traced[trace_id] for trace_id in ids]
        parameters = {
            "ids": ids,
            "since": (
                min(_time(row["started_at"]) for row in rows) - timedelta(minutes=2)
            ).strftime("%Y-%m-%d %H:%M:%S"),
            "until": (
                max(_time(row["finished_at"]) for row in rows) + timedelta(minutes=2)
            ).strftime("%Y-%m-%d %H:%M:%S"),
        }
        with httpx.Client(timeout=30, transport=transport) as client:
            for query, columns in zip(
                _queries(), (LLM_METRICS, TOOL_METRICS), strict=True
            ):
                response = client.post(
                    url,
                    headers={"Authorization": "Bearer " + key},
                    json={"query": query, "parameters": parameters},
                )
                if response.status_code != 200:
                    raise ValueError("Laminar SQL failed; response body omitted")
                if len(response.content) > MAX_BYTES:
                    raise ValueError("Laminar projection exceeds byte limit")
                data = response.json().get("data")
                if not isinstance(data, list) or len(data) > MAX_TRIALS:
                    raise ValueError("Invalid Laminar aggregate row count")
                seen = set()
                for item in data:
                    if not isinstance(item, dict):
                        raise ValueError("Invalid Laminar aggregate object")
                    trace_id = _uuid(item.get("trace_id"))
                    if trace_id not in ids or trace_id in seen:
                        raise ValueError("Unexpected or duplicate Laminar Trace ID")
                    seen.add(trace_id)
                    row = traced[trace_id]
                    telemetry = row.setdefault("laminar", {})
                    for column in columns:
                        value = _number(item.get(column))
                        if value is None or (
                            column
                            not in {
                                "llm_seconds",
                                "recorded_cost_usd",
                                "context_fit_seconds",
                            }
                            and not value.is_integer()
                        ):
                            raise ValueError("Invalid Laminar numeric projection")
                        telemetry[column] = value
                    if columns == LLM_METRICS:
                        if (
                            telemetry["reasoning_observed_calls"]
                            > telemetry["llm_calls"]
                        ):
                            raise ValueError("Reasoning observations exceed LLM calls")
                        if not telemetry["reasoning_observed_calls"]:
                            telemetry["reasoning_tokens"] = None
                        if not telemetry["llm_calls"]:
                            for column in (
                                "input_tokens",
                                "output_tokens",
                                "llm_seconds",
                                "recorded_cost_usd",
                            ):
                                telemetry[column] = None
                    if columns == TOOL_METRICS and telemetry["accepted_submissions"]:
                        seconds = _number(item.get("first_accepted_end_seconds"))
                        if seconds is None:
                            raise ValueError("Missing first accepted timestamp")
                        elapsed = seconds - _time(row["started_at"]).timestamp()
                        if elapsed < 0:
                            raise ValueError("Accepted candidate predates trial")
                        row["first_valid_candidate_seconds"] = elapsed


def _clean_config(value: Any, section: str) -> Any:
    if not isinstance(value, Mapping):
        return value
    return {
        key: item
        for key, item in value.items()
        if key not in LEGACY_HASH_FIELDS.get(section, set())
    }


def compare(directories: Sequence[Path], *, laminar: bool = False) -> dict[str, Any]:
    if not 2 <= len(directories) <= 100:
        raise ValueError("Provide between two and one hundred run directories")
    runs = [load_run(directory) for directory in directories]
    if laminar:
        collect_laminar([trial for run in runs for trial in run["trials"]])
    baseline = runs[0]["summary"]
    output = {"comparison_version": 1, "laminar_requested": laminar, "runs": []}
    for index, (directory, run) in enumerate(zip(directories, runs, strict=True)):
        summary = run["summary"]
        config = summary.get("configuration", {})
        base_config = baseline.get("configuration", {})
        label = directory.name
        if not re.fullmatch(r"[A-Za-z0-9_.-]{1,100}", label):
            label = f"run-{index + 1}"
        output["runs"].append(
            {
                "label": label,
                "comparison_to_first": {
                    "same_selected_inputs": summary.get("selected_inputs")
                    == baseline.get("selected_inputs"),
                    "same_case_trial_counts": Counter(
                        row["case"] for row in run["trials"]
                    )
                    == Counter(row["case"] for row in runs[0]["trials"]),
                    **{
                        f"same_{section}": _clean_config(config.get(section), section)
                        == _clean_config(base_config.get(section), section)
                        for section in ("model", "policy", "prompt")
                    },
                },
                "overall": summarize(run["trials"]),
                "cases": {
                    case: summarize(
                        [row for row in run["trials"] if row["case"] == case]
                    )
                    for case in sorted({row["case"] for row in run["trials"]})
                },
                "trials": run["trials"],
            }
        )
    return output


def render(report: Mapping[str, Any]) -> str:
    lines = [
        "# Accuracy Run Comparison",
        "",
        "Strict scores come from local independent evaluation. Missing telemetry "
        "is shown as n/a; observed denominators are explicit.",
        "",
    ]
    for run in report["runs"]:
        lines.extend(
            [
                f"## {run['label']}",
                "",
                "| Case | Strict | Valid Candidate | Finish Succeeded | Mean Seconds |",
                "| --- | --- | --- | --- | --- |",
            ]
        )
        for case, metrics in [("overall", run["overall"]), *run["cases"].items()]:
            fractions = [
                metrics["strict"],
                metrics["funnel"]["valid_ttp_candidate"],
                metrics["funnel"]["finish_succeeded"],
            ]
            cells = [
                f"{value['successes']}/{value['observations']}" for value in fractions
            ]
            seconds = metrics["local_metrics"]["elapsed_seconds"]["mean"]
            display = f"{seconds:.2f}" if seconds is not None else "n/a"
            lines.append(f"| {case} | {' | '.join(cells)} | {display} |")
        metrics = run["overall"]
        for key in ("first_submission_seconds", "first_valid_candidate_seconds"):
            value = metrics[key]
            display = f"{value['mean']:.2f}" if value["mean"] is not None else "n/a"
            lines.append(f"\n{key}: {display}; observations={value['observations']}")
        telemetry = metrics["laminar"]
        if telemetry:
            lines.append(
                "\nLaminar aggregates: " + json.dumps(telemetry, sort_keys=True)
            )
        lines.append(
            "\nConfiguration checks against first run: "
            + json.dumps(run["comparison_to_first"], sort_keys=True)
            + "\n"
        )
    return "\n".join(lines)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("runs", type=Path, nargs="+")
    parser.add_argument("--laminar", action="store_true")
    parser.add_argument("--json-output", type=Path)
    parser.add_argument("--markdown-output", type=Path)
    args = parser.parse_args(argv)
    try:
        report = compare(args.runs, laminar=args.laminar)
        markdown = render(report)
        for path, text in (
            (args.json_output, json.dumps(report, indent=2, ensure_ascii=True)),
            (args.markdown_output, markdown),
        ):
            if path is not None:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(text + "\n", encoding="utf-8")
    except (OSError, ValueError, TypeError, KeyError, httpx.HTTPError):
        parser.exit(
            2,
            "Comparison failed: invalid/incomplete input or unavailable "
            "Laminar projection; values and response bodies omitted.\n",
        )
    print(markdown)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
