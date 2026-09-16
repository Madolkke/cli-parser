from __future__ import annotations

import asyncio
from typing import Any

import pytest
from agentscope.agent import ReActConfig
from agentscope.event import (
    ModelCallEndEvent,
    ModelCallStartEvent,
    ToolCallStartEvent,
    ToolResultEndEvent,
)
from agentscope.message import ToolResultState, UserMsg
from agentscope.state import AgentState
from test_generator import _closed_schema, _settings, _template_only_workflow

from cli_parser_agent import GenerationRequest, TtpGenerator
from cli_parser_agent.ttp_generation.agent.runner import run_generation_phase
from cli_parser_agent.ttp_generation.contracts import ValidationIssue
from cli_parser_agent.ttp_generation.workflow import _GenerationWorkflow


def _facts_event(events: list[Any]) -> Any:
    matches = [
        event
        for event in events
        if getattr(event, "name", "") == "cli_parser.generation.execution_facts"
    ]
    assert len(matches) == 1
    return matches[0]


async def test_one_generator_keeps_interleaved_request_facts_separate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    frozen_ready = asyncio.Event()
    empty_ready = asyncio.Event()

    async def interleave(self: _GenerationWorkflow):
        if self.request.command_outputs[0] == "value: frozen":
            self.session.frozen_schema = _closed_schema()
            self.session.validated_ttp_template = "value: {{ value }}"
            self.session.finish_called = True
            frozen_ready.set()
            await empty_ready.wait()
        else:
            await frozen_ready.wait()
            empty_ready.set()
        return self._failure(
            "agent_round_limit",
            [
                ValidationIssue(
                    code="generation.agent_round_limit",
                    stage="budget",
                    message="The reasoning round budget was exhausted.",
                )
            ],
        )

    monkeypatch.setattr(_GenerationWorkflow, "run", interleave)
    generator = TtpGenerator(settings=_settings())
    frozen_events: list[Any] = []
    empty_events: list[Any] = []

    frozen_result, empty_result = await asyncio.wait_for(
        asyncio.gather(
            generator.generate(
                GenerationRequest(command_outputs=["value: frozen"]),
                observer=frozen_events.append,
            ),
            generator.generate(
                GenerationRequest(command_outputs=["value: empty"]),
                observer=empty_events.append,
            ),
        ),
        timeout=5,
    )

    frozen_facts = _facts_event(frozen_events)
    empty_facts = _facts_event(empty_events)
    assert frozen_facts.value["schema_frozen"]
    assert frozen_facts.value["valid_ttp_candidate"]
    assert frozen_facts.value["finish_called"]
    assert not frozen_facts.value["finish_succeeded"]
    assert not any(empty_facts.value.values())
    assert frozen_result.metadata.request_id != empty_result.metadata.request_id
    for result, events in (
        (frozen_result, frozen_events),
        (empty_result, empty_events),
    ):
        assert all(
            event.metadata["request_id"] == result.metadata.request_id
            for event in events
        )


async def test_cancel_inside_final_acceptance_retains_finish_facts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    acceptance_entered = asyncio.Event()

    async def accepted_tools(self: _GenerationWorkflow):
        self.session.frozen_schema = _closed_schema()
        self.session.validated_ttp_template = "value: {{ value }}"
        self.session.finish_called = True
        self.session.generation_finished = True
        self.execution_facts.entered_ttp = True
        return await self._accept_artifact()

    async def suspended_acceptance(self: _GenerationWorkflow, summary: dict):
        del self, summary
        acceptance_entered.set()
        await asyncio.Event().wait()
        raise AssertionError("cancelled acceptance must not finish")

    monkeypatch.setattr(_GenerationWorkflow, "run", accepted_tools)
    monkeypatch.setattr(
        _GenerationWorkflow, "_accept_artifact_impl", suspended_acceptance
    )
    events: list[Any] = []
    task = asyncio.create_task(
        TtpGenerator(settings=_settings()).generate(
            GenerationRequest(command_outputs=["value: one"]), observer=events.append
        )
    )
    await asyncio.wait_for(acceptance_entered.wait(), timeout=5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    facts = _facts_event(events).value
    assert facts["schema_frozen"] and facts["entered_ttp"]
    assert facts["valid_ttp_candidate"]
    assert facts["finish_called"] and facts["finish_succeeded"]
    assert facts["final_acceptance_started"]
    assert not facts["final_acceptance_passed"]
    completed = next(
        event
        for event in events
        if getattr(event, "name", "") == "cli_parser.final_validation.completed"
    )
    assert completed.value == {"status": "cancelled", "valid": False}


@pytest.mark.parametrize(
    ("tool_name", "finish_called"),
    [("finish_generation", True), ("submit_ttp_template", False)],
)
async def test_runner_counts_finish_start_even_when_tool_arguments_fail(
    tool_name: str,
    finish_called: bool,
) -> None:
    workflow = _template_only_workflow(_closed_schema())
    workflow._freeze_injected_schema(workflow.injected_schema)

    class EventStream:
        """Exercise runner event handling without invoking an LLM adapter."""

        def __init__(self):
            self.state = AgentState()
            self.react_config = ReActConfig(max_iters=1)

        async def reply_stream(self, message):
            self.state.context.append(message)
            yield ModelCallStartEvent(reply_id="reply", model_name="offline")
            yield ToolCallStartEvent(
                reply_id="reply", tool_call_id="call", tool_call_name=tool_name
            )
            assert workflow.session.finish_called is finish_called
            yield ModelCallEndEvent(
                reply_id="reply", model_name="offline", input_tokens=0, output_tokens=0
            )
            yield ToolResultEndEvent(
                reply_id="reply", tool_call_id="call", state=ToolResultState.ERROR
            )

    outcome = await run_generation_phase(
        EventStream(),
        UserMsg(name="user", content="value: one"),
        workflow.session,
        "ttp",
    )
    workflow.execution_facts.capture(workflow.session)

    assert outcome.tool_call_starts == 1
    assert outcome.tool_result_errors == 1
    assert workflow.execution_facts.finish_called is finish_called
    assert not workflow.execution_facts.finish_succeeded
    assert not workflow.execution_facts.valid_ttp_candidate
