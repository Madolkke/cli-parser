"""Offline checks for the opt-in context experiment adapter."""

from __future__ import annotations

import asyncio
import json
import sys
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from agentscope.message import (
    AssistantMsg,
    TextBlock,
    ThinkingBlock,
    ToolCallBlock,
    ToolResultBlock,
    ToolResultState,
    UserMsg,
)
from agentscope.model import OpenAIChatModel

SCRIPT_DIRECTORY = Path(__file__).resolve().parents[2] / "scripts"
if str(SCRIPT_DIRECTORY) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIRECTORY))

import context_ablation as ablation  # noqa: E402

from cli_parser_agent import GenerationPolicy, TtpGeneratorSettings  # noqa: E402
from cli_parser_agent.ttp_generation.agent import builder  # noqa: E402
from cli_parser_agent.ttp_generation.agent.session import (  # noqa: E402
    GenerationSession,
    ValidatorOutcome,
)
from cli_parser_agent.ttp_generation.progress import ProgressEmitter  # noqa: E402


def _agent(*, context_size: int = 48_000) -> Any:
    session = GenerationSession(
        command_outputs=("value: original",),
        schema_validator=lambda _: ValidatorOutcome(valid=True),
        template_validator=lambda _: ValidatorOutcome(valid=True),
    )
    session.frozen_schema = ablation._synthetic_schema()
    agent = builder.build_agent(
        settings=TtpGeneratorSettings(
            api_key="offline-only",
            model_name="synthetic-model",
            context_size=context_size,
            max_tokens=512,
        ),
        policy=GenerationPolicy(),
        session=session,
        phase="ttp",
    )
    agent.state.context.append(
        builder.build_ttp_task_message(session.command_outputs, session.frozen_schema)
    )
    return agent


async def test_offline_four_variants_exercise_actual_formatter_and_summary() -> None:
    results = {
        variant: await ablation.offline_variant(variant)
        for variant in ablation.VARIANTS
    }

    current = results["current"]
    assert current["summary_calls"] == 1
    assert current["agents"][0]["compressions_completed"] == 1
    assert any(not check["task_intact"] for check in current["agents"][0]["checks"])
    assert any(
        request["forced_tool_choice"] for request in current["agents"][0]["requests"]
    )
    for name in ("estimator", "retention", "combined"):
        result = results[name]
        assert result["summary_calls"] == 0
        assert result["agents"][0]["compressions_completed"] == 0
        assert result["stage_calls"] == 4
        assert result["phase_completed"]
        assert result["candidate_retained"]
        assert result["retained_candidate_submission_index"] == 1
        assert result["tests_used"] == 1
        row = result["agents"][0]
        assert all(check["task_intact"] for check in row["checks"])
        assert all(check["tool_pairs_complete"] for check in row["checks"])
        assert row["checks"][-1]["tool_calls"] == 3
        assert all(not request["forced_tool_choice"] for request in row["requests"])
        assert all(not request["reasoning_content_sent"] for request in row["requests"])
        assert all(check["task_intact"] for check in result["transport_checks"])
        assert result["transport_checks"][-1]["large_result_present"]
        assert all(
            not check["forced_tool_choice"] for check in result["transport_checks"]
        )
        if name in {"retention", "combined"}:
            final_wire = result["transport_checks"][-1]
            assert final_wire["full_submission_result_count"] == 2
            assert final_wire["tool_result_count"] == 3
            assert result["compacted_interactions"] == 0
    assert (
        results["retention"]["agents"][0]["checks"][1]["estimated_tokens_before"]
        > 48_000
    )
    assert (
        results["combined"]["agents"][0]["checks"][1]["estimated_tokens_before"]
        < 48_000
    )
    serialized = json.dumps(results)
    for private in ("synthetic summary", "value: sample", "value: {{", "offline-only"):
        assert private not in serialized
    assert "sha256" not in serialized


@pytest.mark.parametrize("variant", ablation.VARIANTS)
async def test_estimator_changes_count_without_mutating_history(variant: str) -> None:
    messages = [
        UserMsg("user", content="visible task"),
        AssistantMsg("agent", content=[ThinkingBlock(thinking="x" * 240_000)]),
    ]
    original = deepcopy(messages)
    with ablation.context_ablation(variant):
        agent = _agent()
        count = await agent.model.count_tokens(messages, None)
        baseline = await OpenAIChatModel.count_tokens(agent.model, messages, None)
        wire = await agent.model.formatter.format(messages)
    assert messages == original
    assert "x" * 240_000 not in json.dumps(wire)
    if variant in {"estimator", "combined"}:
        assert baseline - count == 60_000
    else:
        assert count == baseline


@pytest.mark.parametrize("variant", ["retention", "combined"])
async def test_retention_keeps_large_tool_results_then_stops_before_overflow(
    variant: str,
) -> None:
    with ablation.context_ablation(variant) as report:
        agent = _agent()
        await agent.compress_context()
        result = ToolResultBlock(
            id="large-result",
            name="test_ttp_template",
            output=[TextBlock(text="private-record-value" * 20_000)],
            state=ToolResultState.SUCCESS,
        )
        retained, discarded = await agent._split_tool_result_for_compression(result)
        assert retained is result
        assert discarded is None
        agent.state.context.append(
            AssistantMsg(
                "agent",
                content=[
                    ToolCallBlock(id=result.id, name=result.name, input="{}"),
                    result,
                ],
            )
        )
        before = deepcopy(agent.state.context)
        with pytest.raises(ablation.ContextAblationBudgetExceeded):
            await agent.compress_context()
        assert agent.state.context == before
        assert not agent.state.summary
    assert report.agents[0]["stop_category"] == "context_budget_exceeded"
    assert report.agents[0]["requests"] == []
    assert "private-record-value" not in json.dumps(report.document())


async def test_retention_refuses_changed_task_and_incomplete_tool_pairs() -> None:
    with ablation.context_ablation("retention") as report:
        first = _agent()
        await first.compress_context()
        first.state.context[0] = UserMsg("user", "changed contract")
        with pytest.raises(ablation.ContextAblationIntegrityError):
            await first.compress_context()
        second = _agent()
        second.state.context.append(
            AssistantMsg(
                "agent",
                content=[
                    ToolCallBlock(id="orphan", name="test_ttp_template", input="{}")
                ],
            )
        )
        with pytest.raises(ablation.ContextAblationIntegrityError):
            await second.compress_context()
    assert all(
        row["stop_category"] == "context_integrity_failed" for row in report.agents
    )


@pytest.mark.parametrize("variant", ["retention", "combined"])
@pytest.mark.parametrize("matches", [0, 2])
async def test_retention_stops_when_candidate_feedback_is_not_unique(
    variant: str, matches: int
) -> None:
    with ablation.context_ablation(variant) as report:
        agent = _agent()
        session = agent._ablation_session
        session.ttp_submissions = 2
        session.validated_ttp_submission_index = 1
        blocks = []
        for index in range(2):
            feedback = {
                "feedback_version": 1,
                "scope": "full_input_validation",
                "accepted": index < matches,
                "candidate_updated": index < matches,
                "retained_candidate_submission_index": 1,
            }
            blocks.extend(
                [
                    ToolCallBlock(
                        id=str(index), name="submit_ttp_template", input="{}"
                    ),
                    ToolResultBlock(
                        id=str(index),
                        name="submit_ttp_template",
                        output="<validation_feedback>\n"
                        + json.dumps(feedback)
                        + "\n</validation_feedback>\nprivate-candidate-result",
                        state=ToolResultState.SUCCESS,
                    ),
                ]
            )
        agent.state.context.append(AssistantMsg("agent", content=blocks))
        original = deepcopy(agent.state.context)
        with pytest.raises(ablation.ContextAblationIntegrityError):
            await agent.compress_context()
        assert agent.state.context == original
    row = report.agents[0]
    assert row["stop_category"] == "context_integrity_failed"
    assert row["requests"] == []
    assert session.ttp_history_compacted_interactions == 0
    assert "private-candidate-result" not in json.dumps(report.document())


@pytest.mark.parametrize("variant", ["retention", "combined"])
@pytest.mark.parametrize("separate_messages", [False, True])
async def test_retention_stops_when_result_precedes_call(
    variant: str, separate_messages: bool
) -> None:
    with ablation.context_ablation(variant) as report:
        agent = _agent()
        blocks = [
            ToolResultBlock(
                id="reversed",
                name="test_ttp_template",
                output="private-result",
                state=ToolResultState.SUCCESS,
            ),
            ToolCallBlock(id="reversed", name="test_ttp_template", input="{}"),
        ]
        messages = (
            [AssistantMsg("agent", content=[block]) for block in blocks]
            if separate_messages
            else [AssistantMsg("agent", content=blocks)]
        )
        facts = ablation._pair_facts(messages)
        assert facts["tool_calls"] == facts["tool_results"] == 1
        assert facts["tool_pair_order_valid"] is False
        assert facts["tool_pairs_complete"] is False
        agent.state.context.extend(messages)
        original = deepcopy(agent.state.context)
        with pytest.raises(ablation.ContextAblationIntegrityError):
            await agent.compress_context()
        assert agent.state.context == original
    assert report.agents[0]["stop_category"] == "context_integrity_failed"
    assert report.agents[0]["requests"] == []


async def test_concurrent_agents_keep_separate_task_comparisons() -> None:
    with ablation.context_ablation("combined") as report:
        agents = [_agent(), _agent()]
        agents[1].state.context[0] = UserMsg("user", "second task")
        await asyncio.gather(*(agent.compress_context() for agent in agents))
        agents[1].state.context[0] = UserMsg("user", "changed second task")
        await agents[0].compress_context()
        with pytest.raises(ablation.ContextAblationIntegrityError):
            await agents[1].compress_context()
    assert report.agents[0]["stop_category"] is None
    assert report.agents[1]["stop_category"] == "context_integrity_failed"


async def test_trigger_does_not_claim_compression_when_parent_returns_without_change(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original_agent = builder.Agent

    async def unchanged(*_: Any, **__: Any) -> None:
        return None

    monkeypatch.setattr(original_agent, "compress_context", unchanged)
    with ablation.context_ablation("current") as report:
        agent = _agent()

        async def exactly_threshold(*_: Any, **__: Any) -> int:
            return 38_400

        monkeypatch.setattr(agent.model, "count_tokens", exactly_threshold)
        await agent.compress_context()
    row = report.agents[0]
    assert row["compression_triggers"] == 1
    assert row["compressions_completed"] == 0
    assert row["checks"][0]["compression_triggered"] is True
    assert row["checks"][0]["compression_completed"] is False


async def test_compaction_preserves_candidate_and_counts_only_rewrites() -> None:
    with ablation.context_ablation("retention"):
        agent = _agent()
        session = agent._ablation_session
        session.ttp_submissions = 4
        session.validated_ttp_submission_index = 2
        blocks = []
        result_texts = {}
        for index in range(1, 5):
            result_texts[index] = (
                "<validation_feedback>\n"
                + json.dumps(
                    {
                        "feedback_version": 1,
                        "scope": "full_input_validation",
                        "accepted": index == 2,
                        "candidate_updated": index == 2,
                        "retained_candidate_submission_index": 2,
                    }
                )
                + f"\n</validation_feedback>\nresult-{index}"
            )
            blocks.extend(
                [
                    ToolCallBlock(
                        id=str(index), name="submit_ttp_template", input="{}"
                    ),
                    ToolResultBlock(
                        id=str(index),
                        name="submit_ttp_template",
                        output=[TextBlock(text=result_texts[index])],
                        state=ToolResultState.SUCCESS,
                    ),
                ]
            )
        agent.state.context.append(AssistantMsg("agent", content=blocks))
        observed = []
        progress = ProgressEmitter(request_id="test", observer=observed.append)
        ablation.runner._compact_ttp_history(agent, session, progress=progress)
        results = {
            block.id: block.output[0].text
            for block in agent.state.context[-1].get_content_blocks()
            if isinstance(block, ToolResultBlock)
        }
        assert results == {
            "1": ablation.runner._SUPERSEDED_TTP_RESULT,
            "2": result_texts[2],
            "3": ablation.runner._SUPERSEDED_TTP_RESULT,
            "4": result_texts[4],
        }
        assert session.ttp_history_compacted_interactions == 2
        assert session.ttp_history_compacted_result_chars == len(
            result_texts[1] + result_texts[3]
        )
        assert observed[0].value["retained_interactions"] == 2
        ablation.runner._compact_ttp_history(agent, session, progress=progress)
        assert session.ttp_history_compaction_events == 1
        assert len(observed) == 1


@pytest.mark.parametrize("variant", ["retention", "combined"])
async def test_parameter_rejection_does_not_shift_retained_candidate_pair(
    variant: str,
) -> None:
    result = await ablation.offline_variant(variant, parameter_rejection=True)
    assert result["phase_completed"]
    assert result["stage_calls"] == 5
    assert result["retained_candidate_submission_index"] == 1
    assert result["compacted_interactions"] == 1
    assert result["transport_checks"][-1]["full_submission_result_count"] == 2
    assert result["transport_checks"][-1]["tool_result_count"] == 4
    assert result["transport_checks"][-1]["large_result_present"]


def test_report_correlates_only_valid_uuid() -> None:
    with ablation.context_ablation("current") as report:
        template = _agent()
        session = template._ablation_session
        for request_id in ("16f3a4a3-fc6b-478d-971b-de86cecc7d02", "private-name"):
            builder.build_agent(
                settings=TtpGeneratorSettings(
                    api_key="offline", model_name="synthetic"
                ),
                policy=GenerationPolicy(),
                session=session,
                phase="ttp",
                progress=ProgressEmitter(request_id=request_id),
            )
    assert report.agents[1]["request_id"] == "16f3a4a3-fc6b-478d-971b-de86cecc7d02"
    assert report.agents[2]["request_id"] is None
    assert "private-name" not in json.dumps(report.document())


def test_report_correlates_agents_to_valid_trace_uuid(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    trace_ids = [
        "a71290f6-8a90-45d2-9516-25ff514b84ac",
        "ea0fa0fb-f289-4d47-9a87-9cbb1d221faa",
        None,
        "private-trace-name",
    ]
    values = iter(trace_ids)
    monkeypatch.setattr(
        ablation.observability, "current_laminar_trace_id", lambda: next(values)
    )
    with ablation.context_ablation("current") as report:
        for _ in trace_ids:
            _agent()
    assert [agent["trace_id"] for agent in report.agents] == [
        trace_ids[0],
        trace_ids[1],
        None,
        None,
    ]
    assert "private-trace-name" not in json.dumps(report.document())


def test_wire_facts_projects_only_controlled_role_token_estimates() -> None:
    messages = [
        {"role": "user", "content": "private input"},
        {"role": "tool", "content": "private result"},
        {"role": "private-role", "content": "private value"},
    ]
    facts = ablation._wire_facts(messages, [], "private input")
    assert set(facts["role_estimated_tokens"]) == {
        "system",
        "developer",
        "user",
        "assistant",
        "tool",
    }
    expected = (
        len(
            json.dumps([messages[0]], ensure_ascii=False, separators=(",", ":")).encode(
                "utf-8"
            )
        )
        + 3
    ) // 4
    assert facts["role_estimated_tokens"]["user"] == expected
    assert "private" not in json.dumps(facts)


def test_patch_scope_restores_production_classes_even_on_error() -> None:
    before = builder.Agent, builder.ObservedOpenAIChatModel
    original_compactor = ablation.runner._compact_ttp_history
    with pytest.raises(LookupError), ablation.context_ablation("combined"):
        assert (builder.Agent, builder.ObservedOpenAIChatModel) != before
        with (
            pytest.raises(RuntimeError, match="nested"),
            ablation.context_ablation("current"),
        ):
            pass
        raise LookupError
    assert (builder.Agent, builder.ObservedOpenAIChatModel) == before
    assert ablation.runner._compact_ttp_history is original_compactor


def test_run_delegates_arguments_to_standard_entry_and_writes_safe_report(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    captured: list[list[str]] = []
    original_agent = builder.Agent

    def standard_main(args: list[str]) -> int:
        assert builder.Agent is not original_agent
        captured.append(args)
        return 7

    monkeypatch.setitem(
        sys.modules, "run_test_sets", SimpleNamespace(main=standard_main)
    )
    output = tmp_path / "diagnostics.json"
    args = ["run", "--mode", "ttp-only", "--registry", "evals/datasets.toml"]
    code = ablation.main(
        ["run", "--variant", "current", "--diagnostics", str(output), "--", *args]
    )
    assert code == 7
    assert captured == [args]
    assert json.loads(output.read_text())["variant"] == "current"
    assert builder.Agent is original_agent


def test_adapter_refuses_unverified_agentscope_version(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(ablation, "version", lambda _: "2.1.0")
    with (
        pytest.raises(RuntimeError, match="unsupported AgentScope"),
        ablation.context_ablation("current"),
    ):
        pass
