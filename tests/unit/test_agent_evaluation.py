"""Deterministic tests for the black-box Agent evaluation harness."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import ssl
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
from lmnr import Laminar

from cli_parser_agent.evaluation import (
    HarnessError,
    load_evaluation_manifest,
    score_executor_output,
    select_cases,
)

PROJECT_ROOT = Path(__file__).resolve().parents[2]
MANIFEST_PATH = PROJECT_ROOT / "evals" / "ttp_generation" / "manifest.json"
SCRIPT_PATH = PROJECT_ROOT / "scripts" / "run_agent_evaluation.py"


def _load_script() -> ModuleType:
    spec = importlib.util.spec_from_file_location("run_agent_evaluation", SCRIPT_PATH)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _replace_target(root: Path, value: dict[str, Any]) -> Path:
    target = root / "target.json"
    target.write_text(json.dumps(value), encoding="utf-8", newline="\n")
    manifest = root / "manifest.json"
    manifest_value = json.loads(manifest.read_text(encoding="utf-8"))
    manifest_value["cases"][0]["target"]["sha256"] = _sha256(target)
    manifest.write_text(json.dumps(manifest_value), encoding="utf-8", newline="\n")
    return manifest


def _write_minimal_definition(root: Path) -> Path:
    source = root / "input.txt"
    source.write_text("Value: one\n", encoding="utf-8", newline="\n")
    target = root / "target.json"
    target.write_text(
        json.dumps(
            {
                "records": [{"value": "one"}],
                "schema_contract": [
                    {"path": "/", "type": "object", "required": False},
                    {"path": "/value", "type": "string", "required": True},
                ],
            },
        ),
        encoding="utf-8",
        newline="\n",
    )
    manifest = root / "manifest.json"
    manifest.write_text(
        json.dumps(
            {
                "version": 1,
                "cases": [
                    {
                        "id": "test.value",
                        "command": "show value",
                        "suites": ["smoke"],
                        "tags": ["line"],
                        "inputs": [
                            {"path": "input.txt", "sha256": _sha256(source)},
                        ],
                        "target": {
                            "path": "target.json",
                            "sha256": _sha256(target),
                        },
                    },
                ],
            },
        ),
        encoding="utf-8",
        newline="\n",
    )
    return manifest


def _schema(
    *,
    required: list[str] | None = None,
) -> dict[str, Any]:
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "type": "object",
        "properties": {
            "interfaces": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "port": {"type": "string"},
                        "status": {"type": "string"},
                    },
                    "required": ["port", "status"],
                    "additionalProperties": False,
                },
            },
        },
        "required": ["interfaces"] if required is None else required,
        "additionalProperties": False,
    }


def _contract() -> list[dict[str, Any]]:
    return [
        {"path": "/", "type": "object", "required": False},
        {"path": "/interfaces", "type": "array", "required": True},
        {"path": "/interfaces/*", "type": "object", "required": False},
        {"path": "/interfaces/*/port", "type": "string", "required": True},
        {"path": "/interfaces/*/status", "type": "string", "required": True},
    ]


def _output(records: list[dict[str, Any]], *, schema: dict[str, Any] | None = None):
    return {
        "trial_key": "test#0",
        "generation_result": {
            "status": "success",
            "artifact": {
                "ttp_template": "irrelevant",
                "result_schema": schema or _schema(),
                "records": records,
                "assumptions": [],
            },
            "issues": [],
            "metadata": {
                "termination_reason": "success",
                "first_ttp_passed": True,
                "elapsed_seconds": 1.5,
                "agent_rounds": 3,
                "schema_agent_rounds": 1,
                "ttp_agent_rounds": 2,
                "tool_call_starts": 3,
                "tool_result_errors": 0,
                "schema_submissions": 1,
                "ttp_submissions": 1,
                "schema_no_tool_responses": 0,
                "ttp_no_tool_responses": 0,
                "schema_no_tool_retries": 0,
                "ttp_no_tool_retries": 0,
            },
            "last_attempt": None,
        },
        "independent_acceptance": {"valid": True, "issue_codes": []},
    }


def _target(records: list[dict[str, Any]]) -> dict[str, Any]:
    return {"records": records, "schema_contract": _contract()}


def _evaluation_environment() -> dict[str, str]:
    return {
        "OPENAI_API_KEY": "local-openai-test-key",
        "OPENAI_MODEL": "test-model",
        "LMNR_PROJECT_API_KEY": "local-laminar-test-key",
        "LMNR_BASE_URL": "http://127.0.0.1",
        "LMNR_HTTP_PORT": "8000",
        "LMNR_GRPC_PORT": "8001",
        "LMNR_FRONTEND_PORT": "5667",
    }


def test_versioned_manifest_preflights_smoke_and_baseline_suites() -> None:
    manifest = load_evaluation_manifest(PROJECT_ROOT, MANIFEST_PATH)
    smoke = select_cases(manifest, suite="smoke", case_ids=())
    baseline = select_cases(manifest, suite="baseline", case_ids=())

    assert len(manifest.cases) == 8
    assert sum(len(case.inputs) for case in manifest.cases) == 15
    assert len(smoke) == 5
    assert sum(len(case.inputs) for case in smoke) == 12
    assert [case.id for case in baseline] == [
        "ntc.cisco_ios.show_ip_interface_brief",
        "ttp.cisco_ios.show_inventory.single_basic",
        "ttp.cisco_ios.show_running_config_pipe_section_interface.single_qinq",
    ]
    assert sum(len(case.inputs) for case in baseline) == 3
    assert all(len(case.inputs) == 1 for case in baseline)
    assert all(
        {"baseline", "single-input"}.issubset(case.tags)
        for case in baseline
    )


def test_sql_query_uses_an_unverified_context_only_when_explicitly_enabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    script = _load_script()
    _, _, runtime, _ = script._configuration(_evaluation_environment())
    monkeypatch.setenv("CLI_PARSER_INSECURE_SKIP_TLS_VERIFY", "1")
    captured: dict[str, Any] = {}

    class Response:
        def __enter__(self) -> Response:
            return self

        def __exit__(self, *_: object) -> None:
            return None

        def read(self) -> bytes:
            return b'{"data":[{"ok":1}]}'

    def open_url(*_: object, **kwargs: Any) -> Response:
        captured.update(kwargs)
        return Response()

    monkeypatch.setattr(script.urllib.request, "urlopen", open_url)

    assert script._sql_query(runtime, "SELECT 1 AS ok", {}) == [{"ok": 1}]
    context = captured["context"]
    assert context.check_hostname is False
    assert context.verify_mode is ssl.CERT_NONE


def test_manifest_rejects_path_traversal(tmp_path: Path) -> None:
    manifest_path = _write_minimal_definition(tmp_path)
    data = json.loads(manifest_path.read_text(encoding="utf-8"))
    data["cases"][0]["inputs"][0]["path"] = "../outside.txt"
    manifest_path.write_text(json.dumps(data), encoding="utf-8")

    with pytest.raises(HarnessError, match="traversal-free"):
        load_evaluation_manifest(tmp_path, manifest_path)


def test_manifest_rejects_target_hash_drift(tmp_path: Path) -> None:
    manifest_path = _write_minimal_definition(tmp_path)
    data = json.loads(manifest_path.read_text(encoding="utf-8"))
    data["cases"][0]["target"]["sha256"] = "0" * 64
    manifest_path.write_text(json.dumps(data), encoding="utf-8")

    with pytest.raises(HarnessError, match="SHA-256 does not match"):
        load_evaluation_manifest(tmp_path, manifest_path)


def test_manifest_rejects_empty_target_record(tmp_path: Path) -> None:
    _write_minimal_definition(tmp_path)
    manifest_path = _replace_target(
        tmp_path,
        {
            "records": [{}],
            "schema_contract": [
                {"path": "/", "type": "object", "required": False},
            ],
        },
    )

    with pytest.raises(HarnessError, match="empty object"):
        load_evaluation_manifest(tmp_path, manifest_path)


def test_manifest_rejects_unclosed_schema_contract(tmp_path: Path) -> None:
    _write_minimal_definition(tmp_path)
    manifest_path = _replace_target(
        tmp_path,
        {
            "records": [{"value": "one"}],
            "schema_contract": [
                {"path": "/", "type": "object", "required": False},
            ],
        },
    )

    with pytest.raises(HarnessError, match="undeclared path"):
        load_evaluation_manifest(tmp_path, manifest_path)


def test_manifest_accepts_optional_properties_in_heterogeneous_records(
    tmp_path: Path,
) -> None:
    _write_minimal_definition(tmp_path)
    manifest_path = _replace_target(
        tmp_path,
        {
            "records": [
                {
                    "items": [
                        {"name": "one", "detail": "one"},
                        {"name": "one"},
                    ],
                },
            ],
            "schema_contract": [
                {"path": "/", "type": "object", "required": False},
                {"path": "/items", "type": "array", "required": True},
                {"path": "/items/*", "type": "object", "required": False},
                {"path": "/items/*/name", "type": "string", "required": True},
                {
                    "path": "/items/*/detail",
                    "type": "string",
                    "required": False,
                },
            ],
        },
    )

    manifest = load_evaluation_manifest(tmp_path, manifest_path)

    assert manifest.cases[0].target.records[0]["items"][1] == {"name": "one"}


@pytest.mark.parametrize(
    ("records", "contract", "message"),
    [
        (
            [{"items": [{"detail": "present"}]}],
            [
                {"path": "/", "type": "object", "required": False},
                {"path": "/items", "type": "array", "required": True},
                {"path": "/items/*", "type": "object", "required": False},
                {"path": "/items/*/name", "type": "string", "required": True},
                {
                    "path": "/items/*/detail",
                    "type": "string",
                    "required": False,
                },
            ],
            "missing a required path",
        ),
        (
            [{"items": [{"name": "one"}]}],
            [
                {"path": "/", "type": "object", "required": False},
                {"path": "/items", "type": "array", "required": True},
                {"path": "/items/*", "type": "object", "required": False},
                {"path": "/items/*/name", "type": "string", "required": True},
                {
                    "path": "/items/*/detail",
                    "type": "string",
                    "required": False,
                },
            ],
            "paths absent from expected records",
        ),
        (
            [{"items": [{"name": "one", "unknown": "value"}]}],
            [
                {"path": "/", "type": "object", "required": False},
                {"path": "/items", "type": "array", "required": True},
                {"path": "/items/*", "type": "object", "required": False},
                {"path": "/items/*/name", "type": "string", "required": True},
            ],
            "undeclared path",
        ),
        (
            [{"items": [{"name": 1}]}],
            [
                {"path": "/", "type": "object", "required": False},
                {"path": "/items", "type": "array", "required": True},
                {"path": "/items/*", "type": "object", "required": False},
                {"path": "/items/*/name", "type": "string", "required": True},
            ],
            "type does not match",
        ),
    ],
)
def test_manifest_rejects_invalid_optional_contracts(
    tmp_path: Path,
    records: list[dict[str, Any]],
    contract: list[dict[str, Any]],
    message: str,
) -> None:
    _write_minimal_definition(tmp_path)
    manifest_path = _replace_target(
        tmp_path,
        {"records": records, "schema_contract": contract},
    )

    with pytest.raises(HarnessError, match=message):
        load_evaluation_manifest(tmp_path, manifest_path)


def test_exact_records_and_schema_contract_pass() -> None:
    records = [
        {
            "interfaces": [
                {"port": "Gi1", "status": "connected"},
                {"port": "Gi2", "status": "notconnect"},
            ],
        },
    ]

    scores = score_executor_output(_output(records), _target(records))

    assert scores["candidate_pass"] == 1.0
    assert scores["records_exact_match"] == 1.0
    assert scores["schema_contract_match"] == 1.0
    assert scores["leaf_f1"] == 1.0


def test_mechanical_acceptance_does_not_hide_header_and_missing_row() -> None:
    expected = [
        {
            "interfaces": [
                {"port": "Gi1", "status": "connected"},
                {"port": "Gi2", "status": "notconnect"},
            ],
        },
    ]
    actual = [
        {
            "interfaces": [
                {"port": "Port", "status": "Status"},
                {"port": "Gi1", "status": "connected"},
            ],
        },
    ]

    scores = score_executor_output(_output(actual), _target(expected))

    assert scores["independent_acceptance"] == 1.0
    assert scores["candidate_pass"] == 0.0
    assert scores["records_exact_match"] == 0.0
    assert scores["leaf_precision"] < 1.0
    assert scores["leaf_recall"] < 1.0


def test_empty_capture_cannot_pass_nonempty_target() -> None:
    expected = [
        {"interfaces": [{"port": "Gi1", "status": "connected"}]},
    ]
    actual = [{"interfaces": []}]

    scores = score_executor_output(_output(actual), _target(expected))

    assert scores["candidate_pass"] == 0.0
    assert scores["records_exact_match"] == 0.0
    assert scores["leaf_recall"] == 0.0


def test_array_order_and_scalar_type_are_strict() -> None:
    expected = [
        {
            "interfaces": [
                {"port": "1", "status": "up"},
                {"port": "2", "status": "down"},
            ],
        },
    ]
    reversed_records = [
        {"interfaces": list(reversed(expected[0]["interfaces"]))},
    ]
    typed_records = [
        {
            "interfaces": [
                {"port": 1, "status": "up"},
                {"port": "2", "status": "down"},
            ],
        },
    ]

    assert (
        score_executor_output(_output(reversed_records), _target(expected))[
            "records_exact_match"
        ]
        == 0.0
    )
    assert (
        score_executor_output(_output(typed_records), _target(expected))[
            "records_exact_match"
        ]
        == 0.0
    )


def test_schema_optionality_mismatch_fails_contract() -> None:
    records = [
        {"interfaces": [{"port": "Gi1", "status": "connected"}]},
    ]

    scores = score_executor_output(
        _output(records, schema=_schema(required=[])),
        _target(records),
    )

    assert scores["records_exact_match"] == 1.0
    assert scores["schema_contract_match"] == 0.0
    assert scores["schema_path_recall"] < 1.0
    assert scores["candidate_pass"] == 0.0


def test_optional_property_participates_in_strict_schema_scoring() -> None:
    records = [
        {
            "interfaces": [
                {"port": "Gi1", "status": "connected"},
                {"port": "Gi2"},
            ],
        },
    ]
    optional_schema = _schema()
    optional_schema["properties"]["interfaces"]["items"]["required"] = ["port"]
    optional_contract = _contract()
    optional_contract[-1] = {
        "path": "/interfaces/*/status",
        "type": "string",
        "required": False,
    }
    target = {"records": records, "schema_contract": optional_contract}

    matching = score_executor_output(
        _output(records, schema=optional_schema),
        target,
    )
    required = score_executor_output(_output(records), target)

    assert matching["schema_contract_match"] == 1.0
    assert matching["candidate_pass"] == 1.0
    assert required["schema_contract_match"] == 0.0
    assert required["candidate_pass"] == 0.0


@pytest.mark.parametrize("argv", [["list"], ["preflight"]])
def test_local_commands_do_not_initialize_laminar(
    monkeypatch: pytest.MonkeyPatch,
    argv: list[str],
) -> None:
    script = _load_script()
    monkeypatch.setattr(
        Laminar,
        "initialize",
        lambda **_: pytest.fail("local commands must not initialize Laminar"),
    )

    assert script.main(argv) == 0


def test_key_placeholders_fail_before_laminar_preflight(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    script = _load_script()
    monkeypatch.setattr(
        script,
        "_preflight_laminar",
        lambda _: pytest.fail("missing keys must fail before networking"),
    )

    assert script.main(["run", "--case", "ntc.cisco_ios.show_interfaces_status"]) == 2


def test_key_values_are_excluded_from_configuration_snapshot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    script = _load_script()
    openai_key = "local-openai-test-key"
    laminar_key = "local-laminar-test-key"
    environment = _evaluation_environment()
    environment["OPENAI_API_KEY"] = openai_key
    environment["LMNR_PROJECT_API_KEY"] = laminar_key

    _, _, _, snapshot = script._configuration(environment)
    encoded = json.dumps(snapshot)

    assert openai_key not in encoded
    assert laminar_key not in encoded


def test_configuration_snapshot_includes_reasoning_and_tls_settings() -> None:
    script = _load_script()
    environment = _evaluation_environment()
    environment.update(
        {
            "CLI_PARSER_MODEL_THINKING_ENABLE": "true",
            "CLI_PARSER_MODEL_REASONING_EFFORT": "high",
            "CLI_PARSER_INSECURE_SKIP_TLS_VERIFY": "1",
        },
    )

    _, _, _, snapshot = script._configuration(environment)

    assert snapshot["model"]["thinking_enable"] is True
    assert snapshot["model"]["reasoning_effort"] == "high"
    assert snapshot["model"]["verify_tls"] is False


def test_telemetry_requires_conditional_phase_spans() -> None:
    script = _load_script()
    complete = {
        "evaluation_span_count": 1,
        "executor_span_count": 1,
        "generation_span_count": 1,
        "schema_phase_count": 1,
        "llm_call_count": 1,
        "entered_ttp": True,
        "ttp_phase_count": 0,
    }

    assert script._telemetry_complete(complete) is False
    complete["ttp_phase_count"] = 1
    complete["finish_called"] = True
    complete["final_acceptance_span_count"] = 0
    assert script._telemetry_complete(complete) is False
    complete["final_acceptance_span_count"] = 1
    assert script._telemetry_complete(complete) is True


def test_strict_pass_depends_only_on_deterministic_candidate_score() -> None:
    script = _load_script()

    assert script._strict_pass(True) is True
    assert script._strict_pass(False) is False
    assert script._strict_pass(None) is False


def test_review_dimensions_are_bounded_and_review_validation_is_local() -> None:
    script = _load_script()

    assert script._parse_review_dimensions(
        ["boundary=good", "optionality=repairable"],
    ) == {"boundary": "good", "optionality": "repairable"}
    with pytest.raises(script.RunnerError, match="NAME=VALUE"):
        script._parse_review_dimensions(["malformed"])
    with pytest.raises(script.RunnerError, match="unsupported"):
        script.record_human_review(
            script.EvaluationRuntimeConfig(
                laminar_project_api_key="local-laminar-test-key",
                laminar_base_url="http://127.0.0.1",
                laminar_http_port=8000,
                laminar_grpc_port=8001,
                laminar_frontend_port=5667,
                artifact_root=PROJECT_ROOT / ".artifacts" / "test",
                telemetry_wait_seconds=1.0,
            ),
            trace_id="00000000-0000-0000-0000-000000000001",
            submission_index=1,
            label="not-a-label",
        )
    with pytest.raises(script.RunnerError, match="phase"):
        script.record_human_review(
            script.EvaluationRuntimeConfig(
                laminar_project_api_key="local-laminar-test-key",
                laminar_base_url="http://127.0.0.1",
                laminar_http_port=8000,
                laminar_grpc_port=8001,
                laminar_frontend_port=5667,
                artifact_root=PROJECT_ROOT / ".artifacts" / "test",
                telemetry_wait_seconds=1.0,
            ),
            trace_id="00000000-0000-0000-0000-000000000001",
            phase="invalid",
            submission_index=1,
            label="repairable",
        )


def test_trial_aggregation_reports_wilson_and_input_macro_micro() -> None:
    script = _load_script()
    aggregate = script._aggregate_trials(
        [
            {
                "case_id": "case.one",
                "strict_pass": True,
                "candidate_pass": True,
                "issue_codes": [],
                "metrics": {
                    "candidate_pass": 1.0,
                    "input_count": 2.0,
                    "input_exact_match_count": 2.0,
                    "input_exact_match_rate": 1.0,
                },
                "candidate_trajectory": {
                    "submission_count": 2,
                    "accepted_count": 1,
                    "finish_after_first_accepted": True,
                },
            },
            {
                "case_id": "case.one",
                "strict_pass": False,
                "candidate_pass": False,
                "issue_codes": ["schema.record_mismatch"],
                "metrics": {
                    "candidate_pass": 0.0,
                    "input_count": 1.0,
                    "input_exact_match_count": 0.0,
                    "input_exact_match_rate": 0.0,
                },
                "candidate_trajectory": {
                    "submission_count": 1,
                    "accepted_count": 0,
                    "finish_after_first_accepted": False,
                },
            },
        ],
    )

    case = aggregate["case.one"]
    assert case["strict_pass_count"] == 1
    assert case["strict_pass_wilson_95"]["lower"] < 0.5
    assert case["candidate_quality"]["submission_count"] == 3
    assert aggregate["__overall__"]["input_exact_match"]["micro"]["rate"] == (
        2 / 3
    )
    assert aggregate["__overall__"]["input_exact_match"]["macro"]["rate"] == 0.5


def test_repository_cases_materialize_as_sdk_datapoints() -> None:
    script = _load_script()
    manifest = load_evaluation_manifest(PROJECT_ROOT, MANIFEST_PATH)

    datapoints = script._materialize_datapoints(
        manifest.cases[:1],
        trials=2,
        config_fingerprint="f" * 64,
    )

    assert len(datapoints) == 2
    assert datapoints[0].data["trial_key"].endswith("#0")
    assert datapoints[1].data["trial_key"].endswith("#1")
    assert "records" in datapoints[0].target


def test_local_summary_uses_the_redacted_field_whitelist(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    script = _load_script()
    monkeypatch.setattr(
        script,
        "_git_facts",
        lambda: {"revision": "abc", "dirty": True},
    )
    trial = {
        "case_id": "case.one",
        "trial_index": 0,
        "strict_pass": False,
        "candidate_pass": False,
        "telemetry_complete": True,
        "failure_category": "records",
        "termination_reason": "success",
        "issue_codes": [],
        "exception_type": None,
        "last_attempt_present": False,
        "trace_id": "trace",
        "reported_trace_id": "trace",
        "metrics": {"candidate_pass": 0.0},
    }

    summary = script._build_local_summary(
        [trial],
        evaluation_id="evaluation",
        evaluation_url="http://127.0.0.1/evaluation",
        config_fingerprint="f" * 64,
        telemetry_complete=True,
    )

    assert set(summary) == {
        "status",
        "evaluation",
        "config_fingerprint",
        "git",
        "trial_count",
        "strict_pass_count",
        "telemetry_complete",
        "trials",
        "cases",
    }
    encoded = json.dumps(summary)
    assert "configuration" not in encoded
    assert "sha256" not in encoded


def test_telemetry_requires_root_executor_generation_schema_and_llm() -> None:
    script = _load_script()
    complete = {
        "evaluation_span_count": 1,
        "executor_span_count": 1,
        "generation_span_count": 1,
        "schema_phase_count": 1,
        "llm_call_count": 1,
    }

    assert script._telemetry_complete(complete) is True
    complete["generation_span_count"] = 0
    assert script._telemetry_complete(complete) is False


async def test_telemetry_polling_tolerates_ingestion_delay(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    script = _load_script()
    query_count = 0

    runtime = script.EvaluationRuntimeConfig(
        laminar_project_api_key="local-laminar-test-key",
        laminar_base_url="http://127.0.0.1",
        laminar_http_port=8000,
        laminar_grpc_port=8001,
        laminar_frontend_port=5667,
        artifact_root=PROJECT_ROOT / ".artifacts" / "test",
        telemetry_wait_seconds=1.0,
    )

    def delayed_query(
        received_runtime: object,
        query: str,
        parameters: dict[str, Any],
    ) -> list[dict[str, Any]]:
        nonlocal query_count
        assert received_runtime is runtime
        del query, parameters
        query_count += 1
        if query_count == 1:
            return []
        return [
            {
                "trace_id": "00000000-0000-0000-0000-000000000001",
                "metadata": json.dumps({"trial_key": "case#0"}),
                "scores": "{}",
            },
        ]

    async def no_wait(_: float) -> None:
        return None

    def complete_telemetry(
        received_runtime: object,
        expected_runtime: object,
    ) -> dict[str, int]:
        assert received_runtime is expected_runtime
        return {
            "evaluation_span_count": 1,
            "executor_span_count": 1,
            "generation_span_count": 1,
            "schema_phase_count": 1,
            "llm_call_count": 1,
        }

    monkeypatch.setattr(script, "_sql_query", delayed_query)
    monkeypatch.setattr(script.asyncio, "sleep", no_wait)
    monkeypatch.setattr(
        script,
        "_telemetry_for_trace",
        lambda received_runtime, _: complete_telemetry(received_runtime, runtime),
    )

    by_trial, complete = await script._collect_telemetry(
        runtime,
        "00000000-0000-0000-0000-000000000002",
        1,
    )

    assert complete is True
    assert query_count == 2
    assert by_trial["case#0"]["complete"] is True
