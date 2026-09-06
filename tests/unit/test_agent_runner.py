from __future__ import annotations

import asyncio
import gc
import json
import re
import time
from contextlib import contextmanager
from copy import deepcopy
from types import SimpleNamespace
from typing import Any

import pytest
from agentscope.agent import Agent, ReActConfig
from agentscope.event import ToolResultEndEvent
from agentscope.message import (
    AssistantMsg,
    TextBlock,
    ThinkingBlock,
    ToolCallBlock,
    ToolResultBlock,
    ToolResultState,
    UserMsg,
)
from agentscope.model import ChatResponse, ChatUsage
from agentscope.state import AgentState
from agentscope.tool import Toolkit

from cli_parser_agent.ttp_generation.agent import runner as runner_module
from cli_parser_agent.ttp_generation.agent.prompt import (
    SCHEMA_NO_TOOL_RETRY_PROMPT,
    TTP_NO_TOOL_RETRY_PROMPT,
)
from cli_parser_agent.ttp_generation.agent.runner import run_generation_phase
from cli_parser_agent.ttp_generation.agent.tools import (
    FINISH_GENERATION_TOOL_NAME,
    SUBMIT_SCHEMA_TOOL_NAME,
    SUBMIT_TEMPLATE_TOOL_NAME,
    TEST_TEMPLATE_TOOL_NAME,
    GenerationPhase,
    GenerationSession,
    SchemaCandidate,
    ValidatorOutcome,
    build_submission_tools,
)
from cli_parser_agent.ttp_generation.validation import TtpParseResult


def _schema() -> dict[str, Any]:
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "type": "object",
        "properties": {"value": {"type": "string"}},
        "required": ["value"],
        "additionalProperties": False,
    }


def _parse_record_blocks(text: str) -> list[Any]:
    matches = re.findall(
        r'<parsed_record input_index="\d+" display_number="\d+">\n'
        r"(.*?)\n</parsed_record>",
        text,
        flags=re.DOTALL,
    )
    assert matches, text
    return [json.loads(record) for record in matches]


def _schema_call(call_id: str = "schema") -> ChatResponse:
    return _response(
        ToolCallBlock(
            id=call_id,
            name=SUBMIT_SCHEMA_TOOL_NAME,
            input=json.dumps(
                {
                    "result_schema": _schema(),
                },
            ),
        ),
    )


def _template_call(
    call_id: str = "template",
    ttp_template: str = "value: {{ value }}",
) -> ChatResponse:
    return _response(
        ToolCallBlock(
            id=call_id,
            name=SUBMIT_TEMPLATE_TOOL_NAME,
            input=json.dumps({"ttp_template": ttp_template}),
        ),
    )


def _finish_call(call_id: str = "finish") -> ChatResponse:
    return _response(
        ToolCallBlock(
            id=call_id,
            name=FINISH_GENERATION_TOOL_NAME,
            input="{}",
        ),
    )


def _test_call(call_id: str = "test") -> ChatResponse:
    return _response(
        ToolCallBlock(
            id=call_id,
            name=TEST_TEMPLATE_TOOL_NAME,
            input=json.dumps(
                {
                    "text": "value: experimental",
                    "ttp_template": "value: {{ value }}",
                },
            ),
        ),
    )


def _response(*blocks: Any) -> ChatResponse:
    return ChatResponse(
        content=list(blocks),
        is_last=True,
        usage=ChatUsage(input_tokens=11, output_tokens=7, time=0.01),
    )


class _ScriptedModel:
    model = "scripted-model"
    context_size = 128_000
    formatter = SimpleNamespace(supported_input_media_types=())

    def __init__(self, responses: list[ChatResponse]) -> None:
        self.responses = list(responses)
        self.calls: list[dict[str, Any]] = []

    async def __call__(
        self,
        messages: list[Any],
        tools: list[dict[str, Any]] | None = None,
        tool_choice: Any = None,
    ) -> ChatResponse:
        self.calls.append(
            {
                "messages": deepcopy(messages),
                "tools": deepcopy(tools),
                "tool_choice": tool_choice,
            },
        )
        if not self.responses:
            raise AssertionError("scripted model response budget exhausted")
        return self.responses.pop(0)

    async def count_tokens(
        self,
        messages: list[Any],
        tools: list[dict[str, Any]] | None = None,
    ) -> int:
        del messages, tools
        return 1


def _session(
    *,
    schema_validator: Any | None = None,
    max_agent_rounds: int = 12,
    max_schema_no_tool_retries: int = 3,
    max_ttp_no_tool_retries: int = 3,
    max_ttp_submissions: int = 9,
    max_ttp_test_calls: int = 3,
    stream_enabled: bool = False,
) -> GenerationSession:
    return GenerationSession(
        command_outputs=("value: one",),
        schema_validator=(
            schema_validator
            if schema_validator is not None
            else lambda _: ValidatorOutcome(valid=True)
        ),
        template_validator=lambda _: ValidatorOutcome(
            valid=True,
            records=({"value": "one"},),
        ),
        max_agent_rounds=max_agent_rounds,
        max_schema_no_tool_retries=max_schema_no_tool_retries,
        max_ttp_no_tool_retries=max_ttp_no_tool_retries,
        max_ttp_submissions=max_ttp_submissions,
        max_ttp_test_calls=max_ttp_test_calls,
        stream_enabled=stream_enabled,
    )


def _agent(
    model: _ScriptedModel,
    session: GenerationSession,
    phase: GenerationPhase,
    *,
    template_validator: Any | None = None,
) -> Agent:
    if template_validator is not None:
        session.template_validator = template_validator
    return Agent(
        name=f"{phase}_generator",
        system_prompt="test",
        model=model,  # type: ignore[arg-type]
        toolkit=Toolkit(tools=build_submission_tools(session, phase=phase)),
        state=AgentState(),
        react_config=ReActConfig(
            max_iters=session.max_agent_rounds,
            interruption_raise_cancelled_error=True,
        ),
    )


def _freeze_schema(session: GenerationSession) -> None:
    session.frozen_schema = _schema()


def _message_text(messages: list[Any]) -> str:
    texts: list[str] = []
    for message in messages:
        for block in message.get_content_blocks("text"):
            texts.append(block.text)
    return "\n".join(texts)


async def test_schema_no_tool_response_is_removed_then_recovers() -> None:
    secret = "SECRET-FREE-TEXT-MUST-DISAPPEAR"
    model = _ScriptedModel(
        [
            _response(
                ThinkingBlock(thinking=f"hidden {secret}"),
                TextBlock(text=f"ordinary {secret}"),
            ),
            _schema_call(),
            _template_call(),
        ],
    )
    session = _session()
    agent = _agent(model, session, "schema")

    outcome = await run_generation_phase(
        agent,
        UserMsg(name="user", content="value: one"),
        session,
        "schema",
    )

    assert session.schema_is_frozen
    assert not session.succeeded
    assert outcome.phase_completed
    assert outcome.stopped_after_terminal_tool
    assert session.schema_no_tool_responses == 1
    assert session.schema_no_tool_retries == 1
    assert session.ttp_no_tool_responses == 0
    assert session.agent_rounds == 2
    assert session.schema_agent_rounds == 2
    assert session.ttp_agent_rounds == 0
    assert model.calls[0]["tool_choice"] is None
    assert SCHEMA_NO_TOOL_RETRY_PROMPT in _message_text(
        model.calls[1]["messages"],
    )
    assert secret not in _message_text(model.calls[1]["messages"])
    assert secret not in agent.state.model_dump_json()
    assert secret not in repr(session)
    assert session.last_issues == ()
    usages = [message.usage for message in agent.state.context if message.usage]
    assert sum(usage.input_tokens for usage in usages) == 11
    assert sum(usage.output_tokens for usage in usages) == 7
    assert len(model.calls) == 2
    assert len(model.responses) == 1


async def test_agent_round_span_records_safe_round_facts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    spans: list[dict[str, Any]] = []
    finishes: list[dict[str, Any]] = []
    stack: list[str] = []

    @contextmanager
    def start(name: str, **kwargs: Any) -> Any:
        spans.append({"name": name, "parent": stack[-1] if stack else None, **kwargs})
        stack.append(name)
        try:
            yield SimpleNamespace(enabled=True, creates_trace=False)
        finally:
            assert stack.pop() == name

    def finish(**kwargs: Any) -> None:
        finishes.append({"span": stack[-1], **kwargs})

    monkeypatch.setattr(runner_module, "start_laminar_span", start)
    monkeypatch.setattr(runner_module, "finish_laminar_span", finish)
    session = _session()
    agent = _agent(_ScriptedModel([_schema_call()]), session, "schema")

    outcome = await run_generation_phase(
        agent,
        UserMsg(name="user", content="value: one"),
        session,
        "schema",
    )

    assert outcome.phase_completed
    assert [item["name"] for item in spans] == ["agent.round"]
    attributes = finishes[0]["attributes"]
    assert attributes["phase"] == "schema"
    assert attributes["round_index"] == 1
    assert attributes["tool_call"] is True
    assert attributes["tool_names"] == SUBMIT_SCHEMA_TOOL_NAME
    assert "value: one" not in str(attributes)


async def test_terminal_tool_interrupts_reply_without_generator_exit() -> None:
    session = _session()
    _freeze_schema(session)
    loop = asyncio.get_running_loop()
    captured_loop_errors: list[dict[str, Any]] = []
    previous_handler = loop.get_exception_handler()

    class _CleanupYieldingAgent:
        def __init__(self) -> None:
            self.state = AgentState()
            self.state.context.append(
                UserMsg(name="user", content="original context"),
            )
            self.react_config = ReActConfig(max_iters=2)
            self.interrupted = False
            self.generator_exit_cleanup = False
            self.reply_starts = 0

        async def _reply_impl(self):
            try:
                session.validated_ttp_template = "value: {{ value }}"
                session.records = ({"value": "one"},)
                session.generation_finished = True
                session.terminal_reason = "success"
                yield ToolResultEndEvent(
                    reply_id="reply",
                    tool_call_id="finish",
                    state=ToolResultState.SUCCESS,
                )
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                self.interrupted = True
                self.state.context.append(
                    AssistantMsg(
                        name="ttp_generator",
                        content="interruption cleanup",
                        usage=ChatUsage(
                            input_tokens=101,
                            output_tokens=103,
                            time=0.01,
                        ),
                    ),
                )
                yield object()
                raise
            finally:
                if not self.interrupted:
                    self.generator_exit_cleanup = True
                    yield object()

        async def reply_stream(self, message: Any):
            del message
            self.reply_starts += 1
            async for event in self._reply_impl():
                yield event

    agent = _CleanupYieldingAgent()
    original_context = deepcopy(agent.state.context)
    loop.set_exception_handler(
        lambda _loop, context: captured_loop_errors.append(context),
    )
    try:
        outcome = await run_generation_phase(
            agent,
            UserMsg(name="user", content="value: one"),
            session,
            "ttp",
        )
        gc.collect()
        await asyncio.sleep(0)
        await asyncio.sleep(0)
    finally:
        loop.set_exception_handler(previous_handler)

    assert outcome.phase_completed
    assert outcome.stopped_after_terminal_tool
    assert agent.interrupted
    assert not agent.generator_exit_cleanup
    assert agent.reply_starts == 1
    assert agent.state.context == original_context
    assert session.records == ({"value": "one"},)
    assert not any(
        "async generator ignored GeneratorExit" in str(context.get("exception", ""))
        for context in captured_loop_errors
    )
    assert asyncio.current_task() is not None
    assert asyncio.current_task().cancelling() == 0


async def test_ttp_no_tool_response_uses_independent_retry_budget() -> None:
    model = _ScriptedModel(
        [
            _response(TextBlock(text="普通文本")),
            _template_call(),
            _finish_call(),
        ],
    )
    session = _session(
        max_schema_no_tool_retries=0,
        max_ttp_no_tool_retries=1,
    )
    _freeze_schema(session)
    agent = _agent(model, session, "ttp")

    outcome = await run_generation_phase(
        agent,
        UserMsg(name="user", content="value: one"),
        session,
        "ttp",
    )

    assert outcome.phase_completed
    assert session.succeeded
    assert session.schema_no_tool_responses == 0
    assert session.schema_no_tool_retries == 0
    assert session.ttp_no_tool_responses == 1
    assert session.ttp_no_tool_retries == 1
    assert session.schema_agent_rounds == 0
    assert session.ttp_agent_rounds == 3
    assert TTP_NO_TOOL_RETRY_PROMPT in _message_text(model.calls[1]["messages"])


async def test_test_call_budget_refuses_without_counting_or_erroring() -> None:
    """A refusal is a budget fact, not a tool error.

    Passing trials use at most two test calls; runaway ones use 25+ and can
    spend an entire round budget on experiments without ever submitting a
    candidate. The refusal must stay non-terminal and must not increment
    tool_result_errors, which would trip the submission_tool_call_invalid path.
    """
    model = _ScriptedModel(
        [_test_call("test-1"), _test_call("test-2"), _template_call(), _finish_call()],
    )
    session = _session(max_agent_rounds=6, max_ttp_test_calls=1)
    session.ttp_test_validator = lambda candidate: TtpParseResult(
        result=[{"value": "experimental"}],
        issues=[],
    )
    _freeze_schema(session)
    agent = _agent(model, session, "ttp")

    outcome = await run_generation_phase(
        agent,
        UserMsg(name="user", content="value: one"),
        session,
        "ttp",
    )

    assert outcome.phase_completed
    assert session.succeeded
    # The executed count stops at the limit; the refusal is tracked apart.
    assert session.ttp_test_calls == 1
    assert session.ttp_test_calls_refused == 1
    assert session.tool_result_errors == 0
    assert session.submission_tool_call_invalids == 0

    refusals = [
        block
        for message in model.calls[2]["messages"]
        for block in message.content
        if isinstance(block, ToolResultBlock) and block.id == "test-2"
    ]
    assert len(refusals) == 1
    assert refusals[0].state is not ToolResultState.ERROR
    assert "调用次数已用尽" in refusals[0].output[0].text


async def test_zero_test_call_budget_withholds_the_tool_entirely() -> None:
    session = _session(max_ttp_test_calls=0)
    _freeze_schema(session)

    names = {tool.name for tool in build_submission_tools(session, phase="ttp")}

    assert TEST_TEMPLATE_TOOL_NAME not in names
    assert {SUBMIT_TEMPLATE_TOOL_NAME, FINISH_GENERATION_TOOL_NAME} <= names


async def test_ttp_test_tool_is_non_terminal_and_preserves_result_context() -> None:
    model = _ScriptedModel(
        [
            _test_call(),
            _template_call(),
            _finish_call(),
        ],
    )
    session = _session(max_agent_rounds=4)
    session.ttp_test_validator = lambda candidate: TtpParseResult(
        result=[{"value": "experimental"}],
        issues=[],
    )
    _freeze_schema(session)
    agent = _agent(model, session, "ttp")

    outcome = await run_generation_phase(
        agent,
        UserMsg(name="user", content="value: one"),
        session,
        "ttp",
    )

    assert outcome.phase_completed
    assert session.succeeded
    assert session.ttp_test_calls == 1
    assert session.ttp_submissions == 1
    assert len(model.calls) == 3
    test_results = [
        block
        for message in model.calls[1]["messages"]
        for block in message.content
        if isinstance(block, ToolResultBlock) and block.id == "test"
    ]
    assert len(test_results) == 1
    assert _parse_record_blocks(test_results[0].output[0].text) == [
        [{"value": "experimental"}],
    ]


async def test_rejected_ttp_feedback_remains_in_same_phase_context() -> None:
    rejected_template = "rejected: {{ value }}"
    corrected_template = "value: {{ value }}"
    issue = {
        "code": "ttp.test_rejected",
        "stage": "template",
        "message": "Use the captured value to correct the template.",
    }
    captured_record = {"value": "captured-one"}
    submitted_templates: list[str] = []

    def validate_template(candidate: Any) -> ValidatorOutcome:
        submitted_templates.append(candidate.ttp_template)
        if len(submitted_templates) == 1:
            return ValidatorOutcome(
                valid=False,
                issues=(issue,),
                records=(captured_record,),
            )
        return ValidatorOutcome(
            valid=True,
            records=({"value": "one"},),
        )

    model = _ScriptedModel(
        [
            _template_call("template-rejected", rejected_template),
            _template_call("template-accepted", corrected_template),
            _finish_call(),
        ],
    )
    session = _session(max_agent_rounds=4)
    session.template_validator = validate_template
    _freeze_schema(session)
    agent = _agent(model, session, "ttp")

    outcome = await run_generation_phase(
        agent,
        UserMsg(name="user", content="value: one"),
        session,
        "ttp",
    )

    assert len(model.calls) == 3
    second_request_messages = model.calls[1]["messages"]
    tool_results = [
        block
        for message in second_request_messages
        for block in message.content
        if isinstance(block, ToolResultBlock)
    ]
    assert len(tool_results) == 1
    assert tool_results[0].id == "template-rejected"
    assert len(tool_results[0].output) == 1
    assert _parse_record_blocks(tool_results[0].output[0].text) == [
        captured_record,
    ]
    assert "accepted" not in tool_results[0].output[0].text
    assert issue["message"] not in tool_results[0].output[0].text

    assert outcome.phase_completed
    assert outcome.stopped_after_terminal_tool
    assert session.succeeded
    assert session.first_ttp_valid is False
    assert session.ttp_submissions == 2
    assert session.ttp_agent_rounds == 3
    assert submitted_templates == [rejected_template, corrected_template]
    assert session.validated_ttp_template == corrected_template
    assert session.records == ({"value": "one"},)


async def test_ttp_history_compacts_only_stale_submission_results() -> None:
    """History keeps tool inputs and thinking while replacing old captures."""

    records_by_submission = [
        ({"value": "first-attempt"},),
        ({"value": "second-attempt"},),
        ({"value": "one"},),
    ]
    submissions: list[str] = []

    def validate_template(candidate: Any) -> ValidatorOutcome:
        index = len(submissions)
        submissions.append(candidate.ttp_template)
        return ValidatorOutcome(
            valid=index == len(records_by_submission) - 1,
            records=records_by_submission[index],
            issues=(
                ()
                if index == len(records_by_submission) - 1
                else ({"code": f"wrong_{index}", "message": "secret feedback"},)
            ),
        )

    model = _ScriptedModel(
        [
            _response(
                ThinkingBlock(thinking="Preserve this reasoning."),
                ToolCallBlock(
                    id="t1",
                    name=SUBMIT_TEMPLATE_TOOL_NAME,
                    input=json.dumps({"ttp_template": "first: {{ value }}"}),
                ),
            ),
            _template_call("t2", "second: {{ value }}"),
            _template_call("t3", "value: {{ value }}"),
            _finish_call(),
        ],
    )
    session = _session(max_agent_rounds=6)
    _freeze_schema(session)
    agent = _agent(model, session, "ttp", template_validator=validate_template)

    outcome = await run_generation_phase(
        agent,
        UserMsg(name="user", content="value: one"),
        session,
        "ttp",
    )

    assert outcome.phase_completed
    assert session.succeeded
    assert session.ttp_history_compacted_interactions == 2
    assert session.ttp_history_compacted_input_chars == 0
    assert session.ttp_history_compaction_skips == 0
    final_blocks = [
        block for message in model.calls[-1]["messages"] for block in message.content
    ]
    assert [
        json.loads(block.input)["ttp_template"]
        for block in final_blocks
        if isinstance(block, ToolCallBlock)
    ] == submissions
    assert [
        block.thinking for block in final_blocks if isinstance(block, ThinkingBlock)
    ] == ["Preserve this reasoning."]
    result_texts = [
        block.output[0].text
        for block in final_blocks
        if isinstance(block, ToolResultBlock)
    ]
    assert result_texts[:2] == ["该次提交的匹配结果已被后续提交取代"] * 2
    assert _parse_record_blocks(result_texts[2]) == [{"value": "one"}]
    for forbidden in (
        "first-attempt",
        "second-attempt",
        "accepted",
        "wrong_0",
        "wrong_1",
        "secret feedback",
        "remaining_submissions",
        "validated_candidate_available",
        "next_action",
    ):
        assert forbidden not in "\n".join(result_texts)

    context_before = deepcopy(agent.state.context)
    events_before = session.ttp_history_compaction_events
    runner_module._compact_ttp_history(agent, session)
    assert agent.state.context == context_before
    assert session.ttp_history_compaction_events == events_before
    assert session.ttp_history_compacted_interactions == 2


async def test_ttp_history_preserves_tests_and_latest_valid_candidate() -> None:
    original_template = "value: {{ value | ORPHRASE }}"
    accepted_template = "value: {{ value }}"
    failed_template = "wrong: {{ value }}"
    model = _ScriptedModel(
        [
            _template_call("original", original_template),
            _test_call("test-before-correction"),
            _template_call("accepted", accepted_template),
            _test_call("test-before-failure"),
            _template_call("failed", failed_template),
            _test_call("test-after-failure"),
            _finish_call(),
        ],
    )

    def validate_template(candidate: Any) -> ValidatorOutcome:
        if candidate.ttp_template == original_template:
            return ValidatorOutcome(valid=True, records=({"value": "overcaptured"},))
        if candidate.ttp_template == accepted_template:
            return ValidatorOutcome(valid=True, records=({"value": "one"},))
        return ValidatorOutcome(
            valid=False,
            issues=({"code": "wrong_candidate", "message": "secret feedback"},),
            records=({"value": "bad"},),
        )

    session = _session(max_agent_rounds=8)
    session.template_validator = validate_template
    session.ttp_test_validator = lambda _: TtpParseResult(
        result=[{"value": "experimental"}],
        issues=[],
    )
    _freeze_schema(session)
    agent = _agent(model, session, "ttp")

    outcome = await run_generation_phase(
        agent,
        UserMsg(name="user", content="value: one"),
        session,
        "ttp",
    )

    assert outcome.phase_completed
    assert session.succeeded
    assert session.validated_ttp_template == accepted_template
    assert session.records == ({"value": "one"},)
    assert session.validated_ttp_candidate_version == 2
    assert session.validated_ttp_submission_index == 2
    assert session.ttp_test_calls == 3
    assert session.ttp_history_compacted_interactions == 2
    assert session.ttp_history_compacted_input_chars == 0
    final_blocks = [
        block for message in model.calls[-1]["messages"] for block in message.content
    ]
    calls_by_id = {
        block.id: block for block in final_blocks if isinstance(block, ToolCallBlock)
    }
    for call_id, template in (
        ("original", original_template),
        ("accepted", accepted_template),
        ("failed", failed_template),
    ):
        assert json.loads(calls_by_id[call_id].input) == {"ttp_template": template}
    results_by_id = {
        block.id: block.output[0].text
        for block in final_blocks
        if isinstance(block, ToolResultBlock)
    }
    for call_id in ("original", "accepted"):
        assert results_by_id[call_id] == "该次提交的匹配结果已被后续提交取代"
    assert _parse_record_blocks(results_by_id["failed"]) == [{"value": "bad"}]
    for call_id in (
        "test-before-correction",
        "test-before-failure",
        "test-after-failure",
    ):
        assert json.loads(calls_by_id[call_id].input) == {
            "text": "value: experimental",
            "ttp_template": "value: {{ value }}",
        }
        assert _parse_record_blocks(results_by_id[call_id]) == [
            [{"value": "experimental"}],
        ]
    for forbidden in (
        "overcaptured",
        "secret feedback",
        "wrong_candidate",
        "accepted",
        "candidate_updated",
        "retained_candidate_version",
        "validation_summary",
    ):
        assert forbidden not in "\n".join(results_by_id.values())


@pytest.mark.parametrize(
    "invalid_pairing",
    [
        "missing_result",
        "orphan_result",
        "duplicate_call",
        "duplicate_result",
        "wrong_name",
    ],
)
def test_ttp_history_skips_ambiguous_or_incomplete_pairs(invalid_pairing: str) -> None:
    blocks: list[Any] = []
    for call_id in ("first", "second"):
        blocks.extend(
            [
                ToolCallBlock(
                    id=call_id,
                    name=SUBMIT_TEMPLATE_TOOL_NAME,
                    input=json.dumps({"ttp_template": f"{call_id}: {{{{ value }}}}"}),
                ),
                ToolResultBlock(
                    id=call_id,
                    name=SUBMIT_TEMPLATE_TOOL_NAME,
                    output=[TextBlock(text=f"capture-{call_id}")],
                    state=ToolResultState.SUCCESS,
                ),
            ],
        )
    if invalid_pairing == "missing_result":
        blocks.pop()
    elif invalid_pairing == "orphan_result":
        blocks.pop(2)
    elif invalid_pairing == "duplicate_call":
        blocks.append(deepcopy(blocks[0]))
    elif invalid_pairing == "duplicate_result":
        blocks.append(deepcopy(blocks[1]))
    else:
        blocks[1] = blocks[1].model_copy(update={"name": TEST_TEMPLATE_TOOL_NAME})
    context = [AssistantMsg(name="ttp_generator", content=blocks)]
    agent = SimpleNamespace(state=SimpleNamespace(context=context))
    session = _session()
    session.ttp_submissions = 2
    context_before = deepcopy(context)

    runner_module._compact_ttp_history(agent, session)

    assert context == context_before
    assert session.ttp_history_compaction_skips == 1
    assert session.ttp_history_compaction_events == 0
    assert session.ttp_history_compacted_interactions == 0


async def test_token_counters_accumulate_from_model_call_end_events() -> None:
    """Token usage must land locally, not only as Laminar span attributes.

    input_tokens_last is the context size of the final call, which is what the
    history compaction exists to hold down; without it, measuring context growth
    means querying Laminar by hand.
    """
    model = _ScriptedModel([_template_call(), _finish_call()])
    session = _session(max_agent_rounds=3, stream_enabled=True)
    _freeze_schema(session)
    agent = _agent(model, session, "ttp")

    await run_generation_phase(
        agent,
        UserMsg(name="user", content="value: one"),
        session,
        "ttp",
    )

    # _response() reports 11 input and 7 output tokens per call.
    assert session.model_calls_observed == len(model.calls) == 2
    assert session.input_tokens_total == 22
    assert session.output_tokens_total == 14
    assert session.input_tokens_last == 11
    assert session.stream_first_delta_seconds is not None
    assert session.stream_chunk_count >= 1
    assert session.stream_tool_call_delta_count >= 0
    assert session.stream_usage_seen


async def test_valid_template_submission_waits_for_explicit_finish() -> None:
    model = _ScriptedModel([_template_call(), _finish_call()])
    session = _session(max_agent_rounds=3)
    _freeze_schema(session)
    agent = _agent(model, session, "ttp")

    outcome = await run_generation_phase(
        agent,
        UserMsg(name="user", content="value: one"),
        session,
        "ttp",
    )

    assert len(model.calls) == 2
    second_request_results = [
        block
        for message in model.calls[1]["messages"]
        for block in message.content
        if isinstance(block, ToolResultBlock)
    ]
    assert len(second_request_results) == 1
    assert _parse_record_blocks(second_request_results[0].output[0].text) == [
        {"value": "one"},
    ]
    assert outcome.phase_completed
    assert outcome.stopped_after_terminal_tool
    assert session.ttp_submissions == 1
    assert session.ttp_agent_rounds == 2
    assert session.generation_finished
    assert session.succeeded


async def test_rejected_finish_continues_until_candidate_is_finished() -> None:
    model = _ScriptedModel(
        [
            _finish_call("finish-rejected"),
            _template_call("template-accepted"),
            _finish_call("finish-accepted"),
        ],
    )
    session = _session(max_agent_rounds=4)
    _freeze_schema(session)
    agent = _agent(model, session, "ttp")

    outcome = await run_generation_phase(
        agent,
        UserMsg(name="user", content="value: one"),
        session,
        "ttp",
    )

    assert len(model.calls) == 3
    second_request_results = [
        block
        for message in model.calls[1]["messages"]
        for block in message.content
        if isinstance(block, ToolResultBlock)
    ]
    assert len(second_request_results) == 1
    rejected_payload = json.loads(second_request_results[0].output[0].text)
    assert rejected_payload["accepted"] is False
    assert rejected_payload["generation_finished"] is False
    assert rejected_payload["issues"][0]["code"] == (
        "generation.finish_without_valid_candidate"
    )
    assert outcome.phase_completed
    assert outcome.stopped_after_terminal_tool
    assert session.ttp_submissions == 1
    assert session.ttp_agent_rounds == 3
    assert session.succeeded


async def test_reaching_ttp_submission_limit_stops_before_finish() -> None:
    validations = 0

    def validate_template(_: Any) -> ValidatorOutcome:
        nonlocal validations
        validations += 1
        return ValidatorOutcome(
            valid=validations == 2,
            issues=() if validations == 2 else ({"code": "retry"},),
            records=({"value": "one"},),
        )

    model = _ScriptedModel(
        [
            _template_call("template-rejected", "bad: {{ value }}"),
            _template_call("template-valid-at-limit"),
            _finish_call("must-not-run"),
        ],
    )
    session = _session(max_agent_rounds=4, max_ttp_submissions=2)
    session.template_validator = validate_template
    _freeze_schema(session)
    agent = _agent(model, session, "ttp")

    outcome = await run_generation_phase(
        agent,
        UserMsg(name="user", content="value: one"),
        session,
        "ttp",
    )

    assert len(model.calls) == 2
    assert len(model.responses) == 1
    assert validations == 2
    assert session.ttp_submissions == 2
    assert session.has_validated_ttp_candidate
    assert not session.generation_finished
    assert not session.succeeded
    assert session.terminal_reason == "ttp_submission_limit"
    assert not outcome.phase_completed
    assert outcome.stopped_after_terminal_tool


async def test_fourth_consecutive_no_tool_response_exhausts_default_limit() -> None:
    model = _ScriptedModel(
        [_response(TextBlock(text=f"reply-{index}")) for index in range(4)],
    )
    session = _session(max_agent_rounds=8)
    agent = _agent(model, session, "schema")

    outcome = await run_generation_phase(
        agent,
        UserMsg(name="user", content="value: one"),
        session,
        "schema",
    )

    assert not session.succeeded
    assert outcome.model_no_tool_retry_limit
    assert session.terminal_reason == "model_no_tool_retry_limit"
    assert session.schema_no_tool_responses == 4
    assert session.schema_no_tool_retries == 3
    assert session.agent_rounds == 4


async def test_zero_disables_no_tool_retry() -> None:
    model = _ScriptedModel([_response(TextBlock(text="reply"))])
    session = _session(max_schema_no_tool_retries=0)
    agent = _agent(model, session, "schema")

    outcome = await run_generation_phase(
        agent,
        UserMsg(name="user", content="value: one"),
        session,
        "schema",
    )

    assert outcome.model_no_tool_retry_limit
    assert session.schema_no_tool_responses == 1
    assert session.schema_no_tool_retries == 0
    assert len(model.calls) == 1


async def test_expected_tool_call_resets_consecutive_no_tool_count() -> None:
    rejected_once = False

    def validate_schema(candidate: SchemaCandidate) -> ValidatorOutcome:
        nonlocal rejected_once
        del candidate
        if not rejected_once:
            rejected_once = True
            return ValidatorOutcome(
                valid=False,
                issues=({"code": "retry", "message": "retry"},),
            )
        return ValidatorOutcome(valid=True)

    model = _ScriptedModel(
        [
            _response(TextBlock(text="first")),
            _schema_call("schema-rejected"),
            _response(TextBlock(text="second")),
            _schema_call("schema-accepted"),
        ],
    )
    session = _session(
        schema_validator=validate_schema,
        max_agent_rounds=8,
        max_schema_no_tool_retries=1,
    )
    agent = _agent(model, session, "schema")

    outcome = await run_generation_phase(
        agent,
        UserMsg(name="user", content="value: one"),
        session,
        "schema",
    )

    assert outcome.phase_completed
    assert session.schema_is_frozen
    assert not session.succeeded
    assert session.schema_no_tool_responses == 2
    assert session.schema_no_tool_retries == 2
    assert session.schema_submissions == 2


async def test_no_tool_retry_cannot_exceed_global_round_budget() -> None:
    model = _ScriptedModel([_response(TextBlock(text="reply"))])
    session = _session(
        max_agent_rounds=1,
        max_schema_no_tool_retries=3,
    )
    agent = _agent(model, session, "schema")

    outcome = await run_generation_phase(
        agent,
        UserMsg(name="user", content="value: one"),
        session,
        "schema",
    )

    assert outcome.exceeded_max_iters
    assert not outcome.model_no_tool_retry_limit
    assert session.schema_no_tool_responses == 1
    assert session.schema_no_tool_retries == 0
    assert session.agent_rounds == 1


async def test_round_is_not_started_without_time_to_finish_it() -> None:
    """A round that cannot fit one model call must not be started.

    Observed live runs overran a 360s budget by up to 374s because the loop
    only checked the round count, so a round beginning near the deadline ran
    to completion well past it.
    """

    model = _ScriptedModel([_response(TextBlock(text="never requested"))])
    session = _session(max_agent_rounds=12)
    session.deadline_monotonic = time.monotonic() + 1.0
    session.min_round_seconds = 60.0
    agent = _agent(model, session, "schema")

    outcome = await run_generation_phase(
        agent,
        UserMsg(name="user", content="value: one"),
        session,
        "schema",
    )

    assert session.agent_rounds == 0
    assert model.calls == []
    assert session.terminal_reason == "generation_timeout"
    assert not outcome.phase_completed


async def test_round_still_starts_when_enough_budget_remains() -> None:
    model = _ScriptedModel([_schema_call()])
    session = _session(max_agent_rounds=12)
    session.deadline_monotonic = time.monotonic() + 600.0
    session.min_round_seconds = 60.0
    agent = _agent(model, session, "schema")

    outcome = await run_generation_phase(
        agent,
        UserMsg(name="user", content="value: one"),
        session,
        "schema",
    )

    assert session.agent_rounds == 1
    assert session.terminal_reason != "generation_timeout"
    assert outcome.phase_completed


async def test_missing_deadline_keeps_rounds_only_behaviour() -> None:
    model = _ScriptedModel([_schema_call()])
    session = _session(max_agent_rounds=12)
    session.deadline_monotonic = None
    session.min_round_seconds = 60.0
    agent = _agent(model, session, "schema")

    outcome = await run_generation_phase(
        agent,
        UserMsg(name="user", content="value: one"),
        session,
        "schema",
    )

    assert outcome.phase_completed
    assert session.agent_rounds == 1


async def test_deadline_stops_agentscope_inner_react_loop_at_a_tool_result() -> None:
    """The budget must also bind inside a single ``reply_stream``.

    AgentScope's own ReAct loop (`_agent.py` ``while cur_iter < max_iters``)
    issues further model calls within one reply, so a guard that only runs
    between outer iterations never fires during the common timeout path.
    """

    model = _ScriptedModel(
        [
            _template_call("t1", "a: {{ value }}"),
            # Any further call means the deadline was ignored.
            _template_call("t2", "b: {{ value }}"),
            _template_call("t3", "c: {{ value }}"),
        ],
    )
    session = _session(max_agent_rounds=8)

    def slow_validator(candidate: Any) -> ValidatorOutcome:
        # Burn the remaining budget while the first submission is validated.
        time.sleep(0.8)
        return ValidatorOutcome(valid=False, records=({"value": "x"},))

    session.template_validator = slow_validator
    _freeze_schema(session)
    # Wide margins on both sides: the first round must comfortably start
    # (remaining 2.0 >= 0.5) and the second must comfortably be refused
    # (remaining ~1.2 - 0.8 < 0.5) even on a loaded machine.
    session.min_round_seconds = 0.5
    session.deadline_monotonic = time.monotonic() + 1.2
    agent = _agent(model, session, "ttp")

    outcome = await run_generation_phase(
        agent,
        UserMsg(name="user", content="value: one"),
        session,
        "ttp",
    )

    assert len(model.calls) == 1
    assert session.ttp_submissions == 1
    assert session.terminal_reason == "generation_timeout"
    assert not outcome.phase_completed


async def test_malformed_submission_tool_call_is_counted_separately() -> None:
    malformed = _response(
        ToolCallBlock(
            id="malformed",
            name=SUBMIT_SCHEMA_TOOL_NAME,
            input='{"result_schema":',
        ),
    )
    model = _ScriptedModel([malformed])
    session = _session(max_agent_rounds=1)
    agent = _agent(model, session, "schema")

    outcome = await run_generation_phase(
        agent,
        UserMsg(name="user", content="value: one"),
        session,
        "schema",
    )

    assert outcome.exceeded_max_iters
    assert outcome.submission_tool_call_invalids == 1
    assert session.schema_no_tool_responses == 0
    assert session.schema_submissions == 0


async def test_malformed_finish_after_valid_candidate_is_counted_separately() -> None:
    malformed_finish = _response(
        ToolCallBlock(
            id="malformed-finish",
            name=FINISH_GENERATION_TOOL_NAME,
            input=json.dumps({"unexpected": True}),
        ),
    )
    model = _ScriptedModel([_template_call(), malformed_finish])
    session = _session(max_agent_rounds=2)
    _freeze_schema(session)
    agent = _agent(model, session, "ttp")

    outcome = await run_generation_phase(
        agent,
        UserMsg(name="user", content="value: one"),
        session,
        "ttp",
    )

    assert outcome.exceeded_max_iters
    assert outcome.ended_after_invalid_tool_call
    assert outcome.submission_tool_call_invalids == 1
    assert session.has_validated_ttp_candidate
    assert not session.generation_finished
    assert not session.succeeded
    assert session.ttp_submissions == 1
    assert session.ttp_no_tool_responses == 0


async def test_cancellation_propagates_without_becoming_no_tool_retry() -> None:
    entered = asyncio.Event()

    class _CancelledAgent:
        state = AgentState()
        react_config = ReActConfig(max_iters=2)

        async def reply_stream(self, message: Any):
            del message
            entered.set()
            await asyncio.Event().wait()
            if False:
                yield None

    session = _session(max_agent_rounds=2)
    task = asyncio.create_task(
        run_generation_phase(
            _CancelledAgent(),
            UserMsg(name="user", content="value: one"),
            session,
            "schema",
        ),
    )
    await entered.wait()
    external_cancel_token = object()
    task.cancel(external_cancel_token)

    with pytest.raises(asyncio.CancelledError) as caught:
        await task

    assert caught.value.args == (external_cancel_token,)
    assert session.schema_no_tool_responses == 0
    assert session.schema_no_tool_retries == 0
