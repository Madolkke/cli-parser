"""Length recovery guidance is isolated from unrelated Schema failure paths."""

import asyncio
import json
from copy import deepcopy

import httpx
import openai
import pytest
from agentscope.agent import ReActConfig
from agentscope.event import ToolResultEndEvent
from agentscope.message import ToolResultState, UserMsg
from agentscope.state import AgentState

from cli_parser_agent import (
    GenerationPolicy,
    GenerationRequest,
    TtpGenerator,
    TtpGeneratorSettings,
)
from cli_parser_agent.ttp_generation.agent.prompt import (
    SCHEMA_NO_TOOL_RETRY_PROMPT,
    SCHEMA_REASONING_LENGTH_RETRY_PROMPT,
)
from cli_parser_agent.ttp_generation.agent.runner import run_generation_phase
from cli_parser_agent.ttp_generation.agent.session import GenerationSession


def schema():
    return {
        "type": "object",
        "properties": {"value": {"type": "string"}},
        "required": ["value"],
        "additionalProperties": False,
    }


def completion(*, finish="length", reasoning="private-reasoning", text=None, args=None):
    message = {"role": "assistant", "content": text}
    if reasoning is not None:
        message["reasoning_content"] = reasoning
    if args is not None:
        message["tool_calls"] = [
            {
                "id": "offline-schema-call",
                "type": "function",
                "function": {
                    "name": "submit_result_schema",
                    "arguments": json.dumps(args),
                },
            }
        ]
    return {
        "id": "offline-length-scope",
        "object": "chat.completion",
        "created": 0,
        "model": "offline",
        "choices": [{"index": 0, "finish_reason": finish, "message": message}],
        "usage": {"prompt_tokens": 10, "completion_tokens": 8192, "total_tokens": 8202},
    }


async def run_responses(monkeypatch, responses, **overrides):
    requests, observed = [], []

    def respond(request):
        requests.append(json.loads(request.content))
        assert len(requests) <= len(responses)
        response = deepcopy(responses[len(requests) - 1])
        response["id"] += f"-{len(requests)}"
        for call in response["choices"][0]["message"].get("tool_calls", []):
            call["id"] += f"-{len(requests)}"
        return httpx.Response(200, json=response)

    settings = {
        "api_key": "offline",
        "model_name": "deepseek-v4-flash",
        "base_url": "https://api.deepseek.com",
        "max_tokens": 8192,
        "model_max_retries": 0,
        **overrides,
    }
    real_client = openai.AsyncClient
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        monkeypatch.setattr(
            openai,
            "AsyncClient",
            lambda **kwargs: real_client(**kwargs, http_client=client),
        )
        result = await TtpGenerator(
            settings=TtpGeneratorSettings(**settings),
            policy=GenerationPolicy(max_agent_rounds=6),
        ).propose_schema(
            GenerationRequest(command_outputs=["Value: one\n"]),
            observer=observed.append,
        )
    assert len(requests) == len(responses)
    return result, requests, observed


@pytest.mark.parametrize(
    "first",
    [
        completion(finish="stop", reasoning=None, text="ordinary reply"),
        completion(finish="length", text="partial ordinary reply"),
        completion(reasoning=None),
        completion(finish="stop"),
    ],
    ids=[
        "ordinary-text",
        "length-with-text",
        "length-without-thinking",
        "thinking-stop",
    ],
)
async def test_non_pure_length_reply_keeps_original_no_tool_retry(monkeypatch, first):
    result, requests, observed = await run_responses(
        monkeypatch,
        [first, completion(finish="tool_calls", args={"result_schema": schema()})],
    )
    assert result.status == "success"
    wire = json.dumps(requests, ensure_ascii=False)
    assert SCHEMA_NO_TOOL_RETRY_PROMPT in wire
    assert SCHEMA_REASONING_LENGTH_RETRY_PROMPT not in wire
    assert "private-reasoning" not in wire
    assert not any(
        getattr(event, "name", None) == "cli_parser.schema.reasoning_history"
        for event in observed
    )


@pytest.mark.parametrize(
    "overrides",
    [
        {"base_url": "https://api.deepseek.com.invalid"},
        {"base_url": "https://api.openai.com/v1"},
        {"thinking_enable": False},
        {"thinking_enable": True, "reasoning_effort": "none"},
        {"extra_body": {"thinking": {"type": "disabled"}}},
        {"extra_body": {"thinking": {"enabled": False}}},
        {"extra_body": {"thinking": False}},
        {"extra_body": {"reasoning_effort": "none"}},
    ],
)
async def test_other_provider_or_explicit_disabled_thinking_never_uses_recovery(
    monkeypatch, overrides
):
    result, requests, observed = await run_responses(
        monkeypatch,
        [
            completion(),
            completion(finish="tool_calls", args={"result_schema": schema()}),
        ],
        **overrides,
    )
    assert result.status == "success"
    wire = json.dumps(requests, ensure_ascii=False)
    assert SCHEMA_NO_TOOL_RETRY_PROMPT in wire
    assert SCHEMA_REASONING_LENGTH_RETRY_PROMPT not in wire
    assert "private-reasoning" not in wire
    assert not any(
        getattr(event, "name", None) == "cli_parser.schema.reasoning_history"
        for event in observed
    )


@pytest.mark.parametrize("rejection", ["business", "arguments"])
async def test_rejected_submission_does_not_receive_length_recovery(
    monkeypatch, rejection
):
    if rejection == "business":
        invalid = {**schema(), "properties": {"class": {"type": "string"}}}
        invalid["required"] = ["class"]
        args = {"result_schema": invalid}
    else:
        args = {"result_schema": schema(), "required": ["value"]}
    result, requests, observed = await run_responses(
        monkeypatch,
        [
            completion(finish="tool_calls", args=args),
            completion(finish="tool_calls", args={"result_schema": schema()}),
        ],
    )
    assert result.status == "success"
    assert result.proposal.result_schema == schema()
    wire = json.dumps(requests, ensure_ascii=False)
    assert SCHEMA_REASONING_LENGTH_RETRY_PROMPT not in wire
    assert SCHEMA_NO_TOOL_RETRY_PROMPT not in wire
    assert not any(
        getattr(event, "name", None) == "cli_parser.schema.reasoning_history"
        and event.value.get("status") == "retained"
        for event in observed
    )
    if rejection == "business":
        assert "schema.python_keyword_property_name" in wire
        assert result.metadata.schema_submissions == 2


@pytest.mark.parametrize("preceding", ["none", "length", "business_rejected"])
async def test_cancelled_provider_call_never_schedules_recovery(monkeypatch, preceding):
    entered = asyncio.Event()
    requests, observed = [], []

    async def respond(request):
        requests.append(json.loads(request.content))
        if len(requests) == 1 and preceding != "none":
            if preceding == "length":
                return httpx.Response(200, json=completion())
            invalid = {**schema(), "properties": {"class": {"type": "string"}}}
            invalid["required"] = ["class"]
            return httpx.Response(
                200,
                json=completion(finish="tool_calls", args={"result_schema": invalid}),
            )
        entered.set()
        await asyncio.Event().wait()
        raise AssertionError("a cancelled provider call must not complete")

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
                model_name="deepseek-v4-flash",
                base_url="https://api.deepseek.com",
                model_max_retries=0,
            ),
        )
        task = asyncio.create_task(
            generator.propose_schema(
                GenerationRequest(command_outputs=["Value: one\n"]),
                observer=observed.append,
            )
        )
        try:
            async with asyncio.timeout(3):
                await entered.wait()
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await task
        finally:
            if not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
    assert len(requests) == (1 if preceding == "none" else 2)
    expected_retries = int(preceding == "length")
    assert (
        sum(
            SCHEMA_REASONING_LENGTH_RETRY_PROMPT
            in json.dumps(request, ensure_ascii=False)
            for request in requests
        )
        == expected_retries
    )
    assert (
        sum(
            getattr(event, "name", None) == "cli_parser.no_tool.retry"
            for event in observed
        )
        == expected_retries
    )
    assert (
        sum(
            getattr(event, "name", None) == "cli_parser.schema.reasoning_history"
            and event.value.get("status") == "retained"
            for event in observed
        )
        == expected_retries
    )


async def test_external_cancel_during_terminal_cleanup_is_not_consumed_as_internal():
    session = GenerationSession(
        command_outputs=("Value: one",),
        schema_validator=lambda _: None,
        template_validator=lambda _: None,
    )

    class CleanupAgent:
        def __init__(self):
            self.state = AgentState()
            self.react_config = ReActConfig(max_iters=2)
            self.internal_interruption_seen = False

        async def reply_stream(self, message):
            self.state.context.append(message)
            session.frozen_schema = schema()
            try:
                yield ToolResultEndEvent(
                    reply_id="reply",
                    tool_call_id="schema",
                    state=ToolResultState.SUCCESS,
                )
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                self.internal_interruption_seen = True
                task = asyncio.current_task()
                assert task is not None
                assert task.cancelling() == 1
                task.cancel("external cancel during internal cleanup")
                # Some AgentScope cleanup paths yield events before re-raising.
                yield object()
                raise

    agent = CleanupAgent()
    task = asyncio.create_task(
        run_generation_phase(
            agent, UserMsg(name="user", content="Value: one"), session, "schema"
        )
    )
    with pytest.raises(asyncio.CancelledError):
        await task
    assert agent.internal_interruption_seen
    assert task.cancelled()
    assert session.schema_no_tool_responses == 0
