"""Retained Schema reasoning is bounded before native compression can run."""

from __future__ import annotations

import asyncio
import json
import time
from typing import Any

import pytest
from agentscope.agent import ContextConfig
from agentscope.credential import OpenAICredential
from agentscope.formatter import OpenAIChatFormatter
from agentscope.message import AssistantMsg, ThinkingBlock, UserMsg
from agentscope.model import OpenAIChatModel

from cli_parser_agent import GenerationPolicy, TtpGeneratorSettings
from cli_parser_agent.ttp_generation.agent.builder import build_agent
from cli_parser_agent.ttp_generation.agent.model_attempts import (
    ModelAttemptRecorder,
    ObservedOpenAIChatModel,
    _ProviderReplyFacts,
)
from cli_parser_agent.ttp_generation.agent.schema_reasoning_formatter import (
    SchemaReasoningOpenAIFormatter,
)
from cli_parser_agent.ttp_generation.agent.schema_reasoning_guard import (
    SchemaReasoningContextLimit,
    SchemaReasoningHistoryGuard,
)
from cli_parser_agent.ttp_generation.agent.session import GenerationSession
from cli_parser_agent.ttp_generation.progress import ProgressEmitter


def _session() -> GenerationSession:
    return GenerationSession(
        command_outputs=("synthetic",),
        schema_validator=lambda _: None,
        template_validator=lambda _: None,
    )


def _agent(
    *, phase: str = "schema", events: list[Any] | None = None, **settings: Any
) -> tuple[Any, GenerationSession]:
    session = _session()
    agent = build_agent(
        settings=TtpGeneratorSettings(
            api_key="offline",
            model_name="deepseek-v4-flash",
            base_url="https://api.deepseek.com",
            max_tokens=8192,
            **settings,
        ),
        policy=GenerationPolicy(),
        session=session,
        phase=phase,
        progress=ProgressEmitter("offline-guard", events.append)
        if events is not None
        else None,
    )
    agent.state.context = [
        UserMsg(name="user", content="synthetic task"),
        AssistantMsg(
            name="model", content=[ThinkingBlock(thinking="private reasoning")]
        ),
    ]
    return agent, session


@pytest.mark.parametrize(
    ("phase", "base_url", "parameters", "extra", "enabled"),
    [
        ("schema", "https://api.deepseek.com", {}, None, True),
        ("schema", "https://api.deepseek.com/v1", {}, None, True),
        ("ttp", "https://api.deepseek.com", {}, None, False),
        ("schema", "https://api.deepseek.com.example.org", {}, None, False),
        ("schema", "https://api.openai.com/v1", {}, None, False),
        (
            "schema",
            "https://api.deepseek.com",
            {"thinking_enable": True, "reasoning_effort": "none"},
            None,
            False,
        ),
        (
            "schema",
            "https://api.deepseek.com",
            {},
            {"thinking": {"type": "disabled"}},
            False,
        ),
        (
            "schema",
            "https://api.deepseek.com",
            {},
            {"thinking": {"enabled": False}},
            False,
        ),
        ("schema", "https://api.deepseek.com", {}, {"thinking": False}, False),
        ("schema", "https://api.deepseek.com", {}, {"reasoning_effort": "none"}, False),
        (
            "schema",
            "https://api.deepseek.com",
            {"thinking_enable": True, "reasoning_effort": "low"},
            None,
            True,
        ),
    ],
)
def test_formatter_eligibility(
    phase: str, base_url: str, parameters: dict, extra: dict | None, enabled: bool
) -> None:
    model = ObservedOpenAIChatModel(
        attempt_recorder=ModelAttemptRecorder(_session(), phase, None),
        credential=OpenAICredential(api_key="offline", base_url=base_url),
        model="offline",
        parameters=OpenAIChatModel.Parameters(**parameters),
        extra_body=extra,
    )
    assert model.schema_reasoning_history_enabled is enabled
    assert isinstance(model.formatter, SchemaReasoningOpenAIFormatter) is enabled
    assert model.extra_body == extra


def test_arbitrary_formatter_is_not_replaced_or_opted_in() -> None:
    class CustomFormatter(OpenAIChatFormatter):
        pass

    custom = CustomFormatter()
    model = ObservedOpenAIChatModel(
        attempt_recorder=ModelAttemptRecorder(_session(), "schema", None),
        credential=OpenAICredential(
            api_key="offline", base_url="https://api.deepseek.com"
        ),
        model="offline",
        formatter=custom,
    )
    assert model.formatter is custom
    assert not model.schema_reasoning_history_enabled


def test_explicit_settings_disable_and_phase_isolation() -> None:
    schema, _ = _agent(thinking_enable=False)
    ttp, _ = _agent(phase="ttp")
    selected, _ = _agent()
    assert not schema.model.schema_reasoning_history_enabled
    assert not schema._compress_context_middlewares
    assert not ttp.model.schema_reasoning_history_enabled
    assert not ttp._compress_context_middlewares
    assert len(selected._compress_context_middlewares) == 1


async def test_counting_matches_sent_thinking_without_mutating_history() -> None:
    schema, _ = _agent()
    ttp, _ = _agent(phase="ttp")
    messages = schema.state.context
    before = [message.model_dump() for message in messages]
    plain = [
        message.model_copy(
            update={
                "content": [
                    block
                    for block in message.get_content_blocks()
                    if not isinstance(block, ThinkingBlock)
                ]
            }
        )
        for message in messages
    ]
    native_full = await OpenAIChatModel.count_tokens(schema.model, messages, [])
    native_plain = await OpenAIChatModel.count_tokens(schema.model, plain, [])
    assert await schema.model.count_tokens(messages, []) == native_full
    assert await ttp.model.count_tokens(messages, []) == native_plain
    assert native_full > native_plain
    assert [message.model_dump() for message in messages] == before


@pytest.mark.parametrize(
    "change",
    [
        {"completed": False},
        {"finished_reason": "stop"},
        {"reasoning_present": False},
        {"text_present": True},
        {"tool_calls_present": True},
        {"round_index": 1},
    ],
)
def test_completed_length_requires_matching_raw_facts(change: dict) -> None:
    agent, session = _agent()
    session.agent_rounds = 2
    values = dict(
        round_index=2,
        attempt_index=3,
        completed=True,
        finished_reason="length",
        reasoning_present=True,
    )
    agent.model._provider_reply_facts = _ProviderReplyFacts(**values)
    assert agent.model.is_completed_reasoning_only_length(2)
    assert not agent.model.is_completed_reasoning_only_length(1)
    values.update(change)
    agent.model._provider_reply_facts = _ProviderReplyFacts(**values)
    assert not agent.model.is_completed_reasoning_only_length(2)


async def test_guard_counts_actual_prepared_messages_tools_and_never_compresses(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events = []
    agent, _ = _agent(events=events)
    original = await agent._prepare_model_input()
    seen = []

    async def count(**kwargs: Any) -> int:
        seen.append(kwargs)
        return 1234

    async def native(**_: Any) -> None:
        pytest.fail("native compression must never run with retained reasoning")

    monkeypatch.setattr(agent.model, "count_tokens", count)
    monkeypatch.setattr(agent, "_compress_context_impl", native)
    await agent.compress_context()

    def semantic(message):
        return {
            "role": message.role,
            "name": message.name,
            "content": [
                {
                    "type": type(block).__name__,
                    "text": getattr(block, "text", None),
                    "thinking": getattr(block, "thinking", None),
                }
                for block in message.get_content_blocks()
            ],
        }

    assert [semantic(item) for item in seen[0]["messages"]] == [
        semantic(item) for item in original["messages"]
    ]
    assert seen[0]["tools"] == original["tools"]
    assert len(events) == 1
    assert events[0].name == "cli_parser.schema.reasoning_history"
    assert events[0].value == {
        "status": "context_checked",
        "round_index": 0,
        "reasoning_blocks": 1,
        "reasoning_chars": len("private reasoning"),
        "estimated_tokens": 1234,
        "context_limit_tokens": 64000,
        "runtime_policy": "schema-reasoning-history-v1",
    }
    assert "private reasoning" not in json.dumps(events[0].value)


@pytest.mark.parametrize(
    ("context", "reserve", "trigger", "limit"),
    [
        (128000, 8192, 0.8, 64000),
        (128000, 100000, 0.8, 28000),
        (128000, 8192, 0.3, 38400),
    ],
)
@pytest.mark.parametrize("offset", [-1, 0, 1])
async def test_guard_enforces_strict_capacity_bound(
    monkeypatch: pytest.MonkeyPatch,
    context: int,
    reserve: int,
    trigger: float,
    limit: int,
    offset: int,
) -> None:
    agent, _ = _agent()
    agent.model.context_size = context
    agent.model.parameters.max_tokens = reserve

    async def count(**_: Any) -> int:
        return limit + offset

    monkeypatch.setattr(agent.model, "count_tokens", count)
    if offset < 0:
        await agent.compress_context(
            context_config=ContextConfig(trigger_ratio=trigger)
        )
    else:
        with pytest.raises(SchemaReasoningContextLimit):
            await agent.compress_context(
                context_config=ContextConfig(trigger_ratio=trigger)
            )


@pytest.mark.parametrize("stage", ["prepare", "count", "summary"])
async def test_failure_cannot_fall_through_to_compression(
    monkeypatch: pytest.MonkeyPatch, stage: str
) -> None:
    events = []
    agent, _ = _agent(events=events)

    async def fail(**_: Any) -> Any:
        raise ValueError("private raw body")

    async def native(**_: Any) -> None:
        pytest.fail("no compression allowed")

    monkeypatch.setattr(agent, "_compress_context_impl", native)
    if stage == "summary":
        agent.state.summary = "private summary"
    elif stage == "prepare":
        monkeypatch.setattr(agent, "_prepare_model_input", fail)
    else:
        monkeypatch.setattr(agent.model, "count_tokens", fail)
    with pytest.raises(SchemaReasoningContextLimit) as error:
        await agent.compress_context()
    assert "private" not in str(error.value)
    assert events[-1].value["status"] == "context_limit"
    assert "private" not in json.dumps(events[-1].value)


@pytest.mark.parametrize("stage", ["prepare", "count"])
async def test_cancellation_propagates(
    monkeypatch: pytest.MonkeyPatch, stage: str
) -> None:
    agent, _ = _agent()

    async def cancel(**_: Any) -> Any:
        raise asyncio.CancelledError

    monkeypatch.setattr(
        agent if stage == "prepare" else agent.model,
        "_prepare_model_input" if stage == "prepare" else "count_tokens",
        cancel,
    )
    with pytest.raises(asyncio.CancelledError):
        await agent.compress_context()


@pytest.mark.parametrize("expired", [False, True])
async def test_deadline_prevents_provider_or_compression(
    monkeypatch: pytest.MonkeyPatch, expired: bool
) -> None:
    agent, session = _agent()
    session.deadline_monotonic = time.monotonic() + (-1 if expired else 0.01)

    async def wait(**_: Any) -> Any:
        await asyncio.sleep(1)
        pytest.fail("deadline should interrupt preparation")

    monkeypatch.setattr(agent, "_prepare_model_input", wait)
    with pytest.raises(TimeoutError):
        await agent.compress_context()


@pytest.mark.parametrize("phase", ["schema", "ttp"])
async def test_native_behavior_remains_without_sent_thinking(
    monkeypatch: pytest.MonkeyPatch, phase: str
) -> None:
    agent, session = _agent(phase=phase)
    if phase == "schema":
        agent.state.context = [UserMsg(name="user", content="synthetic")]
    delegated = []

    async def native() -> None:
        delegated.append(True)

    await SchemaReasoningHistoryGuard(session).on_compress_context(agent, {}, native)
    assert delegated == [True]
