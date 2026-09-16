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


@pytest.mark.parametrize(
    ("base_url", "expected_key"),
    [
        ("https://api.deepseek.com/v1", "max_tokens"),
        ("https://api.openai.com/v1", "max_completion_tokens"),
        ("https://api.deepseek.com.example.invalid/v1", "max_completion_tokens"),
    ],
)
async def test_provider_budget_serializes_exactly_one_limit(base_url, expected_key):
    requests = []

    def respond(request):
        requests.append(json.loads(request.content))
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
            parameters=OpenAIChatModel.Parameters(max_tokens=8192, temperature=0),
            stream=False,
            max_retries=0,
            client_kwargs={"http_client": client, "max_retries": 0},
        )
        await model([UserMsg(name="user", content="synthetic")])
        assert model.parameters.max_tokens == 8192
    assert len(requests) == 1
    assert requests[0][expected_key] == 8192
    other = (
        "max_tokens"
        if expected_key == "max_completion_tokens"
        else "max_completion_tokens"
    )
    assert other not in requests[0]


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
