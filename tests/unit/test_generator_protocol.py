from __future__ import annotations

import asyncio
from contextlib import contextmanager
from types import SimpleNamespace
from typing import Any

import httpx
import pytest

from cli_parser_agent import (
    ArtifactBundle,
    GenerationMetadata,
    GenerationPolicy,
    GenerationRequest,
    GenerationResult,
    TtpGenerator,
    TtpGeneratorSettings,
    ValidationIssue,
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


async def test_generation_result_captures_the_active_laminar_trace(
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
    finishes: list[dict[str, Any]] = []

    @contextmanager
    def start(*_: Any, **__: Any) -> Any:
        yield SimpleNamespace(enabled=True, creates_trace=True)

    monkeypatch.setattr(
        generator_module,
        "initialize_laminar_from_env",
        lambda _=None: True,
    )
    monkeypatch.setattr(
        generator_module,
        "current_laminar_trace_id",
        lambda: "01234567-89ab-cdef-0123-456789abcdef",
    )
    monkeypatch.setattr(generator_module, "start_laminar_span", start)
    monkeypatch.setattr(
        generator_module,
        "finish_laminar_span",
        lambda **kwargs: finishes.append(kwargs),
    )

    result = await _generator().generate(
        GenerationRequest(command_outputs=["value: one"]),
    )

    assert result.metadata.laminar_trace_id == "01234567-89ab-cdef-0123-456789abcdef"
    assert finishes[0]["outcome"] == "failed"
    assert finishes[0]["output"] == result.model_dump(mode="json")
    assert finishes[0]["trace_metadata"] == {
        "request_id": result.metadata.request_id,
        "model_name": "test-model",
        "prompt_version": result.metadata.prompt_version,
        "command_output_count": 1,
        "schema_submissions": 0,
        "ttp_submissions": 0,
        "termination_reason": "model_no_tool_retry_limit",
        "status": "failed",
    }


async def test_generator_remains_compatible_when_laminar_is_disabled(
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
    initialization_calls: list[object] = []
    monkeypatch.setattr(
        generator_module,
        "initialize_laminar_from_env",
        lambda environ=None: initialization_calls.append(environ) or False,
    )
    monkeypatch.setattr(
        generator_module,
        "current_laminar_trace_id",
        lambda: None,
    )

    result = await _generator().generate(
        GenerationRequest(command_outputs=["value: one"]),
    )

    assert result.status == "failed"
    assert result.metadata.laminar_trace_id is None
    assert initialization_calls == [None]


async def test_inherited_trace_is_not_given_generate_owned_metadata(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    generator = _generator()
    finishes: list[dict[str, Any]] = []

    @contextmanager
    def start(*_: Any, **__: Any) -> Any:
        yield SimpleNamespace(enabled=True, creates_trace=False)

    async def complete(*_: Any, **__: Any) -> GenerationResult:
        return GenerationResult(
            status="failed",
            metadata=GenerationMetadata(
                request_id="request-1",
                model_name="test-model",
                command_output_count=1,
                termination_reason="agent_stopped",
            ),
            issues=[
                ValidationIssue(
                    code="generation.agent_stopped",
                    message="The generation stopped.",
                    stage="generation",
                ),
            ],
        )

    monkeypatch.setattr(generator_module, "start_laminar_span", start)
    monkeypatch.setattr(
        generator_module,
        "finish_laminar_span",
        lambda **kwargs: finishes.append(kwargs),
    )
    monkeypatch.setattr(generator, "_generate", complete)

    await generator.generate(GenerationRequest(command_outputs=["value: one"]))

    assert finishes[0]["trace_metadata"] is None


async def test_successful_generation_finishes_the_root_span_with_full_result(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    generator = _generator()
    starts: list[dict[str, Any]] = []
    finishes: list[dict[str, Any]] = []

    @contextmanager
    def start(name: str, **kwargs: Any) -> Any:
        starts.append({"name": name, **kwargs})
        yield SimpleNamespace(enabled=True, creates_trace=True)

    async def complete(
        request: GenerationRequest,
        *,
        request_id: str,
    ) -> GenerationResult:
        return GenerationResult(
            status="success",
            artifact=ArtifactBundle(
                ttp_template="{{ value }}",
                result_schema={
                    "type": "object",
                    "properties": {"value": {"type": "string"}},
                    "required": ["value"],
                    "additionalProperties": False,
                },
                records=[{"value": "one"}],
                assumptions=[],
            ),
            metadata=GenerationMetadata(
                request_id=request_id,
                model_name="test-model",
                command_output_count=len(request.command_outputs),
                schema_submissions=1,
                ttp_submissions=2,
                termination_reason="success",
            ),
        )

    monkeypatch.setattr(generator_module, "start_laminar_span", start)
    monkeypatch.setattr(
        generator_module,
        "finish_laminar_span",
        lambda **kwargs: finishes.append(kwargs),
    )
    monkeypatch.setattr(generator, "_generate", complete)

    result = await generator.generate(
        GenerationRequest(command_outputs=["value: one"]),
    )

    assert result.status == "success"
    assert starts[0]["name"] == "ttp.generate"
    assert starts[0]["input"] == {"command_outputs": ["value: one"]}
    assert "secret" not in str(starts[0])
    assert finishes[0]["outcome"] == "success"
    assert finishes[0]["output"] == result.model_dump(mode="json")
    assert finishes[0]["trace_metadata"]["status"] == "success"


@pytest.mark.parametrize(
    ("error", "expected_outcome", "expected_status"),
    [
        (RuntimeError("private provider response"), "exception", "failed"),
        (
            asyncio.CancelledError("private cancellation text"),
            "cancelled",
            "cancelled",
        ),
    ],
)
async def test_generate_closes_trace_before_propagating_base_exceptions(
    monkeypatch: pytest.MonkeyPatch,
    error: BaseException,
    expected_outcome: str,
    expected_status: str,
) -> None:
    generator = _generator()
    events: list[str] = []
    finishes: list[dict[str, Any]] = []

    @contextmanager
    def start(*_: Any, **__: Any) -> Any:
        events.append("entered")
        try:
            yield SimpleNamespace(enabled=True, creates_trace=True)
        finally:
            events.append("exited")

    async def fail(*_: Any, **__: Any) -> GenerationResult:
        raise error

    def finish(**kwargs: Any) -> None:
        events.append("finished")
        finishes.append(kwargs)

    monkeypatch.setattr(generator_module, "start_laminar_span", start)
    monkeypatch.setattr(generator_module, "finish_laminar_span", finish)
    monkeypatch.setattr(generator, "_generate", fail)

    with pytest.raises(type(error)) as caught:
        await generator.generate(GenerationRequest(command_outputs=["value: one"]))

    assert caught.value is error
    assert events == ["entered", "finished", "exited"]
    assert finishes[0]["outcome"] == expected_outcome
    assert finishes[0]["output"] == {
        "status": expected_status,
        "exception_type": type(error).__name__,
    }
    assert "private" not in str(finishes[0])
