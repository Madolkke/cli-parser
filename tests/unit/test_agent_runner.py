from __future__ import annotations

import asyncio
import gc
import json
from copy import deepcopy
from typing import Any

import pytest
from agentscope.agent import Agent, ReActConfig
from agentscope.event import ToolResultEndEvent
from agentscope.message import (
    AssistantMsg,
    TextBlock,
    ThinkingBlock,
    ToolCallBlock,
    ToolResultState,
    UserMsg,
)
from agentscope.model import ChatResponse, ChatUsage
from agentscope.state import AgentState
from agentscope.tool import Toolkit

from cli_parser_agent.ttp_generation.agent.middleware import (
    GenerationPhaseMiddleware,
)
from cli_parser_agent.ttp_generation.agent.prompt import (
    SCHEMA_NO_TOOL_RETRY_PROMPT,
    TTP_NO_TOOL_RETRY_PROMPT,
)
from cli_parser_agent.ttp_generation.agent.runner import run_generation_agent
from cli_parser_agent.ttp_generation.agent.tools import (
    SUBMIT_SCHEMA_TOOL_NAME,
    SUBMIT_TEMPLATE_TOOL_NAME,
    GenerationSession,
    SchemaCandidate,
    ValidatorOutcome,
    build_submission_tools,
)


def _schema() -> dict[str, Any]:
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "type": "object",
        "properties": {"value": {"type": "string"}},
        "required": ["value"],
        "additionalProperties": False,
    }


def _schema_call(call_id: str = "schema") -> ChatResponse:
    return _response(
        ToolCallBlock(
            id=call_id,
            name=SUBMIT_SCHEMA_TOOL_NAME,
            input=json.dumps(
                {
                    "result_schema": _schema(),
                    "evidence": [
                        {
                            "path": "/value",
                            "output_index": 0,
                            "excerpt": "one",
                        },
                    ],
                    "assumptions": [],
                },
            ),
        ),
    )


def _template_call(call_id: str = "template") -> ChatResponse:
    return _response(
        ToolCallBlock(
            id=call_id,
            name=SUBMIT_TEMPLATE_TOOL_NAME,
            input=json.dumps({"ttp_template": "value: {{ value }}"}),
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
    )


def _agent(
    model: _ScriptedModel,
    session: GenerationSession,
) -> Agent:
    return Agent(
        name="ttp_generator",
        system_prompt="test",
        model=model,  # type: ignore[arg-type]
        toolkit=Toolkit(tools=build_submission_tools(session)),
        middlewares=[GenerationPhaseMiddleware(session)],
        state=AgentState(),
        react_config=ReActConfig(
            max_iters=session.max_agent_rounds,
            interruption_raise_cancelled_error=True,
        ),
    )


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
    agent = _agent(model, session)

    outcome = await run_generation_agent(
        agent,
        UserMsg(name="user", content="value: one"),
        session,
    )

    assert session.succeeded
    assert outcome.stopped_after_terminal_tool
    assert session.schema_no_tool_responses == 1
    assert session.schema_no_tool_retries == 1
    assert session.ttp_no_tool_responses == 0
    assert session.agent_rounds == 3
    assert model.calls[0]["tool_choice"] is None
    assert SCHEMA_NO_TOOL_RETRY_PROMPT in _message_text(
        model.calls[1]["messages"],
    )
    assert secret not in _message_text(model.calls[1]["messages"])
    assert secret not in agent.state.model_dump_json()
    assert secret not in repr(session)
    assert session.last_issues == ()
    usages = [message.usage for message in agent.state.context if message.usage]
    assert sum(usage.input_tokens for usage in usages) == 22
    assert sum(usage.output_tokens for usage in usages) == 14
    assert len(model.calls) == 3


async def test_terminal_tool_interrupts_reply_without_generator_exit() -> None:
    session = _session()
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
                yield ToolResultEndEvent(
                    reply_id="reply",
                    tool_call_id="template",
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
        outcome = await run_generation_agent(
            agent,
            UserMsg(name="user", content="value: one"),
            session,
        )
        gc.collect()
        await asyncio.sleep(0)
        await asyncio.sleep(0)
    finally:
        loop.set_exception_handler(previous_handler)

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
            _schema_call(),
            _response(TextBlock(text="普通文本")),
            _template_call(),
        ],
    )
    session = _session(
        max_schema_no_tool_retries=0,
        max_ttp_no_tool_retries=1,
    )
    agent = _agent(model, session)

    await run_generation_agent(
        agent,
        UserMsg(name="user", content="value: one"),
        session,
    )

    assert session.succeeded
    assert session.schema_no_tool_responses == 0
    assert session.schema_no_tool_retries == 0
    assert session.ttp_no_tool_responses == 1
    assert session.ttp_no_tool_retries == 1
    assert TTP_NO_TOOL_RETRY_PROMPT in _message_text(model.calls[2]["messages"])


async def test_fourth_consecutive_no_tool_response_exhausts_default_limit() -> None:
    model = _ScriptedModel(
        [_response(TextBlock(text=f"reply-{index}")) for index in range(4)],
    )
    session = _session(max_agent_rounds=8)
    agent = _agent(model, session)

    outcome = await run_generation_agent(
        agent,
        UserMsg(name="user", content="value: one"),
        session,
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
    agent = _agent(model, session)

    outcome = await run_generation_agent(
        agent,
        UserMsg(name="user", content="value: one"),
        session,
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
            _template_call(),
        ],
    )
    session = _session(
        schema_validator=validate_schema,
        max_agent_rounds=8,
        max_schema_no_tool_retries=1,
    )
    agent = _agent(model, session)

    await run_generation_agent(
        agent,
        UserMsg(name="user", content="value: one"),
        session,
    )

    assert session.succeeded
    assert session.schema_no_tool_responses == 2
    assert session.schema_no_tool_retries == 2
    assert session.schema_submissions == 2


async def test_no_tool_retry_cannot_exceed_global_round_budget() -> None:
    model = _ScriptedModel([_response(TextBlock(text="reply"))])
    session = _session(
        max_agent_rounds=1,
        max_schema_no_tool_retries=3,
    )
    agent = _agent(model, session)

    outcome = await run_generation_agent(
        agent,
        UserMsg(name="user", content="value: one"),
        session,
    )

    assert outcome.exceeded_max_iters
    assert not outcome.model_no_tool_retry_limit
    assert session.schema_no_tool_responses == 1
    assert session.schema_no_tool_retries == 0
    assert session.agent_rounds == 1


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
    agent = _agent(model, session)

    outcome = await run_generation_agent(
        agent,
        UserMsg(name="user", content="value: one"),
        session,
    )

    assert outcome.exceeded_max_iters
    assert outcome.submission_tool_call_invalids == 1
    assert session.schema_no_tool_responses == 0
    assert session.schema_submissions == 0


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
        run_generation_agent(
            _CancelledAgent(),
            UserMsg(name="user", content="value: one"),
            session,
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
