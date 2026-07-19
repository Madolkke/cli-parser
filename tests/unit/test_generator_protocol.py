from __future__ import annotations

from typing import Any

import httpx
import pytest

from cli_parser_agent import (
    GenerationPolicy,
    GenerationRequest,
    TtpGenerator,
    TtpGeneratorSettings,
)
from cli_parser_agent.ttp_generation import generator as generator_module
from cli_parser_agent.ttp_generation.agent import AgentRunOutcome


def _generator(*, max_agent_rounds: int = 12) -> TtpGenerator:
    return TtpGenerator(
        settings=TtpGeneratorSettings(
            api_key="secret",
            model_name="test-model",
        ),
        policy=GenerationPolicy(max_agent_rounds=max_agent_rounds),
    )


def _install_agent_stubs(
    monkeypatch: pytest.MonkeyPatch,
    run: Any,
) -> None:
    monkeypatch.setattr(generator_module, "build_agent", lambda **_: object())

    async def estimate(*_: Any, **__: Any) -> int:
        return 1

    monkeypatch.setattr(generator_module, "estimate_initial_model_tokens", estimate)
    monkeypatch.setattr(generator_module, "run_generation_agent", run)


async def test_no_tool_retry_limit_returns_new_issue_and_metadata(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def run(agent: Any, message: Any, session: Any) -> AgentRunOutcome:
        del agent, message
        session.agent_rounds = 4
        session.schema_no_tool_responses = 4
        session.schema_no_tool_retries = 3
        session.terminal_reason = "model_no_tool_retry_limit"
        return AgentRunOutcome(model_no_tool_retry_limit=True)

    _install_agent_stubs(monkeypatch, run)
    result = await _generator().generate(
        GenerationRequest(command_outputs=["value: one"]),
    )

    assert result.status == "failed"
    assert result.metadata.termination_reason == "model_no_tool_retry_limit"
    assert result.metadata.schema_no_tool_responses == 4
    assert result.metadata.schema_no_tool_retries == 3
    assert result.metadata.ttp_no_tool_responses == 0
    assert result.last_attempt is None
    assert [issue.code for issue in result.issues] == [
        "model.submission_tool_not_called",
    ]


async def test_malformed_tool_call_uses_distinct_failure_code(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def run(agent: Any, message: Any, session: Any) -> AgentRunOutcome:
        del agent, message
        session.agent_rounds = 2
        session.tool_call_starts = 2
        session.tool_result_errors = 2
        session.submission_tool_call_invalids = 2
        return AgentRunOutcome(
            exceeded_max_iters=True,
            tool_call_starts=2,
            tool_result_errors=2,
            submission_tool_call_invalids=2,
        )

    _install_agent_stubs(monkeypatch, run)
    result = await _generator(max_agent_rounds=2).generate(
        GenerationRequest(command_outputs=["value: one"]),
    )

    assert result.status == "failed"
    assert result.metadata.termination_reason == ("model_submission_tool_call_invalid")
    assert [issue.code for issue in result.issues] == [
        "model.submission_tool_call_invalid",
    ]


async def test_provider_parameter_rejection_is_a_generic_model_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    secret_provider_text = "provider echoed secret command output"

    async def run(agent: Any, message: Any, session: Any) -> AgentRunOutcome:
        del agent, message, session
        raise httpx.ConnectError(
            secret_provider_text,
            request=httpx.Request("POST", "https://example.test"),
        )

    _install_agent_stubs(monkeypatch, run)
    result = await _generator().generate(
        GenerationRequest(command_outputs=["value: one"]),
    )

    assert result.status == "failed"
    assert result.metadata.termination_reason == "model_error"
    assert [issue.code for issue in result.issues] == ["model.request_failed"]
    assert result.issues[0].details == {"exception_type": "ConnectError"}
    assert secret_provider_text not in result.model_dump_json()
