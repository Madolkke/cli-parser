from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import openai
import pytest
from agentscope.model import OpenAIChatModel
from openai.types.chat import ChatCompletion

from cli_parser_agent.config import GenerationPolicy, TtpGeneratorSettings
from cli_parser_agent.ttp_generation.agent import (
    FINISH_GENERATION_TOOL_NAME,
    SCHEMA_SYSTEM_PROMPT,
    SUBMIT_SCHEMA_TOOL_NAME,
    SUBMIT_TEMPLATE_TOOL_NAME,
    TTP_SYSTEM_PROMPT,
    GenerationPhase,
    GenerationSession,
    SchemaCandidate,
    TemplateCandidate,
    build_agent,
    build_schema_task_message,
    build_ttp_task_message,
    estimate_initial_model_tokens,
)


def _schema() -> dict[str, Any]:
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "type": "object",
        "properties": {"value": {"type": "string"}},
        "required": ["value"],
        "additionalProperties": False,
    }


def _unused_schema_validator(candidate: SchemaCandidate) -> Any:
    raise AssertionError(f"schema validator should not be called: {candidate!r}")


def _unused_template_validator(candidate: TemplateCandidate) -> Any:
    raise AssertionError(f"template validator should not be called: {candidate!r}")


def _build_session() -> GenerationSession:
    return GenerationSession(
        command_outputs=["value: one"],
        schema_validator=_unused_schema_validator,
        template_validator=_unused_template_validator,
    )


def _build_test_agent(
    phase: GenerationPhase,
    *,
    session: GenerationSession | None = None,
) -> Any:
    settings = TtpGeneratorSettings(
        api_key="test-key",
        model_name="test-model",
    )
    return build_agent(
        settings=settings,
        policy=GenerationPolicy(),
        session=_build_session() if session is None else session,
        phase=phase,
    )


def test_builder_constructs_openai_model_without_extra_body() -> None:
    agent = _build_test_agent("schema")

    assert isinstance(agent.model, OpenAIChatModel)
    assert agent.model.extra_body is None


def test_builder_does_not_reconfigure_request_session_policy() -> None:
    session = GenerationSession(
        command_outputs=["value: one"],
        schema_validator=_unused_schema_validator,
        template_validator=_unused_template_validator,
        max_ttp_submissions=2,
        max_agent_rounds=4,
        max_schema_no_tool_retries=1,
        max_ttp_no_tool_retries=2,
        deadline_monotonic=123.0,
    )
    policy = GenerationPolicy(
        total_timeout_seconds=90,
        max_agent_rounds=9,
        max_ttp_submissions=7,
        max_schema_no_tool_retries=3,
        max_ttp_no_tool_retries=3,
    )
    settings = TtpGeneratorSettings(
        api_key="test-key",
        model_name="test-model",
    )

    agent = build_agent(
        settings=settings,
        policy=policy,
        session=session,
        phase="schema",
    )

    assert agent.react_config.max_iters == policy.max_agent_rounds
    assert session.max_ttp_submissions == 2
    assert session.max_agent_rounds == 4
    assert session.max_schema_no_tool_retries == 1
    assert session.max_ttp_no_tool_retries == 2
    assert session.deadline_monotonic == 123.0


async def test_phase_agents_have_independent_runtime_components() -> None:
    session = _build_session()
    session.frozen_schema = _schema()

    schema_agent = _build_test_agent("schema", session=session)
    ttp_agent = _build_test_agent("ttp", session=session)

    assert schema_agent.name == "ttp_schema_generator"
    assert ttp_agent.name == "ttp_template_generator"
    assert schema_agent is not ttp_agent
    assert schema_agent.model is not ttp_agent.model
    assert schema_agent.state is not ttp_agent.state
    assert schema_agent.toolkit is not ttp_agent.toolkit

    schema_tools = await schema_agent.toolkit.get_tool_schemas(
        schema_agent.state.tool_context.activated_groups,
    )
    ttp_tools = await ttp_agent.toolkit.get_tool_schemas(
        ttp_agent.state.tool_context.activated_groups,
    )
    assert [item["function"]["name"] for item in schema_tools] == [
        SUBMIT_SCHEMA_TOOL_NAME,
    ]
    assert [item["function"]["name"] for item in ttp_tools] == [
        SUBMIT_TEMPLATE_TOOL_NAME,
        FINISH_GENERATION_TOOL_NAME,
    ]


@pytest.mark.parametrize(
    ("phase", "expected_tools", "expected_prompt"),
    [
        ("schema", [SUBMIT_SCHEMA_TOOL_NAME], SCHEMA_SYSTEM_PROMPT),
        (
            "ttp",
            [SUBMIT_TEMPLATE_TOOL_NAME, FINISH_GENERATION_TOOL_NAME],
            TTP_SYSTEM_PROMPT,
        ),
    ],
)
async def test_initial_token_estimate_counts_phase_tools(
    monkeypatch: pytest.MonkeyPatch,
    phase: GenerationPhase,
    expected_tools: list[str],
    expected_prompt: str,
) -> None:
    agent = _build_test_agent(phase)
    captured_messages: list[Any] = []
    captured_tools: list[dict[str, Any]] = []

    async def capture_count_tokens(
        model: OpenAIChatModel,
        messages: list[Any],
        tools: list[dict[str, Any]],
    ) -> int:
        del model
        captured_messages.extend(messages)
        captured_tools.extend(tools)
        return 123

    monkeypatch.setattr(OpenAIChatModel, "count_tokens", capture_count_tokens)
    message = (
        build_schema_task_message(["value: one"])
        if phase == "schema"
        else build_ttp_task_message(["value: one"], _schema())
    )

    count = await estimate_initial_model_tokens(agent, message, phase)

    assert count == 123
    assert [schema["function"]["name"] for schema in captured_tools] == [
        *expected_tools,
    ]
    assert captured_messages[0].get_text_content() == expected_prompt


async def test_initial_token_estimate_rejects_phase_tool_mismatch() -> None:
    schema_agent = _build_test_agent("schema")

    with pytest.raises(RuntimeError, match="ordered tool schemas"):
        await estimate_initial_model_tokens(
            schema_agent,
            build_ttp_task_message(["value: one"], _schema()),
            "ttp",
        )


@pytest.mark.parametrize(
    ("phase", "expected_tool_names"),
    [
        ("schema", [SUBMIT_SCHEMA_TOOL_NAME]),
        ("ttp", [SUBMIT_TEMPLATE_TOOL_NAME, FINISH_GENERATION_TOOL_NAME]),
    ],
)
async def test_model_wire_request_exposes_only_isolated_phase_tools(
    monkeypatch: pytest.MonkeyPatch,
    phase: GenerationPhase,
    expected_tool_names: list[str],
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
                        "message": {"role": "assistant", "content": "done"},
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
    agent = _build_test_agent(phase)
    message = (
        build_schema_task_message(["value: one"])
        if phase == "schema"
        else build_ttp_task_message(["value: one"], _schema())
    )

    async for _ in agent.reply_stream(message):
        pass

    assert len(captured_requests) == 1
    request = captured_requests[0]
    assert [schema["function"]["name"] for schema in request["tools"]] == [
        *expected_tool_names,
    ]
    assert request["parallel_tool_calls"] is False
    assert "tool_choice" not in request
