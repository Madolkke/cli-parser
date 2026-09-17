"""Exercise the actual OpenAI SDK serialization without provider calls."""

import json

import httpx
import pytest
from agentscope.credential import OpenAICredential
from agentscope.message import UserMsg
from agentscope.model import OpenAIChatModel

from cli_parser_agent.ttp_generation.agent.model_attempts import (
    ModelAttemptRecorder,
    ObservedOpenAIChatModel,
)
from cli_parser_agent.ttp_generation.agent.session import GenerationSession


def _completion_response():
    return httpx.Response(
        200,
        json={
            "id": "offline-budget",
            "object": "chat.completion",
            "created": 0,
            "model": "offline-model",
            "choices": [
                {
                    "index": 0,
                    "finish_reason": "stop",
                    "message": {"role": "assistant", "content": "ok"},
                }
            ],
            "usage": {
                "prompt_tokens": 1,
                "completion_tokens": 1,
                "total_tokens": 2,
            },
        },
    )


@pytest.mark.parametrize("budget", [8192, 16384])
@pytest.mark.parametrize(
    ("base_url", "expected_key"),
    [
        ("https://api.deepseek.com/v1", "max_tokens"),
        ("https://api.openai.com/v1", "max_completion_tokens"),
        ("https://api.deepseek.com.example.invalid/v1", "max_completion_tokens"),
    ],
)
async def test_provider_budget_serializes_exactly_one_limit(
    base_url, expected_key, budget
):
    requests = []

    def respond(request):
        requests.append(json.loads(request.content))
        return _completion_response()

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        model = ObservedOpenAIChatModel(
            attempt_recorder=ModelAttemptRecorder(
                GenerationSession(
                    command_outputs=("synthetic",),
                    schema_validator=lambda _: None,
                    template_validator=lambda _: None,
                ),
                "schema",
            ),
            credential=OpenAICredential(api_key="offline", base_url=base_url),
            model="offline-model",
            parameters=OpenAIChatModel.Parameters(max_tokens=budget, temperature=0),
            stream=False,
            max_retries=0,
            client_kwargs={"http_client": client, "max_retries": 0},
        )
        await model([UserMsg(name="user", content="synthetic")])
        assert model.parameters.max_tokens == budget
    assert len(requests) == 1
    assert requests[0][expected_key] == budget
    other = (
        "max_tokens"
        if expected_key == "max_completion_tokens"
        else "max_completion_tokens"
    )
    assert other not in requests[0]


async def test_deepseek_budget_experiment_changes_only_wire_output_limit():
    requests = []

    def respond(request):
        requests.append(json.loads(request.content))
        return _completion_response()

    tools = [
        {
            "type": "function",
            "function": {
                "name": "submit_result_schema",
                "description": "Submit the complete synthetic result schema.",
                "parameters": {
                    "type": "object",
                    "properties": {"result_schema": {"type": "object"}},
                    "required": ["result_schema"],
                    "additionalProperties": False,
                },
            },
        }
    ]
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        for budget in (8192, 16384):
            model = ObservedOpenAIChatModel(
                attempt_recorder=ModelAttemptRecorder(
                    GenerationSession(
                        command_outputs=("synthetic",),
                        schema_validator=lambda _: None,
                        template_validator=lambda _: None,
                    ),
                    "schema",
                ),
                credential=OpenAICredential(
                    api_key="offline", base_url="https://api.deepseek.com"
                ),
                model="deepseek-v4-flash",
                parameters=OpenAIChatModel.Parameters(
                    max_tokens=budget,
                    temperature=0,
                    parallel_tool_calls=False,
                    thinking_enable=False,
                    reasoning_effort=None,
                ),
                stream=False,
                max_retries=0,
                context_size=128000,
                client_kwargs={"http_client": client, "max_retries": 0},
            )
            await model([UserMsg(name="user", content="synthetic")], tools=tools)

    assert len(requests) == 2
    assert [request["max_tokens"] for request in requests] == [8192, 16384]
    assert {
        key
        for key in requests[0].keys() | requests[1].keys()
        if requests[0].get(key) != requests[1].get(key)
    } == {"max_tokens"}
    for request in requests:
        assert "max_completion_tokens" not in request
        assert "thinking" not in request
        assert "reasoning_effort" not in request
        assert "tool_choice" not in request
        assert request["parallel_tool_calls"] is False
        assert request["tools"] == tools


@pytest.mark.parametrize("key", ["max_tokens", "max_completion_tokens"])
async def test_official_deepseek_cannot_override_budget_in_extra_body(key):
    model = ObservedOpenAIChatModel(
        attempt_recorder=ModelAttemptRecorder(
            GenerationSession(
                command_outputs=("synthetic",),
                schema_validator=lambda _: None,
                template_validator=lambda _: None,
            ),
            "schema",
        ),
        credential=OpenAICredential(
            api_key="offline", base_url="https://api.deepseek.com"
        ),
        model="offline-model",
        parameters=OpenAIChatModel.Parameters(max_tokens=8192),
        extra_body={key: 65536},
        stream=False,
        max_retries=0,
    )
    with pytest.raises(ValueError, match="Output limits"):
        await model._call_api(
            "offline-model", [UserMsg(name="user", content="synthetic")]
        )
