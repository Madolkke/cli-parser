"""Provider finish facts never alter configured generation parameters."""

from __future__ import annotations

import asyncio
import json
import time
from typing import Any

import httpx
import pytest
from agentscope.credential import OpenAICredential
from agentscope.message import UserMsg
from agentscope.model import OpenAIChatModel
from lmnr import Laminar

from cli_parser_agent import (
    GenerationPolicy,
    TtpGeneratorSettings,
)
from cli_parser_agent.ttp_generation.agent.builder import build_agent
from cli_parser_agent.ttp_generation.agent.model_attempts import (
    ModelAttemptRecorder,
    ObservedOpenAIChatModel,
)
from cli_parser_agent.ttp_generation.agent.runner import run_generation_phase
from cli_parser_agent.ttp_generation.agent.session import GenerationSession
from cli_parser_agent.ttp_generation.progress import ProgressEmitter


def _session(**kwargs: Any) -> GenerationSession:
    return GenerationSession(
        command_outputs=("synthetic input",),
        schema_validator=lambda _: None,
        template_validator=lambda _: None,
        **kwargs,
    )


def _body(
    *,
    reason: str = "length",
    text: str | None = None,
    thinking: str | None = "private",
    tool: bool = False,
) -> dict[str, Any]:
    message: dict[str, Any] = {
        "role": "assistant",
        "content": text,
        "reasoning_content": thinking,
    }
    if tool:
        message["tool_calls"] = [
            {
                "index": 0,
                "id": "call-1",
                "type": "function",
                "function": {"name": "submit_result_schema", "arguments": "{}"},
            }
        ]
    return {
        "id": "offline-recovery",
        "object": "chat.completion",
        "created": 0,
        "model": "offline",
        "choices": [
            {
                "index": 0,
                "message": message,
                "finish_reason": reason,
            }
        ],
        "usage": {"prompt_tokens": 10, "completion_tokens": 8192, "total_tokens": 8202},
    }


def _response(body: dict[str, Any], *, stream: bool = False) -> httpx.Response:
    if not stream:
        return httpx.Response(200, json=body)
    choice = body["choices"][0]
    base = {
        "id": body["id"],
        "object": "chat.completion.chunk",
        "created": 0,
        "model": "offline",
    }
    chunks = [
        {
            **base,
            "choices": [
                {"index": 0, "delta": choice["message"], "finish_reason": None}
            ],
        },
        {
            **base,
            "choices": [
                {"index": 0, "delta": {}, "finish_reason": choice["finish_reason"]}
            ],
        },
        {**base, "choices": [], "usage": body["usage"]},
    ]
    content = "".join(f"data: {json.dumps(chunk)}\n\n" for chunk in chunks)
    return httpx.Response(
        200,
        headers={"content-type": "text/event-stream"},
        content=content + "data: [DONE]\n\n",
    )


def _model(
    client: httpx.AsyncClient, *, events: list[Any] | None = None, **kwargs: Any
) -> ObservedOpenAIChatModel:
    return ObservedOpenAIChatModel(
        attempt_recorder=ModelAttemptRecorder(
            _session(),
            kwargs.pop("phase", "schema"),
            ProgressEmitter("provider-facts-test", events.append)
            if events is not None
            else None,
        ),
        credential=OpenAICredential(
            api_key="offline",
            base_url=kwargs.pop("base_url", "https://api.deepseek.com"),
        ),
        model="offline",
        parameters=kwargs.pop(
            "parameters", OpenAIChatModel.Parameters(max_tokens=8192)
        ),
        stream=kwargs.pop("stream", False),
        max_retries=kwargs.pop("max_retries", 0),
        client_kwargs={"http_client": client, "max_retries": 0},
        **kwargs,
    )


async def _round(model: ObservedOpenAIChatModel) -> Any:
    model._attempt_recorder.session.record_agent_round(model._attempt_recorder.phase)
    result = await model([UserMsg(name="user", content="synthetic input")])
    if model.stream:
        async for _ in result:
            pass
    return model._provider_reply_facts


@pytest.fixture(autouse=True)
def _no_tracing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(Laminar, "is_initialized", lambda: False)


@pytest.mark.parametrize("stream", [False, True])
async def test_completed_length_facts_do_not_mutate_later_requests(
    stream: bool,
) -> None:
    requests, events = [], []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(json.loads(request.content))
        return _response(_body(), stream=stream)

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        model = _model(client, events=events, stream=stream)
        for index in range(1, 5):
            facts = await _round(model)
            assert facts.completed
            assert facts.round_index == facts.attempt_index == index
            assert facts.finished_reason == "length"
            assert facts.reasoning_present
            assert not facts.text_present
            assert not facts.tool_calls_present
        assert requests == [requests[0]] * 4
        assert all("thinking" not in request for request in requests)
        assert all("reasoning_effort" not in request for request in requests)
        assert model.extra_body is None
        assert not model.parameters.thinking_enable
        assert model.parameters.reasoning_effort is None
        assert model.parameters.max_tokens == 8192
    assert not any(
        event.name == "cli_parser.schema.reasoning_recovery" for event in events
    )
    assert "private" not in json.dumps([event.value for event in events])


@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize(
    ("body", "finish", "reasoning", "text", "tools"),
    [
        (
            _body(reason="stop", thinking=None, text="private text"),
            "stop",
            False,
            True,
            False,
        ),
        (_body(reason="tool_calls", tool=True), "tool_calls", True, False, True),
        (_body(tool=True), "length", True, False, True),
        (_body(reason="vendor-specific"), "unknown", True, False, False),
    ],
)
async def test_provider_facts_preserve_raw_finish_category(
    stream: bool,
    body: dict[str, Any],
    finish: str,
    reasoning: bool,
    text: bool,
    tools: bool,
) -> None:
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: _response(body, stream=stream))
    ) as client:
        facts = await _round(_model(client, stream=stream))
    assert facts.completed
    assert facts.finished_reason == finish
    assert facts.reasoning_present is reasoning
    assert facts.text_present is text
    assert facts.tool_calls_present is tools
    assert "private" not in repr(facts)


async def test_http_retry_facts_identify_last_completed_attempt() -> None:
    requests = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(json.loads(request.content))
        if len(requests) in {3, 5}:
            return httpx.Response(500, json={"error": {"message": "private"}})
        return _response(_body())

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        model = _model(client, max_retries=1, retry_delay=0)
        facts = [await _round(model) for _ in range(4)]
        assert [(f.round_index, f.attempt_index) for f in facts] == [
            (1, 1),
            (2, 2),
            (3, 4),
            (4, 6),
        ]
        assert all(f.completed for f in facts)
        assert requests == [requests[0]] * 6
        assert model._attempt_recorder.session.model_retries_observed == 2


@pytest.mark.parametrize(
    "options",
    [
        {"phase": "ttp"},
        {"base_url": "https://api.deepseek.com.invalid"},
        {"extra_body": {}},
        {"extra_body": {"thinking": {"type": "enabled"}}},
        {"extra_body": {"thinking": {"type": "disabled"}}},
        {"parameters": OpenAIChatModel.Parameters(thinking_enable=True)},
        {
            "parameters": OpenAIChatModel.Parameters(
                thinking_enable=True, reasoning_effort="none"
            )
        },
        {
            "parameters": OpenAIChatModel.Parameters(
                thinking_enable=True, reasoning_effort="high"
            )
        },
        {
            "parameters": OpenAIChatModel.Parameters(
                thinking_enable=True, reasoning_effort="low"
            )
        },
    ],
)
async def test_phase_provider_and_explicit_settings_remain_unchanged(
    options: dict[str, Any],
) -> None:
    requests = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(json.loads(request.content))
        return _response(_body())

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        model = _model(client, **options)
        for _ in range(4):
            await _round(model)
        assert requests == [requests[0]] * 4


@pytest.mark.parametrize("failure", ["timeout", "cancel"])
async def test_failed_attempt_cannot_reuse_observed_length(failure: str) -> None:
    calls = 0

    def respond(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 3:
            if failure == "cancel":
                raise asyncio.CancelledError()
            raise httpx.ReadTimeout("private failure", request=request)
        return _response(_body())

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        model = _model(client)
        await _round(model)
        previous = await _round(model)
        if failure == "timeout":
            with pytest.raises(Exception, match="timed out"):
                await _round(model)
        else:
            await _round(model)
        assert model._provider_reply_facts is not previous
        assert not model._provider_reply_facts.completed
        assert (await _round(model)).completed


@pytest.mark.parametrize("limit", ["rounds", "no_tool", "deadline", "default"])
async def test_runner_retains_existing_no_tool_and_budget_limits(limit: str) -> None:
    session = _session(
        max_agent_rounds=3 if limit == "rounds" else 26,
        max_schema_no_tool_retries=2 if limit == "no_tool" else 3,
        min_round_seconds=1 if limit == "deadline" else 0,
    )
    requests = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(json.loads(request.content))
        if len(requests) == 3 and limit == "deadline":
            session.deadline_monotonic = time.monotonic() - 1
        return _response(_body())

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        agent = build_agent(
            settings=TtpGeneratorSettings(
                api_key="offline",
                model_name="offline",
                base_url="https://api.deepseek.com",
            ),
            policy=GenerationPolicy(max_agent_rounds=26),
            session=session,
            phase="schema",
        )
        agent.model.client_kwargs["http_client"] = client
        await run_generation_phase(
            agent, UserMsg(name="user", content="synthetic input"), session, "schema"
        )
    assert len(requests) == (4 if limit == "default" else 3)
    assert all("reasoning_effort" not in request for request in requests)
    assert all("thinking" not in request for request in requests)
    assert session.frozen_schema is None


async def test_models_do_not_share_provider_facts() -> None:
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: _response(_body()))
    ) as client:
        first, second = _model(client), _model(client)
        for _ in range(3):
            await _round(first)
        await _round(second)
        assert first._provider_reply_facts is not second._provider_reply_facts
        assert first._provider_reply_facts.round_index == 3
        assert second._provider_reply_facts.round_index == 1
