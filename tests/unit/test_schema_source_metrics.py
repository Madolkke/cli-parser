"""Source view telemetry persists counts only, never source data."""

import json

import pytest
from agentscope import event as events
from test_dataset_registry import _load_runner


def payload():
    return {
        "runtime_policy": "schema-source-recovery-v1",
        "status": "injected",
        "round_index": 1,
        "attempt_index": 1,
        "estimated_tokens": 5000,
        "input_count": 1,
        "complete_input_count": 1,
        "skipped_truncated_input_count": 0,
        "complete_source_line_count": 2,
        "displayed_line_count": 2,
        "displayed_token_count": 6,
        "omitted_line_limit_count": 0,
        "omitted_token_limit_line_count": 0,
        "omitted_byte_limit_line_count": 0,
        "serialized_bytes": 1024,
    }


def event(value, phase="schema"):
    return events.CustomEvent(
        name="cli_parser.schema.source_view",
        value=value,
        metadata={"phase": phase, "sensitive": False},
    )


@pytest.mark.parametrize(
    "status",
    [
        "injected",
        "skipped_deadline",
        "skipped_no_source",
        "skipped_history",
        "skipped_context",
        "skipped_count",
    ],
)
def test_source_view_counts_bounded_and_missing_is_unavailable(status):
    tracer = _load_runner()._SchemaTracer(record_rows=True)
    assert tracer.facts()["source_view"] == {"status": "unavailable", "events": []}
    value = {**payload(), "status": status}
    for _ in range(40):
        tracer(event(value))
    assert tracer.facts()["source_view"] == {
        "status": "observed",
        "events": [value] * 32,
    }
    assert tracer.rounds.rows == []


@pytest.mark.parametrize(
    "key,value",
    [
        ("runtime_policy", "PRIVATE"),
        ("status", "PRIVATE"),
        ("status", ["injected"]),
        ("round_index", True),
        ("attempt_index", 0),
        ("serialized_bytes", 16385),
        ("displayed_token_count", 257),
        ("estimated_tokens", "PRIVATE"),
        ("estimated_tokens", False),
        ("complete_input_count", -1),
        ("input_count", 6),
        ("displayed_line_count", 61),
        ("token", "PRIVATE"),
    ],
)
def test_malformed_source_event_never_exports_body(key, value):
    tracer = _load_runner()._SchemaTracer(record_rows=True)
    tracer(event({**payload(), key: value}))
    assert tracer.facts()["source_view"]["status"] == "unavailable"
    assert "PRIVATE" not in json.dumps(tracer.facts())
    assert tracer.rounds.rows == []


def test_other_phase_and_incomplete_event_are_not_accepted():
    tracer = _load_runner()._SchemaTracer()
    tracer(event(payload(), phase="ttp"))
    incomplete = payload()
    del incomplete["estimated_tokens"]
    tracer(event(incomplete))
    assert tracer.facts()["source_view"]["status"] == "unavailable"
