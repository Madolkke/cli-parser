"""Offline and privacy regression tests for multi-stage accuracy comparison."""

from __future__ import annotations

import importlib.util
import json
from datetime import datetime
from pathlib import Path
from types import ModuleType

import httpx
import pytest

TRACE_ID = "430b0f6f-98f1-216b-9ce8-613073377f1b"
START = "2026-09-06T13:00:00+00:00"


def _load() -> ModuleType:
    path = Path(__file__).resolve().parents[2] / "scripts" / "compare_accuracy_runs.py"
    spec = importlib.util.spec_from_file_location("compare_accuracy_runs", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _run(directory: Path, *, passed: bool = False, metrics: dict | None = None) -> Path:
    trials = directory / "datasets" / "example.case" / "trials"
    trials.mkdir(parents=True)
    trial = {
        "candidate_pass": passed,
        "trial_index": 0,
        "case": {"id": "example.case"},
        "started_at": START,
        "finished_at": "2026-09-06T13:10:00+00:00",
        "metrics": metrics or {"elapsed_seconds": 600},
        "trace_id": TRACE_ID,
        "input": "SENSITIVE_INPUT",
        "records": {"SENSITIVE_FIELD": "SENSITIVE_VALUE"},
        "ttp_template": "SENSITIVE_TEMPLATE",
    }
    (trials / "trial-01.json").write_text(json.dumps(trial), encoding="utf-8")
    summary = {
        "runner_version": 5,
        "mode": "ttp-only",
        "trial_count": 1,
        "strict_pass_count": int(passed),
        "selected_inputs": [{"dataset": "example.case", "selected_input": "001.txt"}],
        "configuration": {"model": {"name": "test"}, "policy": {}, "prompt": {}},
    }
    (directory / "summary.json").write_text(json.dumps(summary), encoding="utf-8")
    return directory


def test_local_comparison_is_offline_and_discards_nonmetric_fields(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _load()
    monkeypatch.setattr(httpx.Client, "post", lambda *a, **kw: pytest.fail("network"))
    before = _run(tmp_path / "before")
    after = _run(tmp_path / "after", passed=True)

    report = module.compare([before, after])

    assert report["runs"][0]["overall"]["strict"]["rate"] == 0
    assert report["runs"][1]["overall"]["strict"]["rate"] == 1
    assert "SENSITIVE" not in json.dumps(report)
    assert report["runs"][0]["overall"]["laminar"] is None
    assert "n/a" in module.render(report)


def test_missing_funnel_observations_are_not_reported_as_failures(
    tmp_path: Path,
) -> None:
    module = _load()
    run = module.load_run(_run(tmp_path / "run"))
    metrics = module.summarize(run["trials"])

    assert metrics["funnel"]["valid_ttp_candidate"] == {
        "successes": 0,
        "observations": 0,
        "rate": None,
    }
    assert metrics["first_valid_candidate_seconds"]["mean"] is None
    assert metrics["local_metrics"]["input_tokens_total"]["sum"] is None


def test_rounds_supply_first_full_submission_without_acceptance_inference(
    tmp_path: Path,
) -> None:
    module = _load()
    directory = _run(tmp_path / "run", metrics={"valid_ttp_candidate": 1.0})
    path = directory / "datasets/example.case/trials/trial-01.rounds.jsonl"
    path.write_text(
        "\n".join(
            json.dumps(row)
            for row in [
                {
                    "event": "ToolResultStartEvent",
                    "name": "test_ttp_template",
                    "elapsed_seconds": 10,
                },
                {
                    "event": "ToolResultStartEvent",
                    "name": "submit_ttp_template",
                    "elapsed_seconds": 20,
                    "body": "SENSITIVE",
                },
                {
                    "event": "ToolResultStartEvent",
                    "name": "submit_ttp_template",
                    "elapsed_seconds": 50,
                },
            ]
        ),
        encoding="utf-8",
    )

    metrics = module.summarize(module.load_run(directory)["trials"])

    assert metrics["first_submission_seconds"]["mean"] == 20
    assert metrics["first_valid_candidate_seconds"]["mean"] is None
    assert metrics["funnel"]["valid_ttp_candidate"]["successes"] == 1


def test_incomplete_or_inconsistent_runs_are_rejected(tmp_path: Path) -> None:
    module = _load()
    directory = _run(tmp_path / "run")
    summary_path = directory / "summary.json"
    summary = json.loads(summary_path.read_text())
    summary["strict_pass_count"] = 1
    summary_path.write_text(json.dumps(summary))
    with pytest.raises(ValueError, match="strict count"):
        module.load_run(directory)
    summary["trial_count"] = 2
    summary_path.write_text(json.dumps(summary))
    with pytest.raises(ValueError, match="incomplete"):
        module.load_run(directory)


def test_configuration_and_selection_drift_is_visible(tmp_path: Path) -> None:
    module = _load()
    before = _run(tmp_path / "before")
    after = _run(tmp_path / "after")
    path = after / "summary.json"
    summary = json.loads(path.read_text())
    summary["configuration"]["policy"]["max_agent_rounds"] = 99
    summary["configuration"]["prompt"]["ttp_system_sha256"] = "historical"
    summary["selected_inputs"] = []
    path.write_text(json.dumps(summary))

    checks = module.compare([before, after])["runs"][1]["comparison_to_first"]

    assert checks["same_policy"] is False
    assert checks["same_prompt"] is True
    assert checks["same_selected_inputs"] is False


def _environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LMNR_PROJECT_API_KEY", "test-secret")
    monkeypatch.setenv("LMNR_BASE_URL", "http://localhost")
    monkeypatch.setenv("LMNR_HTTP_PORT", "8000")


def test_laminar_queries_only_aggregate_numbers_and_use_submission_denominators(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _load()
    _environment(monkeypatch)
    trials = module.load_run(_run(tmp_path / "run"))["trials"]
    seen = []

    def respond(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        query = payload["query"]
        seen.append(query)
        assert payload["parameters"]["ids"] == [TRACE_ID]
        assert "start_time >= {since:DateTime}" in query
        assert "GROUP BY trace_id" in query
        assert "SELECT *" not in query
        assert ", input," not in query and ", output," not in query
        if "span_type = 'LLM'" in query:
            row = dict.fromkeys(module.LLM_METRICS, 0)
            row.update(
                llm_calls=5,
                input_tokens=100,
                output_tokens=80,
                reasoning_tokens=70,
                reasoning_observed_calls=5,
                llm_seconds=200,
                context_fit_calls=2,
                context_fit_seconds=0.25,
            )
        else:
            row = dict.fromkeys(module.TOOL_METRICS, 0)
            row.update(
                submissions=4,
                accepted_submissions=1,
                schema_error_submissions=2,
                worker_error_submissions=1,
                system_exit_submissions=1,
                tests=8,
                successful_tests=3,
                worker_error_tests=1,
                system_exit_tests=1,
                incompatible_pipe_tests=4,
                first_accepted_end_seconds=datetime.fromisoformat(START).timestamp()
                + 250,
            )
        return httpx.Response(
            200, json={"data": [{"trace_id": TRACE_ID, "body": "SENSITIVE", **row}]}
        )

    module.collect_laminar(trials, transport=httpx.MockTransport(respond))
    summary = module.summarize(trials)

    assert len(seen) == 2
    assert "SENSITIVE" not in json.dumps(trials)
    assert summary["first_valid_candidate_seconds"]["mean"] == 250
    telemetry = summary["laminar"]
    assert telemetry["submission_rates"]["schema_error_submissions"] == {
        "successes": 2,
        "observations": 4,
        "rate": 0.5,
    }
    assert telemetry["test_rates"]["incompatible_pipe_tests"] == {
        "successes": 4,
        "observations": 8,
        "rate": 0.5,
    }
    assert telemetry["test_rates"]["worker_error_tests"]["rate"] == 1 / 8
    assert telemetry["test_rates"]["system_exit_tests"]["rate"] == 1 / 8
    assert telemetry["test_rates"]["successful_tests"]["rate"] == 3 / 8
    for column in (
        "worker_error_tests",
        "system_exit_tests",
        "incompatible_pipe_tests",
    ):
        assert f"AS {column}" in seen[1]
    assert telemetry["reasoning_output_ratio"] == 70 / 80
    assert telemetry["reasoning_observation_rate"] == 1
    assert telemetry["context_fit_calls"] == 2
    assert telemetry["context_fit_seconds"] == 0.25
    assert "countIf(name = 'context.fit')" in seen[0]
    assert "JSONHas(attributes, 'gen_ai.usage.reasoning_tokens')" in seen[0]


@pytest.mark.parametrize("observations", [0, 2])
def test_unobserved_reasoning_is_not_a_complete_zero_sum(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    observations: int,
) -> None:
    module = _load()
    _environment(monkeypatch)
    trials = module.load_run(_run(tmp_path / "run"))["trials"]

    def respond(request: httpx.Request) -> httpx.Response:
        query = json.loads(request.content)["query"]
        row = {"trace_id": TRACE_ID, **dict.fromkeys(module.LLM_METRICS, 0)}
        row.update(
            llm_calls=5,
            output_tokens=50,
            reasoning_tokens=observations * 10,
            reasoning_observed_calls=observations,
        )
        data = [row] if "span_type = 'LLM'" in query else []
        return httpx.Response(200, json={"data": data})

    module.collect_laminar(trials, transport=httpx.MockTransport(respond))
    telemetry = module.summarize(trials)["laminar"]

    assert telemetry["reasoning_tokens"] == (None if observations == 0 else 20)
    assert telemetry["reasoning_observation_rate"] == observations / 5
    assert telemetry["reasoning_output_ratio"] is None


def test_context_only_trace_does_not_imply_zero_llm_usage(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _load()
    _environment(monkeypatch)
    trials = module.load_run(_run(tmp_path / "run"))["trials"]

    def respond(request: httpx.Request) -> httpx.Response:
        query = json.loads(request.content)["query"]
        row = {"trace_id": TRACE_ID, **dict.fromkeys(module.LLM_METRICS, 0)}
        row.update(context_fit_calls=1, context_fit_seconds=0.5)
        data = [row] if "span_type = 'LLM'" in query else []
        return httpx.Response(200, json={"data": data})

    module.collect_laminar(trials, transport=httpx.MockTransport(respond))
    telemetry = module.summarize(trials)["laminar"]

    assert telemetry["input_tokens"] is None
    assert telemetry["reasoning_tokens"] is None
    assert telemetry["llm_observed_traces"] == 0
    assert telemetry["context_fit_calls"] == 1


def test_missing_laminar_rows_do_not_become_zero_usage(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _load()
    _environment(monkeypatch)
    trials = module.load_run(_run(tmp_path / "run"))["trials"]

    def respond(request: httpx.Request) -> httpx.Response:
        query = json.loads(request.content)["query"]
        data = (
            []
            if "span_type = 'LLM'" in query
            else [
                {"trace_id": TRACE_ID, **dict.fromkeys(module.TOOL_METRICS, 0)},
            ]
        )
        return httpx.Response(200, json={"data": data})

    module.collect_laminar(trials, transport=httpx.MockTransport(respond))
    telemetry = module.summarize(trials)["laminar"]

    assert telemetry["input_tokens"] is None
    assert telemetry["llm_observed_traces"] == 0
    assert telemetry["tool_observed_traces"] == 1


@pytest.mark.parametrize("value", ["SENSITIVE", True, -1, float("inf"), 1.5])
def test_untrusted_laminar_numeric_projection_is_rejected(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    value,
) -> None:
    module = _load()
    _environment(monkeypatch)
    trials = module.load_run(_run(tmp_path / "run"))["trials"]
    row = {
        "trace_id": TRACE_ID,
        **dict.fromkeys(module.LLM_METRICS, 0),
        "llm_calls": value,
    }
    response_text = json.dumps({"data": [row]})
    transport = httpx.MockTransport(
        lambda request: httpx.Response(
            200,
            text=response_text,
            headers={"content-type": "application/json"},
        )
    )

    with pytest.raises(ValueError, match="numeric projection"):
        module.collect_laminar(trials, transport=transport)


def test_cli_saves_only_projected_json_and_markdown(tmp_path: Path) -> None:
    module = _load()
    before = _run(tmp_path / "before")
    after = _run(tmp_path / "after", passed=True)
    output = tmp_path / "report.json"
    markdown = tmp_path / "report.md"

    assert (
        module.main(
            [
                str(before),
                str(after),
                "--json-output",
                str(output),
                "--markdown-output",
                str(markdown),
            ]
        )
        == 0
    )
    assert "SENSITIVE" not in output.read_text()
    assert "SENSITIVE" not in markdown.read_text()
