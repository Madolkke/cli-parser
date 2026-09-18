"""Reasoning history diagnostics must never persist the reasoning itself."""

import json

import pytest
from agentscope.event import CustomEvent
from test_dataset_registry import _load_runner


def observe(tracer, value, phase="schema"):
    tracer(
        CustomEvent(
            name="cli_parser.schema.reasoning_history",
            value=value,
            metadata={"phase": phase, "sensitive": False},
        )
    )


@pytest.mark.parametrize("status", ["retained", "context_checked", "context_limit"])
def test_reasoning_history_counts_and_missing_observations(status):
    tracer = _load_runner()._SchemaTracer()
    assert tracer.facts()["reasoning_history"] == {
        "status": "unavailable",
        "events": [],
    }
    value = {
        "runtime_policy": "schema-reasoning-history-v1",
        "status": status,
        "round_index": 1,
        "reasoning_chars": 500,
    }
    if status != "retained":
        value.update(
            reasoning_blocks=1, estimated_tokens=600, context_limit_tokens=64000
        )
    for _ in range(70):
        observe(tracer, value)
    facts = tracer.facts()["reasoning_history"]
    assert facts == {"status": "observed", "events": [value] * 64}


@pytest.mark.parametrize(
    "extra",
    [
        {"reasoning_content": "private-body"},
        {"reasoning_chars": True},
        {"round_index": -1},
        {"status": "private-body"},
        {"runtime_policy": "other"},
    ],
)
def test_reasoning_history_rejects_uncontrolled_payload(extra):
    tracer = _load_runner()._SchemaTracer()
    value = {
        "runtime_policy": "schema-reasoning-history-v1",
        "status": "retained",
        "round_index": 1,
        "reasoning_chars": 500,
        **extra,
    }
    observe(tracer, value)
    assert tracer.facts()["reasoning_history"]["events"] == []
    assert "private-body" not in json.dumps(tracer.facts())


def test_reasoning_history_never_projects_ttp():
    tracer = _load_runner()._SchemaTracer()
    observe(
        tracer,
        {
            "runtime_policy": "schema-reasoning-history-v1",
            "status": "retained",
            "round_index": 1,
            "reasoning_chars": 500,
        },
        phase="ttp",
    )
    assert tracer.facts()["reasoning_history"]["events"] == []
