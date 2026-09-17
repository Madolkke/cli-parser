"""Only observed reasoning exhaustion may alter an existing Schema retry."""

from __future__ import annotations

import asyncio
import json
import time
from typing import Any

import httpx
import openai
import pytest
from agentscope.credential import OpenAICredential
from agentscope.message import UserMsg
from agentscope.model import OpenAIChatModel
from lmnr import Laminar

from cli_parser_agent import (
    GenerationPolicy,
    GenerationRequest,
    TtpGenerator,
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
            ProgressEmitter("recovery-test", events.append)
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


async def _round(model: ObservedOpenAIChatModel) -> bool:
    model._attempt_recorder.session.record_agent_round(model._attempt_recorder.phase)
    result = await model([UserMsg(name="user", content="synthetic input")])
    if model.stream:
        async for _ in result:
            pass
    return model.prepare_schema_reasoning_recovery()


@pytest.fixture(autouse=True)
def _no_tracing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(Laminar, "is_initialized", lambda: False)


@pytest.mark.parametrize("stream", [False, True])
async def test_three_complete_observed_limits_activate_only_existing_next_request(
    stream: bool,
) -> None:
    requests, events = [], []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(json.loads(request.content))
        return _response(_body(), stream=stream)

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        model = _model(client, events=events, stream=stream)
        assert [await _round(model) for _ in range(3)] == [False, False, True]
        assert len(requests) == 3
        assert not model.prepare_schema_reasoning_recovery()
        await _round(model)
        await _round(model)
        assert all("thinking" not in request for request in requests)
        assert all("reasoning_effort" not in request for request in requests[:3])
        assert requests[:3] == [requests[0]] * 3
        for request in requests[3:]:
            assert request == {**requests[0], "reasoning_effort": "low"}
        assert model.extra_body is None
        assert not model.parameters.thinking_enable
        assert model.parameters.reasoning_effort is None
        assert model.parameters.max_tokens == 8192
    recovery = [
        event
        for event in events
        if event.name == "cli_parser.schema.reasoning_recovery"
    ]
    assert len(recovery) == 1
    assert recovery[0].value["runtime_policy"] == "schema-reasoning-recovery-v2"
    assert recovery[0].value["mode"] == "reasoning_low"
    assert recovery[0].value["reason"] == "consecutive_reasoning_length"
    assert recovery[0].value["consecutive_responses"] == 3
    assert "private" not in json.dumps([event.value for event in events])


@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("tool_rounds", [{0, 1, 2}, {2}])
async def test_discarded_length_tools_still_count_as_reasoning_exhaustion(
    stream: bool, tool_rounds: set[int]
) -> None:
    requests, events = [], []

    def respond(request: httpx.Request) -> httpx.Response:
        index = len(requests)
        requests.append(json.loads(request.content))
        body = _body(tool=index in tool_rounds)
        if index in tool_rounds:
            body["choices"][0]["message"]["tool_calls"][0]["function"]["arguments"] = (
                '{"result_schema":{"type":"object","properties":{'
            )
        return _response(body, stream=stream)

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        model = _model(client, stream=stream, events=events)
        assert [await _round(model) for _ in range(3)] == [False, False, True]
        assert len(requests) == 3
        await _round(model)
        assert requests[3]["reasoning_effort"] == "low"
    discarded = [
        event
        for event in events
        if event.name == "cli_parser.schema.truncated_submission_discarded"
    ]
    assert len(discarded) == len(tool_rounds)
    assert model._attempt_recorder.session.frozen_schema is None


async def test_http_retries_neither_complete_rounds_nor_reset_override() -> None:
    requests, events = [], []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(json.loads(request.content))
        if len(requests) in {3, 5}:
            return httpx.Response(500, json={"error": {"message": "private"}})
        return _response(_body())

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        model = _model(client, events=events, max_retries=1, retry_delay=0)
        assert [await _round(model) for _ in range(3)] == [False, False, True]
        assert len(requests) == 4
        await _round(model)
        assert len(requests) == 6
        assert requests[:4] == [requests[0]] * 4
        assert requests[4:] == [{**requests[0], "reasoning_effort": "low"}] * 2
        assert model._attempt_recorder.session.model_retries_observed == 2
    recovery = [
        event
        for event in events
        if event.name == "cli_parser.schema.reasoning_recovery"
    ]
    assert len(recovery) == 1
    assert recovery[0].value["after_round_index"] == 3
    assert recovery[0].value["after_attempt_index"] == 4


@pytest.mark.parametrize(
    "middle",
    [
        _body(reason="stop"),
        _body(text="visible"),
        _body(thinking=None),
        _body(reason="tool_calls", tool=True),
    ],
)
async def test_nonqualifying_response_breaks_the_sequence(
    middle: dict[str, Any],
) -> None:
    bodies = iter([_body(), _body(), middle, _body(), _body(), _body()])
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: _response(next(bodies)))
    ) as client:
        model = _model(client)
        assert [await _round(model) for _ in range(6)] == [
            False,
            False,
            False,
            False,
            False,
            True,
        ]


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
        {"parameters": OpenAIChatModel.Parameters(reasoning_effort="high")},
        {"parameters": OpenAIChatModel.Parameters(reasoning_effort="low")},
    ],
)
async def test_phase_provider_and_explicit_settings_opt_out(
    options: dict[str, Any],
) -> None:
    requests = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(json.loads(request.content))
        return _response(_body())

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        model = _model(client, **options)
        assert [await _round(model) for _ in range(4)] == [False] * 4
        assert requests == [requests[0]] * 4


async def test_intervening_round_without_recovery_check_resets_streak() -> None:
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: _response(_body()))
    ) as client:
        model = _model(client)
        assert not await _round(model)
        assert not await _round(model)
        model._attempt_recorder.session.record_agent_round("schema")
        assert [await _round(model) for _ in range(3)] == [False, False, True]


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
        assert not await _round(model)
        assert not await _round(model)
        if failure == "timeout":
            with pytest.raises(Exception, match="timed out"):
                await _round(model)
        else:
            assert not await _round(model)
        assert not model.prepare_schema_reasoning_recovery()
        assert not model._provider_reply_facts.completed
        assert not await _round(model)


@pytest.mark.parametrize("limit", ["rounds", "no_tool", "deadline"])
async def test_runner_does_not_activate_or_request_when_retry_budget_exhausted(
    limit: str,
) -> None:
    session = _session(
        max_agent_rounds=3 if limit == "rounds" else 26,
        max_schema_no_tool_retries=2 if limit == "no_tool" else 3,
        min_round_seconds=1 if limit == "deadline" else 0,
    )
    calls = 0

    def respond(_: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 3 and limit == "deadline":
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
        assert calls == 3
        assert not agent.model._schema_reasoning_recovery


async def test_models_do_not_share_recovery_state() -> None:
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: _response(_body()))
    ) as client:
        first, second = _model(client), _model(client)
        assert [await _round(first) for _ in range(3)] == [False, False, True]
        assert not await _round(second)
        assert second._reasoning_length_streak == 1


@pytest.mark.parametrize("concurrent", [False, True])
async def test_reused_generator_keeps_recovery_local_to_each_request(
    monkeypatch: pytest.MonkeyPatch,
    concurrent: bool,
) -> None:
    schema = {
        "type": "object",
        "properties": {"value": {"type": "string"}},
        "required": ["value"],
        "additionalProperties": False,
    }
    requests: dict[str, list[dict[str, Any]]] = {"first": [], "second": []}

    async def respond(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        key = "first" if "synthetic-first" in json.dumps(payload) else "second"
        requests[key].append(payload)
        await asyncio.sleep(0)
        body = _body()
        if key == "second" or len(requests[key]) == 4:
            body = _body(reason="tool_calls", thinking=None, tool=True)
            body["choices"][0]["message"]["tool_calls"][0]["function"]["arguments"] = (
                json.dumps({"result_schema": schema})
            )
        return _response(body)

    real_client = openai.AsyncClient
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        monkeypatch.setattr(
            openai,
            "AsyncClient",
            lambda **kwargs: real_client(**kwargs, http_client=client),
        )
        generator = TtpGenerator(
            settings=TtpGeneratorSettings(
                api_key="offline",
                model_name="offline",
                base_url="https://api.deepseek.com",
            ),
            policy=GenerationPolicy(max_agent_rounds=8),
        )

        async def generate(key: str) -> Any:
            return await generator.propose_schema(
                GenerationRequest(command_outputs=[f"synthetic-{key}"])
            )

        if concurrent:
            results = await asyncio.gather(generate("first"), generate("second"))
        else:
            results = [await generate("first"), await generate("second")]
    assert all(result.status == "success" for result in results)
    assert len(requests["first"]) == 4
    assert len(requests["second"]) == 1
    assert requests["first"][3]["reasoning_effort"] == "low"
    assert "thinking" not in requests["first"][3]
    assert "reasoning_effort" not in requests["second"][0]
    assert "thinking" not in requests["second"][0]
