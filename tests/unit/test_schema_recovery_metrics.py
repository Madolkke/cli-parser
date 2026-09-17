"""Schema recovery observations retain fixed facts without Trace content."""

import json

import pytest
from agentscope import event as events
from test_dataset_registry import _load_runner


def recovery_value():
    return {
        "runtime_policy": "schema-reasoning-recovery-v1",
        "mode": "thinking_disabled",
        "reason": "consecutive_reasoning_only_length",
        "consecutive_responses": 3,
        "after_round_index": 3,
        "after_attempt_index": 3,
    }


def recovery_event(value, *, phase="schema"):
    return events.CustomEvent(
        name="cli_parser.schema.reasoning_recovery",
        value=value,
        metadata={"phase": phase, "sensitive": False},
    )


def test_recovery_observation_projects_fixed_facts_with_bounded_rounds():
    tracer = _load_runner()._SchemaTracer(record_rows=True)
    for index in range(40):
        value = recovery_value()
        value["after_round_index"] += index
        value["after_attempt_index"] += index
        tracer(recovery_event(value))
    facts = tracer.facts()["reasoning_recovery"]
    assert facts == {
        "status": "observed",
        "activation_count": 40,
        "rounds": [
            {
                "after_round_index": index + 3,
                "after_attempt_index": index + 3,
                "consecutive_responses": 3,
            }
            for index in range(32)
        ],
        "runtime_policy": "schema-reasoning-recovery-v1",
        "mode": "thinking_disabled",
        "reason": "consecutive_reasoning_only_length",
    }
    assert tracer.rounds.rows == []
    assert tracer.facts()["observed_submissions"] == 0


def test_missing_or_other_phase_recovery_is_unavailable_not_zero():
    tracer = _load_runner()._SchemaTracer()
    before = tracer.facts()["reasoning_recovery"]
    assert before == {
        "status": "unavailable",
        "activation_count": None,
        "rounds": [],
        "runtime_policy": None,
        "mode": None,
        "reason": None,
    }
    tracer(recovery_event(recovery_value(), phase="ttp"))
    assert tracer.facts()["reasoning_recovery"] == before


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("runtime_policy", "PRIVATE_POLICY"),
        ("mode", "PRIVATE_MODE"),
        ("reason", "PRIVATE_REASON"),
        ("consecutive_responses", "PRIVATE_COUNT"),
        ("consecutive_responses", True),
        ("consecutive_responses", 3.0),
        ("consecutive_responses", 2),
        ("after_round_index", "PRIVATE_ROUND"),
        ("after_round_index", True),
        ("after_round_index", 0),
        ("after_round_index", -1),
        ("after_attempt_index", "PRIVATE_ATTEMPT"),
        ("after_attempt_index", False),
        ("after_attempt_index", 3.0),
        ("after_attempt_index", 0),
        ("description", "PRIVATE_BODY"),
    ],
)
def test_malformed_or_extended_recovery_event_is_rejected_without_leakage(key, value):
    tracer = _load_runner()._SchemaTracer(record_rows=True)
    payload = recovery_value()
    payload[key] = value
    tracer(recovery_event(payload))
    assert tracer.facts()["reasoning_recovery"]["activation_count"] is None
    assert "PRIVATE" not in json.dumps(tracer.facts())
    assert tracer.rounds.rows == []


@pytest.mark.parametrize("missing_key", recovery_value())
def test_incomplete_recovery_event_is_ignored(missing_key):
    tracer = _load_runner()._SchemaTracer()
    payload = recovery_value()
    del payload[missing_key]
    tracer(recovery_event(payload))
    assert tracer.facts()["reasoning_recovery"]["status"] == "unavailable"
