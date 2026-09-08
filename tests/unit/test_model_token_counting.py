"""The default estimator counts visible payloads without changing history."""

from __future__ import annotations

import json
from typing import Any

import pytest
from agentscope.agent import Agent
from agentscope.formatter import OpenAIChatFormatter
from agentscope.message import (
    Base64Source,
    DataBlock,
    HintBlock,
    Msg,
    SystemMsg,
    TextBlock,
    ThinkingBlock,
    ToolCallBlock,
    ToolResultBlock,
    UserMsg,
)
from agentscope.model import OpenAIChatModel

from cli_parser_agent.config import GenerationPolicy, TtpGeneratorSettings
from cli_parser_agent.ttp_generation.agent import (
    SCHEMA_SYSTEM_PROMPT,
    TTP_SYSTEM_PROMPT,
    GenerationPhase,
    GenerationSession,
    build_agent,
    build_schema_task_message,
    build_ttp_task_message,
    estimate_initial_model_tokens,
)
from cli_parser_agent.ttp_generation.agent.model_attempts import ObservedOpenAIChatModel


def _schema() -> dict[str, Any]:
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "type": "object",
        "properties": {"value": {"type": "string"}},
        "required": ["value"],
        "additionalProperties": False,
    }


def _agent(phase: GenerationPhase = "ttp") -> Agent:
    def unused(_: Any) -> Any:
        raise AssertionError("token counting must not execute validators")

    session = GenerationSession(
        command_outputs=("value: one",),
        schema_validator=unused,
        template_validator=unused,
    )
    session.frozen_schema = _schema()
    return build_agent(
        settings=TtpGeneratorSettings(api_key="unused-key", model_name="test-model"),
        policy=GenerationPolicy(),
        session=session,
        phase=phase,
    )


def _assistant(content: list[Any]) -> Msg:
    return Msg(name="assistant", role="assistant", content=content)


@pytest.mark.parametrize(
    "thinking",
    ["", "verify 字段与边界\n", "large reasoning 推理\n" * 30_000],
    ids=["empty", "multilingual", "long"],
)
@pytest.mark.parametrize("thinking_only", [False, True], ids=["mixed", "only"])
async def test_thinking_length_does_not_increase_default_estimate(
    thinking: str,
    thinking_only: bool,
) -> None:
    model = _agent().model
    visible = [] if thinking_only else [TextBlock(text="Reviewed 字段: value")]
    original = [_assistant(visible)]
    with_thinking = [
        _assistant([ThinkingBlock(thinking=thinking), *visible]),
        _assistant([ThinkingBlock(thinking=thinking)]),
    ]

    visible_count = await OpenAIChatModel.count_tokens(model, original, None)
    assert await model.count_tokens(with_thinking, None) == visible_count
    if thinking:
        assert (
            await OpenAIChatModel.count_tokens(model, with_thinking, None)
            > visible_count
        )
    if thinking_only:
        assert visible_count == 0


@pytest.mark.parametrize(
    "block",
    [
        TextBlock(text="payload 字段"),
        HintBlock(hint="payload 字段"),
        HintBlock(hint=[TextBlock(text="payload 字段")]),
        ToolCallBlock(id="call", name="test_ttp_template", input="payload 字段"),
        ToolResultBlock(id="call", name="test_ttp_template", output="payload 字段"),
        ToolResultBlock(
            id="call",
            name="test_ttp_template",
            output=[TextBlock(text="payload 字段")],
        ),
    ],
    ids=["text", "hint", "hint-list", "tool-call", "tool-result", "result-list"],
)
async def test_all_visible_text_payloads_keep_the_original_utf8_estimate(
    block: Any,
) -> None:
    model = _agent().model
    messages = [_assistant([block])]
    expected = int(len("payload 字段".encode()) / 4 + 0.5)

    assert await model.count_tokens(messages, None) == expected
    assert await OpenAIChatModel.count_tokens(model, messages, None) == expected
    messages[0].content.insert(0, ThinkingBlock(thinking="unsent" * 1_000))
    assert await model.count_tokens(messages, None) == expected


@pytest.mark.parametrize(
    "tools",
    [
        None,
        [],
        [
            {
                "type": "function",
                "function": {
                    "name": "review",
                    "description": "复核字段",
                    "parameters": {"type": "object", "properties": {}},
                },
            }
        ],
    ],
)
async def test_tools_keep_original_counting_with_and_without_thinking(
    tools: list[dict[str, Any]] | None,
) -> None:
    model = _agent().model
    empty_count = await model.count_tokens([], tools)
    expected_text = json.dumps(tools, ensure_ascii=False) if tools else ""
    assert empty_count == int(len(expected_text.encode("utf-8")) / 4 + 0.5)
    assert empty_count == await OpenAIChatModel.count_tokens(model, [], tools)
    assert (
        await model.count_tokens(
            [_assistant([ThinkingBlock(thinking="unsent" * 1_000)])], tools
        )
        == empty_count
    )


async def test_non_text_payload_estimate_is_preserved() -> None:
    model = _agent().model
    messages = [
        UserMsg(
            name="user",
            content=[
                DataBlock(source=Base64Source(data="AA==", media_type="image/png"))
            ],
        )
    ]
    original_count = await OpenAIChatModel.count_tokens(model, messages, None)
    assert original_count > 0
    assert await model.count_tokens(messages, None) == original_count


async def test_counting_preserves_history_and_formatted_request() -> None:
    agent = _agent()
    messages = [
        SystemMsg(name="system", content="system context"),
        UserMsg(name="user", content="value: one"),
        Msg(
            name="assistant",
            role="assistant",
            metadata={"observer": {"phase": "ttp", "labels": ["original"]}},
            usage={"input_tokens": 123, "output_tokens": 456},
            content=[
                ThinkingBlock(thinking="private reasoning" * 1_000, signature="kept"),
                TextBlock(text="reviewing"),
                ToolCallBlock(
                    id="call-test",
                    name="test_ttp_template",
                    input='{"ttp_template": "value: {{ value }}"}',
                    state="finished",
                ),
                ToolResultBlock(
                    id="call-test",
                    name="test_ttp_template",
                    output=[TextBlock(text='[{"value": "one"}]')],
                    state="success",
                    metadata={"diagnostic": {"counts": [1]}},
                ),
                HintBlock(hint="review the full result"),
            ],
        ),
    ]
    formatter = OpenAIChatFormatter()
    snapshots = [message.model_dump(mode="json") for message in messages]
    identities = [
        (message, message.content, tuple(message.content)) for message in messages
    ]
    formatted = await formatter.format(messages)

    first_count = await agent.model.count_tokens(messages, None)
    assert await agent.model.count_tokens(messages, None) == first_count

    assert [message.model_dump(mode="json") for message in messages] == snapshots
    for message, (original, content, blocks) in zip(messages, identities, strict=True):
        assert message is original
        assert message.content is content
        assert all(a is b for a, b in zip(message.content, blocks, strict=True))
    assert await formatter.format(messages) == formatted
    assert "private reasoning" not in json.dumps(formatted)
    assert (
        messages[-1]
        .get_content_blocks("thinking")[0]
        .thinking.startswith("private reasoning")
    )
    assert any(item["role"] == "tool" for item in formatted)


@pytest.mark.parametrize("phase", ["schema", "ttp"])
async def test_both_phase_builders_and_initial_fit_use_the_default_counter(
    phase: GenerationPhase,
) -> None:
    agent = _agent(phase)
    assert type(agent.model) is ObservedOpenAIChatModel
    tools = await agent.toolkit.get_tool_schemas(
        agent.state.tool_context.activated_groups
    )
    message = (
        build_schema_task_message(["value: one"])
        if phase == "schema"
        else build_ttp_task_message(["value: one"], _schema())
    )
    system_prompt = SCHEMA_SYSTEM_PROMPT if phase == "schema" else TTP_SYSTEM_PROMPT
    messages = [SystemMsg(name="system", content=system_prompt), message]
    original_count = await OpenAIChatModel.count_tokens(agent.model, messages, tools)

    assert await estimate_initial_model_tokens(agent, message, phase) == original_count
    assert original_count > await agent.model.count_tokens(messages, None)
    messages.append(_assistant([ThinkingBlock(thinking="unsent" * 50_000)]))
    assert await agent.model.count_tokens(messages, tools) == original_count
    assert (
        await OpenAIChatModel.count_tokens(agent.model, messages, tools)
        > original_count
    )
