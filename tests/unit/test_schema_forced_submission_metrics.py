"""Forced submission telemetry is distinct from replies and accepted artifacts."""

import json
from types import SimpleNamespace

import pytest
from agentscope import event as events
from test_dataset_registry import _load_runner


def forced_value():
    return {
        "reason": "pure_reasoning_length_retry_limit",
        "consecutive_no_tool_responses": 3,
        "runtime_policy": "schema-forced-submission-v1",
    }


def forced_event(value=None, *, phase="schema", sensitive=False):
    return events.CustomEvent(
        name="cli_parser.schema.forced_submission",
        value=forced_value() if value is None else value,
        metadata={
            "phase": phase,
            "sensitive": sensitive,
            "sequence": 42,
            "elapsed_seconds": 12.5,
        },
    )


def test_forced_submission_activation_and_round_row_are_projected_independently():
    tracer = _load_runner()._SchemaTracer(record_rows=True)
    tracer(forced_event())
    facts = tracer.facts()
    assert facts["forced_submission"] == {
        "status": "observed",
        "activation_count": 1,
        "events": [forced_value()],
    }
    assert tracer.rounds.rows == [
        {
            "sequence": 42,
            "phase": "schema",
            "elapsed_seconds": 12.5,
            "event": "CustomEvent",
            "name": "cli_parser.schema.forced_submission",
            "value": forced_value(),
        }
    ]
    # Activation happens before model I/O, and does not assert any response,
    # successful submission, freeze or provider finish count.
    assert facts["observed_submissions"] == 0
    assert facts["first_frozen_seconds"] is None
    assert facts["protocol"]["repair_observations"] == 0
    assert facts["provider_finish_reason_observations"] == 0
    assert facts["provider_length_count"] is None


def test_forced_submission_event_history_is_bounded_without_losing_total():
    tracer = _load_runner()._SchemaTracer()
    for _ in range(40):
        tracer(forced_event())
    assert tracer.facts()["forced_submission"] == {
        "status": "observed",
        "activation_count": 40,
        "events": [forced_value()] * 32,
    }
    assert tracer.rounds.rows == []


def test_missing_forced_submission_observation_is_unavailable_for_legacy_runs():
    tracer = _load_runner()._SchemaTracer(record_rows=True)
    assert tracer.facts()["forced_submission"] == {
        "status": "unavailable",
        "activation_count": None,
        "events": [],
    }
    assert tracer.rounds.rows == []


@pytest.mark.parametrize(
    "value",
    [
        "PRIVATE_BODY",
        [forced_value()],
        None,
        {},
        *[
            {key: item for key, item in forced_value().items() if key != missing}
            for missing in forced_value()
        ],
        *[
            {**forced_value(), key: value}
            for key, value in (
                ("reason", "PRIVATE_REASON"),
                ("runtime_policy", "PRIVATE_POLICY"),
                ("consecutive_no_tool_responses", "PRIVATE_COUNT"),
                ("consecutive_no_tool_responses", True),
                ("consecutive_no_tool_responses", 3.0),
                ("consecutive_no_tool_responses", 0),
                ("consecutive_no_tool_responses", -1),
                ("description", "PRIVATE_BODY"),
                ("reasoning_content", "PRIVATE_REASONING"),
            )
        ],
    ],
)
def test_malformed_forced_submission_is_rejected_without_content_leakage(value):
    tracer = _load_runner()._SchemaTracer(record_rows=True)
    event = forced_event()
    event.value = value
    tracer(event)
    assert tracer.facts()["forced_submission"]["activation_count"] is None
    assert tracer.rounds.rows == []
    assert "PRIVATE" not in json.dumps(tracer.facts())


@pytest.mark.parametrize(
    "phase,sensitive", [("ttp", False), ("schema", True), ("schema", None)]
)
def test_non_schema_or_sensitive_forced_submission_is_ignored(phase, sensitive):
    tracer = _load_runner()._SchemaTracer(record_rows=True)
    tracer(forced_event(phase=phase, sensitive=sensitive))
    assert tracer.facts()["forced_submission"]["status"] == "unavailable"
    assert tracer.rounds.rows == []


def test_no_tool_and_length_evidence_do_not_imply_forced_submission():
    tracer = _load_runner()._SchemaTracer(record_rows=True)
    tracer(
        events.ModelCallEndEvent(
            reply_id="reply",
            input_tokens=100,
            output_tokens=8192,
            metadata={"phase": "schema", "sensitive": False},
        )
    )
    tracer(
        events.CustomEvent(
            name="cli_parser.protocol.repair",
            value={"category": "no_tool", "consecutive_failures": 3},
            metadata={"phase": "schema", "sensitive": False},
        )
    )
    tracer(
        events.CustomEvent(
            name="cli_parser.schema.truncated_submission_discarded",
            value={
                "reason": "provider_length",
                "discarded_tool_calls": 1,
                "round_index": 2,
                "attempt_index": 2,
                "runtime_policy": "schema-truncated-submission-guard-v1",
            },
            metadata={"phase": "schema", "sensitive": False},
        )
    )
    tracer(
        events.CustomEvent(
            name="unknown_event",
            value=forced_value(),
            metadata={"phase": "schema", "sensitive": False},
        )
    )
    before = tracer.facts()
    assert before["forced_submission"]["status"] == "unavailable"
    assert before["protocol"]["category_counts"] == {"no_tool": 1}
    assert before["truncated_submission_guard"]["discarded_tool_calls"] == 1
    assert before["provider_length_count"] is None
    tracer(forced_event())
    after = tracer.facts()
    assert after["forced_submission"]["activation_count"] == 1
    for key in (
        "protocol",
        "truncated_submission_guard",
        "provider_length_count",
        "provider_finish_reason_observations",
        "observed_submissions",
    ):
        assert after[key] == before[key]


async def test_trial_keeps_activation_even_when_provider_request_fails(monkeypatch):
    runner = _load_runner()

    class Generator:
        def __init__(self, **kwargs):
            pass

        async def propose_schema(self, request, *, observer):
            observer(forced_event())
            raise RuntimeError("PRIVATE_PROVIDER_FAILURE")

    monkeypatch.setattr(runner, "TtpGenerator", Generator)
    document, schema = await runner._run_schema_trial(
        SimpleNamespace(inputs=[SimpleNamespace(text="PRIVATE_INPUT")]),
        None,
        None,
        runner._SchemaTracer(),
    )
    assert schema is None
    assert document["generation_success"] is False
    assert document["observations"]["forced_submission"]["activation_count"] == 1
    assert document["observations"]["observed_submissions"] == 0
    assert "PRIVATE" not in json.dumps(document)
