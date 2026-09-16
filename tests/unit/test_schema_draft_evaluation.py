"""Draft experiments share inputs, isolate strategies and export counts only."""

import asyncio
import json
from copy import deepcopy
from dataclasses import replace
from types import SimpleNamespace

import pytest
from agentscope import event as events
from test_dataset_registry import _load_runner, _write_dataset

from cli_parser_agent import GenerationPolicy, TtpGeneratorSettings
from cli_parser_agent.evaluation import (
    load_dataset_registry,
    preflight_dataset_registry,
)
from cli_parser_agent.ttp_generation.agent.schema_strategy import (
    current_prompt_version,
    current_schema_strategy,
    schema_strategy_for_testing,
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


async def test_draft_fallback_pairs_exclude_unfrozen_and_unknown_and_export_only_counts(
    tmp_path, monkeypatch
):
    runner = _load_runner()
    registry, reports, root = environment(tmp_path, monkeypatch, runner)
    calls = 0

    async def trial(case, settings, policy, tracer):
        nonlocal calls
        calls += 1
        if calls in {1, 2}:
            tracer(draft_result(draft=field_draft(f"private_{calls}")))
        elif calls == 3:
            tracer(draft_result(draft=field_draft("private_unfrozen"), frozen=False))
        return {"generation_success": True, "proposal_revalidated": True}, candidate()

    monkeypatch.setattr(runner, "_run_schema_trial", trial)
    args = options(runner, registry, trials="4")
    args.schema_experiment_arm = ["draft"]
    assert await runner._run_schema(args, registry, reports) == 0
    summary = json.loads(next(root.rglob("summary.json")).read_text())
    assert [row["fallback_naming"]["status"] for row in summary["trials"]] == [
        "observed",
        "observed",
        "unobserved",
        "unobserved",
    ]
    pair_metrics = summary["fallback_naming_consistency"]["demo.case"]
    assert pair_metrics["valid_schema_count"] == 4
    assert pair_metrics["fallback_observation_count"] == 2
    assert pair_metrics["evaluated_pair_count"] == 1
    assert pair_metrics["name_sets_equal_pair_count"] == 0
    assert pair_metrics["pairs"][0]["symmetric_difference_count"] == 2
    for path in root.rglob("*.json"):
        assert "private_" not in path.read_text()
        assert "PRIVATE" not in path.read_text()


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
            "--schema-experiment-arm",
            "direct",
            "--schema-experiment-arm",
            "draft",
        ]
    )


@pytest.mark.parametrize("mode", ["schema-only", "end-to-end"])
async def test_arm_rotation_is_request_local_and_global_concurrency_four(
    tmp_path, monkeypatch, mode
):
    runner = _load_runner()
    registry, reports, root = environment(tmp_path, monkeypatch, runner)
    # Directory parents differ per arm; timestamp uniqueness alone cannot make
    # their run IDs unique when the clock returns the same instant.
    monkeypatch.setattr(
        runner._run_support, "new_run_id", lambda: "20260101T000000.000000Z"
    )
    second = replace(
        reports[0],
        dataset=replace(reports[0].dataset, name="second.case"),
        case=replace(reports[0].case, id="second.case"),
    )
    reports = (*reports, second)
    started = []
    input_ids = {}
    active = peak = 0

    async def trial(case, settings, policy, tracer):
        nonlocal active, peak
        arm = current_schema_strategy()
        version = current_prompt_version()
        started.append((case.id, arm))
        input_ids.setdefault(case.id, set()).add(id(case.inputs))
        active += 1
        peak = max(peak, active)
        await asyncio.sleep(0.01)
        assert current_schema_strategy() == arm
        assert current_prompt_version() == version
        active -= 1
        return {
            "generation_success": True,
            "proposal_revalidated": True,
            "schema_generation_success": True,
            "independent_acceptance": {"valid": True},
        }, candidate()

    monkeypatch.setattr(
        runner,
        "_run_schema_trial" if mode == "schema-only" else "_run_end_to_end_trial",
        trial,
    )
    assert (
        await runner._run_schema(
            options(runner, registry, mode=mode), registry, reports
        )
        == 0
    )
    assert peak == 4
    assert started == [
        ("demo.case", "direct"),
        ("demo.case", "draft"),
        ("demo.case", "draft"),
        ("demo.case", "direct"),
        ("second.case", "draft"),
        ("second.case", "direct"),
        ("second.case", "direct"),
        ("second.case", "draft"),
    ]
    assert all(len(v) == 1 for v in input_ids.values())
    summaries = [json.loads(p.read_text()) for p in root.rglob("summary.json")]
    assert len(summaries) == 2
    assert {s["trial_count"] for s in summaries} == {4}
    assert len({t["trial_id"] for s in summaries for t in s["trials"]}) == 8
    assert len({s["configuration"]["prompt"]["version"] for s in summaries}) == 2
    assert {s["run_id"] for s in summaries} == {
        "20260101T000000.000000Z-direct",
        "20260101T000000.000000Z-draft",
    }
    manifest = json.loads(next(root.rglob("experiment.json")).read_text())
    assert manifest["completed_request_count"] == manifest["planned_request_count"] == 8
    assert manifest["same_input_snapshot"] is True
    assert current_schema_strategy() == "direct"


@pytest.mark.parametrize("failure", ["cancel", "exception"])
async def test_interrupted_experiment_keeps_actual_completed_count(
    tmp_path, monkeypatch, failure
):
    runner = _load_runner()
    registry, reports, root = environment(tmp_path, monkeypatch, runner)
    calls = 0

    async def trial(case, settings, policy, tracer):
        nonlocal calls
        calls += 1
        if calls == 2:
            if failure == "cancel":
                raise asyncio.CancelledError()
            raise RuntimeError("PRIVATE_ERROR")
        await asyncio.sleep(0.01)
        return {"generation_success": True, "proposal_revalidated": True}, candidate()

    monkeypatch.setattr(runner, "_run_schema_trial", trial)
    with pytest.raises(asyncio.CancelledError if failure == "cancel" else RuntimeError):
        await runner._run_schema(
            options(runner, registry, concurrency="1"), registry, reports
        )
    manifest = json.loads(next(root.rglob("experiment.json")).read_text())
    assert manifest["status"] == "interrupted"
    assert manifest["completed_request_count"] == 1
    assert manifest["planned_request_count"] == 4
    assert len(list(root.rglob("trial-*.json"))) == 1
    assert "PRIVATE_ERROR" not in json.dumps(manifest)
    assert current_schema_strategy() == "direct"


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
    args.schema_experiment_arm = ["draft"]
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
    with schema_strategy_for_testing("draft"):
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


def test_draft_factory_does_not_escape_failed_context():
    assert current_schema_strategy() == "direct"
    with pytest.raises(RuntimeError), schema_strategy_for_testing("draft"):
        assert current_schema_strategy() == "draft"
        raise RuntimeError()
    assert current_schema_strategy() == "direct"


def test_ttp_only_rejects_experiment_arm_before_registry(tmp_path):
    runner = _load_runner()
    assert (
        runner.main(
            [
                "run",
                "--registry",
                str(tmp_path / "absent.toml"),
                "--mode",
                "ttp-only",
                "--schema-experiment-arm",
                "draft",
            ]
        )
        == 2
    )


def test_duplicate_experiment_arm_is_rejected_before_registry(tmp_path, capsys):
    assert (
        _load_runner().main(
            [
                "run",
                "--registry",
                str(tmp_path / "absent.toml"),
                "--mode",
                "schema-only",
                "--schema-experiment-arm",
                "draft",
                "--schema-experiment-arm",
                "draft",
            ]
        )
        == 2
    )
    assert "duplicate schema experiment arm" in capsys.readouterr().err
