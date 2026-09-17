"""A length-finished Schema tool call cannot become a repaired frozen proposal."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator

import httpx
import openai
import pytest
from agentscope.credential import OpenAICredential
from agentscope.message import ToolCallBlock, UserMsg
from lmnr import Laminar

from cli_parser_agent import (
    GenerationPolicy,
    GenerationRequest,
    TtpGenerator,
    TtpGeneratorSettings,
)
from cli_parser_agent.ttp_generation.agent.model_attempts import (
    ModelAttemptRecorder,
    ObservedOpenAIChatModel,
)
from cli_parser_agent.ttp_generation.agent.session import GenerationSession

SCHEMA = {
    "type": "object",
    "properties": {"value": {"type": "string"}},
    "required": ["value"],
    "additionalProperties": False,
}


def _reply(arguments: str, finish: str, *, stream: bool) -> httpx.Response:
    call = {
        "id": "call-guard",
        "type": "function",
        "function": {"name": "submit_result_schema", "arguments": arguments},
    }
    message = {"role": "assistant", "content": None, "tool_calls": [call]}
    base = {"id": "offline-guard", "created": 0, "model": "offline"}
    usage = {"prompt_tokens": 10, "completion_tokens": 30, "total_tokens": 40}
    if not stream:
        return httpx.Response(
            200,
            json={
                **base,
                "object": "chat.completion",
                "choices": [{"index": 0, "message": message, "finish_reason": finish}],
                "usage": usage,
            },
        )
    prefix, suffix = arguments[: len(arguments) // 2], arguments[len(arguments) // 2 :]
    chunks = [
        {
            **base,
            "object": "chat.completion.chunk",
            "choices": [
                {
                    "index": 0,
                    "delta": {
                        "tool_calls": [
                            {
                                **call,
                                "index": 0,
                                "function": {**call["function"], "arguments": prefix},
                            }
                        ]
                    },
                    "finish_reason": None,
                }
            ],
        },
        {
            **base,
            "object": "chat.completion.chunk",
            "choices": [
                {
                    "index": 0,
                    "delta": {
                        "tool_calls": [
                            {
                                "index": 0,
                                "function": {"arguments": suffix},
                            }
                        ]
                    },
                    "finish_reason": None,
                }
            ],
        },
        {
            **base,
            "object": "chat.completion.chunk",
            "choices": [
                {
                    "index": 0,
                    "delta": {},
                    "finish_reason": finish,
                }
            ],
        },
        {**base, "object": "chat.completion.chunk", "choices": [], "usage": usage},
    ]
    return httpx.Response(
        200,
        headers={"content-type": "text/event-stream"},
        content="".join(f"data: {json.dumps(chunk)}\n\n" for chunk in chunks)
        + "data: [DONE]\n\n",
    )


@pytest.fixture(autouse=True)
def _no_trace(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(Laminar, "is_initialized", lambda: False)


@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("partial", [False, True])
async def test_length_response_never_freezes_legal_or_repairable_arguments(
    monkeypatch: pytest.MonkeyPatch,
    stream: bool,
    partial: bool,
) -> None:
    arguments = json.dumps({"result_schema": SCHEMA})
    if partial:
        arguments = arguments[:-2]
    calls, events = [], []

    def respond(request: httpx.Request) -> httpx.Response:
        calls.append(json.loads(request.content))
        return _reply(arguments, "length", stream=stream)

    real_client = openai.AsyncClient
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        monkeypatch.setattr(
            openai,
            "AsyncClient",
            lambda **kwargs: real_client(**kwargs, http_client=client),
        )
        result = await TtpGenerator(
            settings=TtpGeneratorSettings(
                api_key="offline", model_name="offline", stream=stream
            ),
            policy=GenerationPolicy(max_schema_no_tool_retries=0),
        ).propose_schema(
            GenerationRequest(command_outputs=["Value: one"]), observer=events.append
        )
    assert len(calls) == 1
    assert result.status == "failed"
    assert result.proposal is None
    assert not any(
        getattr(event, "tool_call_name", None) == "submit_result_schema"
        for event in events
    )
    guard = [
        event
        for event in events
        if getattr(event, "name", None)
        == "cli_parser.schema.truncated_submission_discarded"
    ]
    assert len(guard) == 1
    assert guard[0].value["discarded_tool_calls"] == 1
    assert "value" not in json.dumps(guard[0].value)


@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("finish", ["stop", "tool_calls"])
async def test_complete_provider_response_still_freezes_unmodified_schema(
    monkeypatch: pytest.MonkeyPatch,
    stream: bool,
    finish: str,
) -> None:
    real_client = openai.AsyncClient
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda _: _reply(
                json.dumps({"result_schema": SCHEMA}), finish, stream=stream
            )
        )
    ) as client:
        monkeypatch.setattr(
            openai,
            "AsyncClient",
            lambda **kwargs: real_client(**kwargs, http_client=client),
        )
        result = await TtpGenerator(
            settings=TtpGeneratorSettings(
                api_key="offline", model_name="offline", stream=stream
            ),
        ).propose_schema(GenerationRequest(command_outputs=["Value: one"]))
    assert result.status == "success"
    assert result.proposal.result_schema == SCHEMA


@pytest.mark.parametrize("stream", [False, True])
async def test_guard_keeps_ttp_model_tool_blocks_unchanged(stream: bool) -> None:
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: _reply("{}", "length", stream=stream))
    ) as client:
        model = ObservedOpenAIChatModel(
            attempt_recorder=ModelAttemptRecorder(
                GenerationSession(
                    command_outputs=("Value: one",),
                    schema_validator=lambda _: None,
                    template_validator=lambda _: None,
                ),
                "ttp",
            ),
            credential=OpenAICredential(api_key="offline"),
            model="offline",
            stream=stream,
            max_retries=0,
            client_kwargs={"http_client": client},
        )
        result = await model([UserMsg(name="user", content="Value: one")])
        if stream:
            chunks = [chunk async for chunk in result]
            result = chunks[-1]
        assert any(isinstance(block, ToolCallBlock) for block in result.content)


@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("finish", ["length", "tool_calls"])
async def test_guard_preserves_provider_usage(stream: bool, finish: str) -> None:
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda _: _reply(
                json.dumps({"result_schema": SCHEMA}), finish, stream=stream
            )
        )
    ) as client:
        model = ObservedOpenAIChatModel(
            attempt_recorder=ModelAttemptRecorder(
                GenerationSession(
                    command_outputs=("Value: one",),
                    schema_validator=lambda _: None,
                    template_validator=lambda _: None,
                ),
                "schema",
            ),
            credential=OpenAICredential(api_key="offline"),
            model="offline",
            stream=stream,
            max_retries=0,
            client_kwargs={"http_client": client},
        )
        result = await model([UserMsg(name="user", content="Value: one")])
        if stream:
            chunks = [chunk async for chunk in result]
            result = chunks[-1]
        assert result.usage.input_tokens == 10
        assert result.usage.output_tokens == 30
        assert sum(isinstance(block, ToolCallBlock) for block in result.content) == (
            0 if finish == "length" else 1
        )


async def test_cancel_after_buffered_tool_fragment_closes_without_exposing_call() -> (
    None
):
    waiting_for_next_chunk = asyncio.Event()
    close_count = 0

    class PausedToolStream(httpx.AsyncByteStream):
        async def __aiter__(self) -> AsyncIterator[bytes]:
            chunk = {
                "id": "offline-guard-cancel",
                "created": 0,
                "model": "offline",
                "object": "chat.completion.chunk",
                "choices": [
                    {
                        "index": 0,
                        "delta": {
                            "tool_calls": [
                                {
                                    "index": 0,
                                    "id": "call-guard-cancel",
                                    "type": "function",
                                    "function": {
                                        "name": "submit_result_schema",
                                        "arguments": '{"result_schema":{',
                                    },
                                }
                            ]
                        },
                        "finish_reason": None,
                    }
                ],
            }
            yield f"data: {json.dumps(chunk)}\n\n".encode()
            # Reaching this point proves the adapter consumed the tool fragment.
            waiting_for_next_chunk.set()
            await asyncio.Event().wait()

        async def aclose(self) -> None:
            nonlocal close_count
            close_count += 1

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda _: httpx.Response(
                200,
                headers={"content-type": "text/event-stream"},
                stream=PausedToolStream(),
            )
        )
    ) as client:
        model = ObservedOpenAIChatModel(
            attempt_recorder=ModelAttemptRecorder(
                GenerationSession(
                    command_outputs=("Value: one",),
                    schema_validator=lambda _: None,
                    template_validator=lambda _: None,
                ),
                "schema",
            ),
            credential=OpenAICredential(api_key="offline"),
            model="offline",
            stream=True,
            max_retries=0,
            client_kwargs={"http_client": client},
        )
        stream = await model([UserMsg(name="user", content="Value: one")])
        observed_chunks = []

        async def consume() -> None:
            async for chunk in stream:
                observed_chunks.append(chunk)

        consumer = asyncio.create_task(consume())
        try:
            async with asyncio.timeout(3):
                await waiting_for_next_chunk.wait()
                consumer.cancel()
                # AgentScope consumes cancellation into its terminal response.
                await consumer
        finally:
            if not consumer.done():
                consumer.cancel()
                await asyncio.gather(consumer, return_exceptions=True)
            await stream.aclose()

        assert close_count == 1
        assert not any(
            isinstance(block, ToolCallBlock)
            for chunk in observed_chunks
            for block in chunk.content
        )
        assert model._provider_reply_facts is not None
        assert not model._provider_reply_facts.completed
