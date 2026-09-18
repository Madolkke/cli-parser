"""External cancellation never schedules another Schema provider request."""

import asyncio
import json

import httpx
import openai
import pytest
from agentscope.agent import ReActConfig
from agentscope.event import ToolResultEndEvent
from agentscope.message import ToolResultState, UserMsg
from agentscope.state import AgentState

from cli_parser_agent import GenerationRequest, TtpGenerator, TtpGeneratorSettings
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


@pytest.mark.parametrize("preceding", ["none", "length", "business_rejected"])
async def test_cancelled_provider_call_never_schedules_another_request(
    monkeypatch, preceding
):
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
