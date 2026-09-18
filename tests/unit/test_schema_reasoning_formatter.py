"""Synthetic history tests; no provider calls or evaluation assets."""

from copy import deepcopy

import pytest
from agentscope.formatter import OpenAIChatFormatter
from agentscope.message import (
    DataBlock,
    HintBlock,
    Msg,
    TextBlock,
    ThinkingBlock,
    ToolCallBlock,
    ToolResultBlock,
    URLSource,
)

from cli_parser_agent.ttp_generation.agent.schema_reasoning_formatter import (
    SchemaReasoningOpenAIFormatter,
)


def _assistant(*blocks):
    return Msg(name="schema", role="assistant", content=list(blocks))


def _thinking(text):
    return ThinkingBlock(thinking=text)


def _call(identifier, arguments=' { "result_schema" : {} } '):
    return ToolCallBlock(id=identifier, name="submit_result_schema", input=arguments)


def _result(identifier, output="rejected"):
    return ToolResultBlock(id=identifier, name="submit_result_schema", output=output)


@pytest.mark.asyncio
async def test_first_request_and_ordinary_history_match_upstream_exactly():
    messages = [
        Msg(name="system", role="system", content=[TextBlock(text="policy")]),
        Msg(name="user", role="user", content=[TextBlock(text="synthetic input")]),
    ]
    formatter = SchemaReasoningOpenAIFormatter()
    upstream = OpenAIChatFormatter()
    assert await formatter.format(messages) == await upstream.format(messages)
    messages.append(
        _assistant(
            TextBlock(text="ordinary text"),
            _call("one"),
            _result("one"),
            HintBlock(hint="retry"),
            _call("two"),
        )
    )
    assert await formatter.format(messages) == await upstream.format(messages)


@pytest.mark.asyncio
async def test_coalesced_business_rejection_cycles_keep_reasoning_and_pairs_local():
    arguments = ' { "result_schema" : {"type":"object"} }\n'
    message = _assistant(
        _thinking("first decision"),
        _call("one", arguments),
        _result("one", "invalid field name"),
        _thinking("correct name"),
        _call("two", arguments),
        _result("two", "unsupported constraint"),
        _thinking("correct constraint"),
        TextBlock(text="submitted"),
        _call("three", arguments),
    )
    before = deepcopy(message.model_dump())
    rows = await SchemaReasoningOpenAIFormatter().format([message])
    assert [row["role"] for row in rows] == [
        "assistant",
        "tool",
        "assistant",
        "tool",
        "assistant",
    ]
    assert [row.get("reasoning_content") for row in rows] == [
        "first decision",
        None,
        "correct name",
        None,
        "correct constraint",
    ]
    assert [row["tool_calls"][0]["id"] for row in rows if "tool_calls" in row] == [
        "one",
        "two",
        "three",
    ]
    assert [row["tool_call_id"] for row in rows if row["role"] == "tool"] == [
        "one",
        "two",
    ]
    assert all(
        row["tool_calls"][0]["function"]["arguments"] == arguments
        for row in rows
        if "tool_calls" in row
    )
    without_reasoning = [
        {key: value for key, value in row.items() if key != "reasoning_content"}
        for row in rows
    ]
    assert without_reasoning == await OpenAIChatFormatter().format([message])
    assert message.model_dump() == before


@pytest.mark.asyncio
async def test_reasoning_only_is_preserved_with_empty_content():
    message = _assistant(_thinking("unfinished reasoning"))
    assert await SchemaReasoningOpenAIFormatter().format([message]) == [
        {
            "name": "schema",
            "role": "assistant",
            "content": "",
            "reasoning_content": "unfinished reasoning",
        }
    ]


@pytest.mark.asyncio
async def test_multiple_thinking_blocks_concatenate_without_inserted_characters():
    message = _assistant(
        _thinking("one\n"),
        TextBlock(text="visible"),
        _thinking("two"),
        _thinking(""),
        _thinking("三"),
    )
    rows = await SchemaReasoningOpenAIFormatter().format([message])
    assert rows == [
        {
            "name": "schema",
            "role": "assistant",
            "content": [{"type": "text", "text": "visible"}],
            "reasoning_content": "one\ntwo三",
        }
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("hint", ["retry", [TextBlock(text="retry")]])
async def test_hints_flush_reasoning_and_keep_upstream_user_row(hint):
    message = _assistant(
        _thinking("before hint"),
        HintBlock(hint=hint),
        _thinking("after hint"),
        _call("call"),
        _result("call"),
        _thinking("after result"),
    )
    rows = await SchemaReasoningOpenAIFormatter().format([message])
    assert [row["role"] for row in rows] == [
        "assistant",
        "user",
        "assistant",
        "tool",
        "assistant",
    ]
    assert rows[1] == {"role": "user", "content": [{"type": "text", "text": "retry"}]}
    assert [row.get("reasoning_content") for row in rows] == [
        "before hint",
        None,
        "after hint",
        None,
        "after result",
    ]


@pytest.mark.asyncio
async def test_multimodal_tool_result_promotion_does_not_receive_reasoning(monkeypatch):
    monkeypatch.setattr("shortuuid.uuid", lambda: "synthetic-image-id")
    image = DataBlock(
        source=URLSource(
            url="https://example.test/synthetic.png", media_type="image/png"
        )
    )
    message = _assistant(
        _thinking("first"),
        _call("call"),
        _result("call", [TextBlock(text="feedback"), image]),
        _thinking("second"),
        TextBlock(text="final"),
    )
    upstream = await OpenAIChatFormatter().format([message])
    rows = await SchemaReasoningOpenAIFormatter().format([message])
    assert [row["role"] for row in rows] == ["assistant", "tool", "user", "assistant"]
    assert rows[1:3] == upstream[1:3]
    assert [row.get("reasoning_content") for row in rows] == [
        "first",
        None,
        None,
        "second",
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("role", ["user", "system"])
async def test_nonassistant_thinking_is_not_promoted(role):
    # Normal Msg validation prevents these blocks; restored state can still be
    # constructed without validation. The adapter must not promote their role.
    message = Msg.model_construct(
        name="source",
        role=role,
        content=[_thinking("private nonassistant block"), TextBlock(text="visible")],
    )
    assert await SchemaReasoningOpenAIFormatter().format([message]) == (
        await OpenAIChatFormatter().format([message])
    )


@pytest.mark.asyncio
async def test_empty_message_stays_absent_but_explicit_empty_reasoning_is_preserved():
    formatter = SchemaReasoningOpenAIFormatter()
    assert await formatter.format([_assistant()]) == []
    assert await formatter.format([_assistant(_thinking(""))]) == [
        {"role": "assistant", "name": "schema", "content": "", "reasoning_content": ""}
    ]
    assert await formatter.format([_assistant(_thinking(""), TextBlock(text=""))]) == [
        {
            "role": "assistant",
            "name": "schema",
            "content": [{"type": "text", "text": ""}],
            "reasoning_content": "",
        }
    ]


@pytest.mark.asyncio
async def test_multiple_calls_and_results_remain_single_exact_pairs():
    message = _assistant(
        _thinking("review"),
        _call("one"),
        _call("two"),
        _result("one"),
        _result("two"),
        _thinking("revised"),
    )
    rows = await SchemaReasoningOpenAIFormatter().format([message])
    assert [row["role"] for row in rows] == ["assistant", "tool", "tool", "assistant"]
    assert [call["id"] for call in rows[0]["tool_calls"]] == ["one", "two"]
    assert [row["tool_call_id"] for row in rows[1:3]] == ["one", "two"]
    assert [row.get("reasoning_content") for row in rows] == [
        "review",
        None,
        None,
        "revised",
    ]


@pytest.mark.asyncio
async def test_separate_messages_never_share_reasoning():
    messages = [
        _assistant(_thinking("first"), _call("one")),
        _assistant(_result("one")),
        Msg(name="user", role="user", content=[TextBlock(text="retry")]),
        _assistant(_thinking("second")),
    ]
    original = [message.model_dump() for message in messages]
    rows = await SchemaReasoningOpenAIFormatter().format(messages)
    assert [row.get("reasoning_content") for row in rows] == [
        "first",
        None,
        None,
        "second",
    ]
    assert [message.model_dump() for message in messages] == original
