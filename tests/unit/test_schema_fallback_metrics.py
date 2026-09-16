"""Fallback-name observations retain names in memory only until comparison."""

import json
from copy import deepcopy

import pytest
from agentscope import event as events
from test_dataset_registry import _load_runner, _write_dataset

from cli_parser_agent import GenerationPolicy, TtpGeneratorSettings
from cli_parser_agent.evaluation import (
    load_dataset_registry,
    preflight_dataset_registry,
)


def plan(names=("private_name", "private_status")):
    return {
        "version": 1,
        "nodes": [
            {
                "id": f"value_{index}",
                "kind": "value",
                "role": "text",
                "fallback_name": name,
                "fallback_reason": "unlabeled",
                "occurrences": [
                    {
                        "parent_instance_id": "root_0",
                        "segments": [
                            {
                                "input_index": 0,
                                "fragment_index": 0,
                                "start": 0,
                                "end": 1,
                            }
                        ],
                    }
                ],
            }
            for index, name in enumerate(names)
        ],
    }


def tool(name="submit_schema_plan", *, accepted=True, frozen=False, candidate=None):
    return events.CustomEvent(
        name="cli_parser.tool.result",
        metadata={"phase": "schema"},
        value={
            "tool_name": name,
            "input": {"plan": candidate},
            "output": {"accepted": accepted, "frozen": frozen, "issues": []},
        },
    )


def start():
    return events.ToolCallStartEvent(
        reply_id="reply",
        tool_call_id="call",
        tool_call_name="submit_schema_plan",
        metadata={"phase": "schema"},
    )


def test_direct_freeze_counts_nodes_and_distinct_name_set_separately():
    runner = _load_runner()
    tracer = runner._SchemaTracer()
    candidate = plan(("private_name", "private_name"))
    tracer(tool(candidate=candidate, frozen=True))
    snapshot = tracer.frozen_fallback
    assert snapshot.names == frozenset({"private_name"})
    assert snapshot.nodes == snapshot.fallback_nodes == 2
    assert runner._fallback_trial_metrics(snapshot, applicable=True) == {
        "status": "observed",
        "business_nodes": 2,
        "fallback_nodes": 2,
        "fallback_ratio": 1,
    }
    assert "private" not in repr(snapshot)
    assert "private" not in json.dumps(tracer.facts())


def test_confirmation_only_freezes_latest_accepted_plan():
    runner = _load_runner()
    tracer = runner._SchemaTracer()
    tracer(tool(candidate=plan(("private_old",))))
    assert tracer.frozen_fallback is None
    tracer(start())
    tracer(tool(candidate=plan(("private_latest",))))
    tracer(tool("confirm_schema_plan", frozen=True))
    assert tracer.frozen_fallback.names == frozenset({"private_latest"})
    assert tracer._pending_fallback is None
    tracer(tool(candidate=plan(("private_closed",)), accepted=False, frozen=True))
    assert tracer.frozen_fallback.names == frozenset({"private_latest"})


@pytest.mark.parametrize("replacement", ["framework", "product", "malformed_result"])
def test_invalid_replacement_revokes_pending_fallback(replacement):
    tracer = _load_runner()._SchemaTracer()
    tracer(tool(candidate=plan()))
    if replacement == "framework":
        tracer(start())
    elif replacement == "product":
        tracer(tool(candidate={"wrong": "private"}, accepted=False))
    else:
        event = tool(candidate=plan())
        event.value["output"] = None
        tracer(event)
    # Even an inconsistent observed confirmation cannot recover the stale plan.
    tracer(tool("confirm_schema_plan", frozen=True))
    assert tracer.frozen_fallback is None
    assert tracer._pending_fallback is None


def test_rejected_or_unconfirmed_or_malformed_plans_are_not_final():
    runner = _load_runner()
    for event in (
        tool(candidate=plan(), accepted=False, frozen=True),
        tool(candidate=plan()),
        tool(candidate={"nodes": "private"}, frozen=True),
    ):
        tracer = runner._SchemaTracer()
        tracer(event)
        assert tracer.frozen_fallback is None
    assert runner._fallback_trial_metrics(None, applicable=False) == {
        "status": "not_applicable",
        "business_nodes": None,
        "fallback_nodes": None,
        "fallback_ratio": None,
    }


def test_zero_fallback_and_set_comparison_do_not_claim_field_rename():
    runner = _load_runner()
    empty = runner._FallbackSnapshot(frozenset(), 2, 0)
    first = runner._FallbackSnapshot(frozenset({"private_one", "private_two"}), 3, 2)
    second = runner._FallbackSnapshot(frozenset({"private_two", "private_three"}), 4, 2)
    base = {"generation_success": True, "proposal_revalidated": True}
    results = [
        ({**base, "trial_id": f"trial_{index}"}, {"type": "object"}, snapshot)
        for index, snapshot in enumerate((empty, empty, first, second))
    ]
    # Failure and missing observation must not become empty sets or extra pairs.
    results.extend(
        [
            ({**base, "trial_id": "failed", "generation_success": False}, {}, first),
            ({**base, "trial_id": "bad", "proposal_revalidated": False}, {}, first),
            ({**base, "trial_id": "missing"}, {}, None),
        ]
    )
    output = runner._fallback_repeat_consistency(results)
    assert output["valid_schema_count"] == 5
    assert output["fallback_observation_count"] == 4
    assert output["evaluated_pair_count"] == 6
    assert output["name_sets_equal_pair_count"] == 1
    assert output["pairs"][0]["name_set_jaccard"] == 1
    pair = output["pairs"][-1]
    assert pair["name_sets_equal"] is False
    assert pair["name_set_jaccard"] == pytest.approx(1 / 3)
    assert pair["symmetric_difference_count"] == 2
    assert pair["left_only_count"] == pair["right_only_count"] == 1
    serialized = json.dumps(output)
    for forbidden in ("private", "rename", "signature", "hash"):
        assert forbidden not in serialized


async def test_runner_persists_only_final_counts_and_pair_numbers(
    tmp_path, monkeypatch
):
    runner = _load_runner()
    registry = load_dataset_registry(_write_dataset(tmp_path, complete=True))
    reports = preflight_dataset_registry(registry)
    root = tmp_path / "output"
    monkeypatch.setattr(
        runner,
        "_configuration",
        lambda: (
            TtpGeneratorSettings(api_key="offline", model_name="offline"),
            GenerationPolicy(),
            root,
            {},
        ),
    )
    expected = {
        "type": "object",
        "properties": {"item": {"type": "string"}},
        "additionalProperties": False,
    }

    async def trial(case, settings, policy, tracer):
        tracer(tool(candidate=plan(), frozen=True))
        return {"generation_success": True, "proposal_revalidated": True}, deepcopy(
            expected
        )

    monkeypatch.setattr(runner, "_run_schema_trial", trial)
    args = runner._build_parser().parse_args(
        [
            "run",
            "--registry",
            str(registry.path),
            "--mode",
            "schema-only",
            "--trials",
            "4",
            "--concurrency",
            "4",
        ]
    )
    assert await runner._run_schema(args, registry, reports) == 0
    summary = json.loads(next(root.rglob("summary.json")).read_text())
    for row in summary["trials"]:
        assert row["fallback_naming"]["fallback_nodes"] == 2
        assert row["fallback_naming"]["fallback_ratio"] == 1
    consistency = next(iter(summary["fallback_naming_consistency"].values()))
    assert consistency["evaluated_pair_count"] == 6
    assert consistency["name_sets_equal_pair_count"] == 6
    for path in root.rglob("*.json"):
        assert "private_name" not in path.read_text()
        assert "private_status" not in path.read_text()
