"""Historical draft facts stay readable after removing candidate execution."""

import json
from copy import deepcopy
from types import SimpleNamespace

import pytest
from agentscope import event as events
from test_dataset_registry import _load_runner, _write_dataset

from cli_parser_agent import GenerationPolicy, TtpGeneratorSettings
from cli_parser_agent.evaluation import (
    load_dataset_registry,
    preflight_dataset_registry,
)


def environment(tmp_path, monkeypatch, runner):
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
    return registry, reports, root


def options(runner, registry, *, mode="schema-only", trials="2", concurrency="4"):
    return runner._build_parser().parse_args(
        [
            "run",
            "--registry",
            str(registry.path),
            "--mode",
            mode,
            "--trials",
            trials,
            "--concurrency",
            concurrency,
        ]
    )


def candidate():
    return {
        "type": "object",
        "properties": {"private_generated": {"type": "string"}},
        "additionalProperties": False,
    }


def custom(name, value, *, phase="schema"):
    return events.CustomEvent(
        name=name, value=value, metadata={"phase": phase, "sensitive": True}
    )


def draft_result(*, submission=1, accepted=True, frozen=True, facts=None, draft=None):
    return custom(
        "cli_parser.tool.result",
        {
            "tool_name": "submit_schema_draft",
            "input": {"draft": draft or {"source_quote": "PRIVATE_QUOTE"}},
            "output": {
                "accepted": accepted,
                "frozen": frozen,
                "issues": [],
                "schema_submission": submission,
                "compiled_schema": candidate(),
                "facts": facts or {},
            },
        },
    )


def test_draft_frozen_contract_is_memory_only_and_counts_are_allowlisted():
    runner = _load_runner()
    tracer = runner._SchemaTracer(capture_frozen=True)
    numeric = {
        "property_count": 4,
        "source_name_count": 3,
        "fallback_name_count": 1,
        "reference_count": 3,
        "fallback_unlabeled_count": 1,
        "fallback_ambiguous_source_count": 0,
        "fallback_invalid_name_count": 0,
        "fallback_conflict_count": 0,
        "fallback_split_component_count": 0,
    }
    tracer(
        draft_result(
            facts={
                **numeric,
                "quote": "PRIVATE_QUOTE",
                "source": "PRIVATE_SOURCE",
                "draft_bytes": True,
            }
        )
    )
    assert tracer.frozen_schema == candidate()
    assert tracer.facts()["compiler_facts"] == [numeric]
    assert tracer.facts()["observed_submissions"] == 1
    text = json.dumps(tracer.facts())
    assert "PRIVATE" not in text and "private_generated" not in text


def field_draft(fallback="private_fallback"):
    return {
        "version": 1,
        "fields": [
            {
                "name": {"source": [{"line_id": "i0f0l1", "quote": "PRIVATE"}]},
                "required": True,
                "node": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "fields": [
                            {
                                "name": {"fallback": fallback, "reason": "unlabeled"},
                                "required": False,
                                "node": {"type": "string"},
                            }
                        ],
                    },
                },
            }
        ],
    }


def test_draft_fallback_only_observes_final_freeze_and_nested_named_fields():
    runner = _load_runner()
    tracer = runner._SchemaTracer()
    tracer(draft_result(draft=field_draft("private_pending"), frozen=False))
    tracer(draft_result(submission=2, draft=field_draft("private_bad"), accepted=False))
    assert tracer.frozen_fallback is None
    tracer(draft_result(submission=3, draft=field_draft()))
    snapshot = tracer.frozen_fallback
    assert snapshot.names == frozenset({"private_fallback"})
    assert snapshot.nodes == 2
    assert snapshot.fallback_nodes == 1
    assert runner._fallback_trial_metrics(snapshot, applicable=True) == {
        "status": "observed",
        "business_nodes": 2,
        "fallback_nodes": 1,
        "fallback_ratio": 0.5,
    }
    tracer(
        draft_result(submission=4, draft=field_draft("private_closed"), accepted=False)
    )
    assert tracer.frozen_fallback is snapshot
    assert "private" not in repr(snapshot)
    empty = runner._accepted_draft_fallback(
        {"input": {"draft": {"version": 1, "fields": []}}}
    )
    assert runner._fallback_trial_metrics(empty, applicable=True) == {
        "status": "observed",
        "business_nodes": 0,
        "fallback_nodes": 0,
        "fallback_ratio": None,
    }


def test_draft_rejections_preserve_fixed_categories_without_raw_issue_values():
    tracer = _load_runner()._SchemaTracer()
    event = draft_result(
        accepted=False,
        frozen=False,
        facts={"source_rejection_count": 1, "name_conflict_count": 0},
    )
    event.value["output"]["issues"] = [
        {"code": "schema_draft.invalid_reference", "path": "PRIVATE_PATH"},
        {"code": "schema_draft.invalid_reference"},
        {"code": "schema_draft.PRIVATE_OTHER"},
    ]
    tracer(event)
    facts = tracer.facts()
    assert facts["draft_rejection_counts"]["schema_draft.invalid_reference"] == 1
    assert facts["draft_rejection_counts"]["schema_draft.other_rejection"] == 1
    assert facts["draft_rejection_counts"]["schema_draft.name_collision"] == 0
    assert facts["rejection_counts"]["schema.other_rejection"] == 1
    assert facts["compiler_facts"] == [
        {"source_rejection_count": 1, "name_conflict_count": 0}
    ]
    assert "PRIVATE" not in json.dumps(facts)


def test_draft_fallback_pairs_exclude_unfrozen_and_unknown_and_export_only_counts():
    runner = _load_runner()
    results = []
    for index in range(4):
        tracer = runner._SchemaTracer()
        if index < 2:
            tracer(draft_result(draft=field_draft(f"private_{index}")))
        elif index == 2:
            tracer(draft_result(draft=field_draft("private_unfrozen"), frozen=False))
        snapshot = tracer.frozen_fallback
        document = {
            "trial_id": f"run/demo.case/{index + 1}",
            "generation_success": True,
            "proposal_revalidated": True,
            "fallback_naming": runner._fallback_trial_metrics(
                snapshot, applicable=True
            ),
        }
        results.append((document, candidate(), snapshot))
    assert [row[0]["fallback_naming"]["status"] for row in results] == [
        "observed",
        "observed",
        "unobserved",
        "unobserved",
    ]
    pair_metrics = runner._fallback_repeat_consistency(results)
    assert pair_metrics["valid_schema_count"] == 4
    assert pair_metrics["fallback_observation_count"] == 2
    assert pair_metrics["evaluated_pair_count"] == 1
    assert pair_metrics["name_sets_equal_pair_count"] == 0
    assert pair_metrics["pairs"][0]["symmetric_difference_count"] == 2
    safe = json.dumps([pair_metrics, [row[0] for row in results]])
    assert "private_" not in safe and "PRIVATE" not in safe


def test_business_counter_does_not_count_framework_or_product_argument_failures():
    runner = _load_runner()
    tracer = runner._SchemaTracer()
    tracer(custom("cli_parser.protocol.repair", {"category": "framework_arguments"}))
    tracer(draft_result(submission=0, accepted=False, frozen=False))
    tracer(draft_result(submission=1, accepted=False, frozen=False))
    tracer(draft_result(submission=1, accepted=False, frozen=False))
    tracer(custom("cli_parser.protocol.repair", {"category": "product_arguments"}))
    tracer(draft_result(submission=2))
    assert tracer.facts()["observed_submissions"] == 2
    assert tracer.facts()["first_submission_accepted"] is False
    assert tracer.facts()["protocol"]["category_counts"] == {
        "framework_arguments": 1,
        "product_arguments": 1,
    }


def test_source_exposure_counts_unicode_and_keeps_unknown_finish_reason_unknown():
    tracer = _load_runner()._SchemaTracer()
    before = tracer.facts()
    assert before["source_display"]["sampled_chars"] is None
    assert before["provider_length_count"] is None
    tracer(
        custom(
            "cli_parser.phase.sampling_completed",
            {
                "sampled_outputs": [
                    {"text": "秘密甲", "original_char_count": 3, "truncated": False},
                    {"text": "秘密乙", "original_char_count": 90, "truncated": True},
                ],
                "input_fits": True,
            },
        )
    )
    tracer(
        custom(
            "cli_parser.phase.input_prepared",
            {
                "message": {"content": [{"type": "text", "text": "TASK秘密甲秘密乙"}]},
            },
        )
    )
    tracer(
        events.ModelCallEndEvent(
            reply_id="reply",
            input_tokens=7,
            output_tokens=8192,
            metadata={"phase": "schema"},
        )
    )
    facts = tracer.facts()
    assert facts["source_display"] == {
        "sampling_observations": 1,
        "input_prepared_observations": 1,
        "original_chars": 93,
        "sampled_chars": 6,
        "truncated_inputs": 1,
        "input_count": 2,
        "input_fits": True,
        "prepared_message_chars": 10,
        "prepared_minus_sampled_chars": 4,
    }
    assert facts["provider_finish_reason_observations"] == 0
    assert facts["provider_length_count"] is None
    assert "秘密" not in json.dumps(facts, ensure_ascii=False)
    tracer(
        custom(
            "cli_parser.phase.input_prepared",
            {"message": {"content": [{"type": "text", "text": "PRIVATE"}]}},
            phase="ttp",
        )
    )
    assert tracer.facts() == facts


async def test_schema_only_still_calls_public_proposal_with_only_inputs(
    tmp_path, monkeypatch
):
    runner = _load_runner()
    registry, reports, root = environment(tmp_path, monkeypatch, runner)
    requests = []

    class Generator:
        def __init__(self, **kwargs):
            pass

        async def propose_schema(self, request, *, observer):
            requests.append(request.model_dump())
            observer(draft_result(facts={"property_count": 1, "reference_count": 1}))
            return SimpleNamespace(
                status="success",
                proposal=SimpleNamespace(result_schema=deepcopy(candidate())),
                metadata=SimpleNamespace(
                    laminar_trace_id=None,
                    schema_submissions=1,
                    schema_agent_rounds=1,
                    termination_reason="success",
                ),
            )

    monkeypatch.setattr(runner, "TtpGenerator", Generator)
    args = options(runner, registry)
    assert await runner._run_schema(args, registry, reports) == 0
    assert requests == [{"command_outputs": ["Value: alpha\n"]}] * 2
    for path in root.rglob("*.json"):
        saved = path.read_text()
        assert "PRIVATE_QUOTE" not in saved
        assert "private_generated" not in saved
        assert "Value: alpha" not in saved


@pytest.mark.parametrize("frozen", [True, False])
async def test_end_to_end_draft_freeze_survives_later_exception_in_memory_only(
    monkeypatch, frozen
):
    runner = _load_runner()
    requests = []

    class Generator:
        def __init__(self, **kwargs):
            pass

        async def generate(self, request, *, observer):
            requests.append(request.model_dump())
            observer(draft_result(frozen=frozen, draft=field_draft()))
            raise RuntimeError("PRIVATE_TRANSPORT")

    monkeypatch.setattr(runner, "TtpGenerator", Generator)
    case = SimpleNamespace(
        inputs=[SimpleNamespace(text="PRIVATE_INPUT")],
        schema={"type": "object", "properties": {}, "additionalProperties": False},
    )
    tracer = runner._SchemaTracer(capture_frozen=True)
    document, proposal = await runner._run_end_to_end_trial(
        case, None, GenerationPolicy(), tracer
    )
    assert requests == [{"command_outputs": ["PRIVATE_INPUT"]}]
    assert document["generation_success"] is False
    assert document["schema_generation_success"] is frozen
    assert document["exception_type"] == "RuntimeError"
    assert (proposal is not None) is frozen
    assert (tracer.frozen_fallback is not None) is frozen
    assert "PRIVATE" not in json.dumps(document)
    assert "private_generated" not in json.dumps(document)


def test_candidate_selection_is_removed_after_pretrial():
    runner = _load_runner()
    with pytest.raises(SystemExit):
        runner._build_parser().parse_args(
            ["run", "--mode", "schema-only", "--schema-experiment-arm", "draft"]
        )
