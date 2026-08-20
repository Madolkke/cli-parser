from __future__ import annotations

import logging
from types import SimpleNamespace
from typing import Any

import httpx
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
    settings: TtpGeneratorSettings | None = None,
) -> Any:
    if settings is None:
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


@pytest.mark.parametrize("phase", ["schema", "ttp"])
def test_builder_passes_extra_body_to_both_phase_models(
    phase: GenerationPhase,
) -> None:
    extra_body = {
        "reasoning_effort": "provider-high",
        "max_tokens": 321,
        "thinking": {"enabled": True},
    }
    agent = _build_test_agent(
        phase,
        settings=TtpGeneratorSettings(
            api_key="test-key",
            model_name="test-model",
            extra_body=extra_body,
        ),
    )

    assert agent.model.extra_body == extra_body


def test_builder_injects_an_unverified_http_client_only_when_opted_in(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from cli_parser_agent.ttp_generation.agent import builder as builder_module

    captured: dict[str, object] = {}
    http_client = object()

    def build_http_client(**kwargs: object) -> object:
        captured.update(kwargs)
        return http_client

    monkeypatch.setattr(builder_module.httpx, "AsyncClient", build_http_client)
    settings = TtpGeneratorSettings(
        api_key="test-key",
        model_name="test-model",
        verify_tls=False,
    )
    agent = build_agent(
        settings=settings,
        policy=GenerationPolicy(),
        session=_build_session(),
        phase="schema",
    )

    assert captured == {"verify": False, "timeout": httpx.Timeout(60.0)}
    assert agent.model.client_kwargs["http_client"] is http_client


def test_model_timeout_applies_to_every_phase_and_retries_stay_single_layer() -> None:
    """Timeout reaches every httpx phase and retry accounting is not doubled.

    This does NOT make ``model_timeout_seconds`` a total per-call cap: httpx
    has no total-request timeout, and ``read`` only bounds the gap between
    reads, so a steadily streaming slow response is never cut off (a 599s call
    was observed live under a 120s setting).  The real per-call backstop is the
    generation deadline plus the pre-round guard, not this value.
    """

    settings = TtpGeneratorSettings(
        api_key="test-key",
        model_name="test-model",
        model_timeout_seconds=45.0,
        model_max_retries=2,
    )

    agent = build_agent(
        settings=settings,
        policy=GenerationPolicy(),
        session=_build_session(),
        phase="schema",
    )

    timeout = agent.model.client_kwargs["timeout"]
    assert isinstance(timeout, httpx.Timeout)
    assert timeout.connect == 45.0
    assert timeout.read == 45.0
    assert timeout.write == 45.0
    assert timeout.pool == 45.0

    # Retry accounting stays with AgentScope only.
    assert agent.model.client_kwargs["max_retries"] == 0
    assert agent.model.max_retries == settings.model_max_retries


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


@pytest.mark.parametrize(
    ("thinking_enable", "reasoning_effort", "expected_effort"),
    [
        (None, None, None),
        (True, "high", "high"),
        (False, "high", "none"),
    ],
)
@pytest.mark.parametrize("phase", ["schema", "ttp"])
async def test_builder_maps_reasoning_settings_to_openai_request(
    monkeypatch: pytest.MonkeyPatch,
    phase: GenerationPhase,
    thinking_enable: bool | None,
    reasoning_effort: str | None,
    expected_effort: str | None,
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
    agent = _build_test_agent(
        phase,
        settings=TtpGeneratorSettings(
            api_key="test-key",
            model_name="test-model",
            thinking_enable=thinking_enable,
            reasoning_effort=reasoning_effort,
        ),
    )
    message = (
        build_schema_task_message(["value: one"])
        if phase == "schema"
        else build_ttp_task_message(["value: one"], _schema())
    )

    async for _ in agent.reply_stream(message):
        pass

    assert len(captured_requests) == 1
    request = captured_requests[0]
    if expected_effort is None:
        assert "reasoning_effort" not in request
    else:
        assert request["reasoning_effort"] == expected_effort


@pytest.mark.parametrize("phase", ["schema", "ttp"])
async def test_builder_sends_complete_extra_body_on_every_model_request(
    monkeypatch: pytest.MonkeyPatch,
    phase: GenerationPhase,
) -> None:
    captured_requests: list[dict[str, Any]] = []
    extra_body = {
        "reasoning_effort": "provider-high",
        "max_tokens": 321,
        "thinking": {"enabled": True},
    }

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
    agent = _build_test_agent(
        phase,
        settings=TtpGeneratorSettings(
            api_key="test-key",
            model_name="test-model",
            reasoning_effort="high",
            thinking_enable=True,
            extra_body=extra_body,
        ),
    )
    message = (
        build_schema_task_message(["value: one"])
        if phase == "schema"
        else build_ttp_task_message(["value: one"], _schema())
    )

    for _ in range(2):
        async for _event in agent.reply_stream(message):
            pass

    assert len(captured_requests) == 2
    for request in captured_requests:
        assert request["extra_body"] == extra_body
        assert request["reasoning_effort"] == "high"
        assert "tool_choice" not in request


def test_retry_log_filter_scrubs_text_and_counts_against_current_session() -> None:
    secret_provider_text = "provider echoed secret command output"
    session = _build_session()

    _build_test_agent("schema", session=session)

    logger = logging.getLogger("as")
    record = logger.makeRecord(
        logger.name,
        logging.WARNING,
        __file__,
        0,
        "Attempt %d failed for model %s: %s. Retrying in %.1fs...",
        (1, "test-model", secret_provider_text, 1.0),
        None,
    )
    assert all(handler_filter.filter(record) for handler_filter in logger.filters)

    assert session.model_retries_observed == 1
    formatted = record.getMessage()
    assert secret_provider_text not in formatted
    assert formatted == "Model request failed; retrying without response details."


def test_retry_log_filter_scopes_counting_to_the_most_recently_built_session() -> None:
    first_session = _build_session()
    second_session = _build_session()

    _build_test_agent("schema", session=first_session)
    _build_test_agent("schema", session=second_session)

    logger = logging.getLogger("as")
    record = logger.makeRecord(
        logger.name,
        logging.WARNING,
        __file__,
        0,
        "Attempt %d failed for model %s: %s. Retrying in %.1fs...",
        (1, "test-model", "irrelevant", 1.0),
        None,
    )
    for handler_filter in logger.filters:
        handler_filter.filter(record)

    assert second_session.model_retries_observed == 1
    assert first_session.model_retries_observed == 0
