"""Exercise bounded reasoning recovery through the public API and actual wire."""

import json

import httpx
import openai
import pytest

from cli_parser_agent import (
    GenerationPolicy,
    GenerationRequest,
    TtpGenerator,
    TtpGeneratorSettings,
)
from cli_parser_agent.ttp_generation.agent.prompt import (
    SCHEMA_SYSTEM_PROMPT,
    build_ttp_task_prompt,
)


def completion(*, length=False, tool=None, args=None):
    message = {"role": "assistant", "content": None}
    if length:
        message["reasoning_content"] = "private-unfinished-analysis"
    if tool:
        message["tool_calls"] = [
            {
                "id": "call-" + tool,
                "type": "function",
                "function": {"name": tool, "arguments": json.dumps(args)},
            }
        ]
    return {
        "id": "offline-recovery",
        "object": "chat.completion",
        "created": 0,
        "model": "deepseek-flash",
        "choices": [
            {
                "index": 0,
                "finish_reason": "length" if length else "tool_calls",
                "message": message,
            }
        ],
        "usage": {
            "prompt_tokens": 10,
            "completion_tokens": 8192 if length else 30,
            "total_tokens": 8202 if length else 40,
        },
    }


@pytest.mark.parametrize("failed_rounds", [0, 1, 2, 3])
async def test_recovery_changes_only_fourth_schema_request_and_never_ttp(
    monkeypatch, failed_rounds
):
    schema = {
        "type": "object",
        "properties": {"value": {"type": "string"}},
        "required": ["value"],
        "additionalProperties": False,
    }
    responses = [completion(length=True) for _ in range(failed_rounds)] + [
        completion(tool="submit_result_schema", args={"result_schema": schema}),
        completion(
            tool="submit_ttp_template", args={"ttp_template": "Value: {{ value }}"}
        ),
        completion(tool="finish_generation", args={}),
    ]
    requests = []

    def respond(request):
        requests.append(json.loads(request.content))
        assert len(requests) <= len(responses)
        return httpx.Response(200, json=responses[len(requests) - 1])

    real_client = openai.AsyncClient
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        monkeypatch.setattr(
            openai,
            "AsyncClient",
            lambda **kwargs: real_client(**kwargs, http_client=client),
        )
        result = await TtpGenerator(
            settings=TtpGeneratorSettings(
                api_key="offline",
                model_name="deepseek-v4-flash",
                base_url="https://api.deepseek.com",
                max_tokens=8192,
            ),
            policy=GenerationPolicy(max_agent_rounds=8),
        ).generate(GenerationRequest(command_outputs=["Value: one\n"]))

    assert result.status == "success"
    assert result.artifact.result_schema == schema
    assert result.artifact.records == [{"value": "one"}]
    assert len(requests) == failed_rounds + 3
    for index, request in enumerate(requests):
        assert request["max_tokens"] == 8192
        assert "max_completion_tokens" not in request
        assert "tool_choice" not in request
        assert request["parallel_tool_calls"] is False
        if failed_rounds == 3 and index == 3:
            assert request["thinking"] == {"type": "disabled"}
        else:
            assert "thinking" not in request
        assert "reasoning_effort" not in request
        assert "private-unfinished-analysis" not in json.dumps(request)
    initial = requests[0]
    for request in requests[: failed_rounds + 1]:
        assert request["messages"][:2] == initial["messages"][:2]
        assert request["tools"] == initial["tools"]
        system = request["messages"][0]["content"]
        assert system == SCHEMA_SYSTEM_PROMPT or system == [
            {"type": "text", "text": SCHEMA_SYSTEM_PROMPT}
        ]
    ttp = requests[failed_rounds + 1]
    assert ttp["messages"][-1]["content"] == [
        {"type": "text", "text": build_ttp_task_prompt(["Value: one\n"], schema)}
    ]
