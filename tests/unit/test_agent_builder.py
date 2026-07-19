from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import openai
import pytest
from agentscope.model import OpenAIChatModel
from openai.types.chat import ChatCompletion

from cli_parser_agent.config import GenerationPolicy, TtpGeneratorSettings
from cli_parser_agent.ttp_generation.agent import (
    GenerationSession,
    SchemaCandidate,
    TemplateCandidate,
    build_agent,
    build_task_message,
    estimate_initial_model_tokens,
)
from cli_parser_agent.ttp_generation.agent.tools import (
    SUBMIT_SCHEMA_TOOL_NAME,
    SUBMIT_TEMPLATE_TOOL_NAME,
)


def _unused_schema_validator(candidate: SchemaCandidate) -> Any:
    raise AssertionError(f"schema validator should not be called: {candidate!r}")


def _unused_template_validator(candidate: TemplateCandidate) -> Any:
    raise AssertionError(f"template validator should not be called: {candidate!r}")


def _build_test_agent(*, schema_frozen: bool = False) -> Any:
    settings = TtpGeneratorSettings(
        api_key="test-key",
        model_name="test-model",
    )
    session = GenerationSession(
        command_outputs=["value: one"],
        schema_validator=_unused_schema_validator,
        template_validator=_unused_template_validator,
    )
    if schema_frozen:
        session.frozen_schema = {
            "$schema": "https://json-schema.org/draft/2020-12/schema",
            "type": "object",
            "properties": {},
            "required": [],
            "additionalProperties": False,
        }

    return build_agent(
        settings=settings,
        policy=GenerationPolicy(),
        session=session,
    )


def test_builder_constructs_openai_model_without_extra_body() -> None:
    agent = _build_test_agent()

    assert isinstance(agent.model, OpenAIChatModel)
    assert agent.model.extra_body is None


async def test_initial_token_estimate_counts_only_schema_phase_tool(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    agent = _build_test_agent()
    captured_tools: list[dict[str, Any]] = []

    async def capture_count_tokens(
        model: OpenAIChatModel,
        messages: list[Any],
        tools: list[dict[str, Any]],
    ) -> int:
        del model, messages
        captured_tools.extend(tools)
        return 123

    monkeypatch.setattr(OpenAIChatModel, "count_tokens", capture_count_tokens)

    count = await estimate_initial_model_tokens(
        agent,
        build_task_message(["value: one"]),
    )

    assert count == 123
    assert [schema["function"]["name"] for schema in captured_tools] == [
        SUBMIT_SCHEMA_TOOL_NAME
    ]


@pytest.mark.parametrize(
    ("schema_frozen", "expected_tool_name"),
    [
        (False, SUBMIT_SCHEMA_TOOL_NAME),
        (True, SUBMIT_TEMPLATE_TOOL_NAME),
    ],
)
async def test_model_wire_request_exposes_only_current_phase_tool(
    monkeypatch: pytest.MonkeyPatch,
    schema_frozen: bool,
    expected_tool_name: str,
) -> None:
    captured_requests: list[dict[str, Any]] = []

    async def create_completion(**kwargs: Any) -> ChatCompletion:
        captured_requests.append(kwargs)
        return ChatCompletion.model_validate(
            {
                "id": "chatcmpl-test",
                "object": "chat.completion",
                "created": 0,
                "model": "test-model",
                "choices": [
                    {
                        "index": 0,
                        "finish_reason": "stop",
                        "message": {
                            "role": "assistant",
                            "content": "done",
                        },
                    },
                ],
                "usage": {
                    "prompt_tokens": 1,
                    "completion_tokens": 1,
                    "total_tokens": 2,
                },
            },
        )

    def build_fake_client(**kwargs: Any) -> SimpleNamespace:
        del kwargs
        return SimpleNamespace(
            chat=SimpleNamespace(
                completions=SimpleNamespace(create=create_completion),
            ),
        )

    monkeypatch.setattr(openai, "AsyncClient", build_fake_client)
    agent = _build_test_agent(schema_frozen=schema_frozen)

    async for _ in agent.reply_stream(build_task_message(["value: one"])):
        pass

    assert len(captured_requests) == 1
    request = captured_requests[0]
    assert [schema["function"]["name"] for schema in request["tools"]] == [
        expected_tool_name
    ]
    assert request["parallel_tool_calls"] is False
    assert "tool_choice" not in request
