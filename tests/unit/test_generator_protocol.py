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
from cli_parser_agent.ttp_generation import workflow as workflow_module
from cli_parser_agent.ttp_generation.agent import AgentRunOutcome


def _result_schema() -> dict[str, Any]:
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "type": "object",
        "properties": {"value": {"type": "string"}},
        "required": ["value"],
        "additionalProperties": False,
    }


def _sample(text: str, *, original_char_count: int) -> Any:
    return workflow_module.SampledCommandOutput(
        index=0,
        text=text,
        truncated=len(text) < original_char_count,
        original_char_count=original_char_count,
        allocated_char_budget=len(text),
    )


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
    monkeypatch.setattr(
        workflow_module,
        "build_agent",
        lambda **kwargs: SimpleNamespace(phase=kwargs["phase"]),
    )

    async def estimate(*_: Any, **__: Any) -> int:
        return 1

    monkeypatch.setattr(workflow_module, "estimate_initial_model_tokens", estimate)
    monkeypatch.setattr(workflow_module, "run_generation_phase", run)


async def test_no_tool_retry_limit_returns_new_issue_and_metadata(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def run(
        agent: Any,
        message: Any,
        session: Any,
        phase: str,
    ) -> AgentRunOutcome:
        del agent, message
        assert phase == "schema"
        for _ in range(4):
            session.record_agent_round("schema")
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
    async def run(
        agent: Any,
        message: Any,
        session: Any,
        phase: str,
    ) -> AgentRunOutcome:
        del agent, message
        assert phase == "schema"
        for _ in range(2):
            session.record_agent_round("schema")
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

    async def run(
        agent: Any,
        message: Any,
        session: Any,
        phase: str,
    ) -> AgentRunOutcome:
        del agent, message, session
        assert phase == "schema"
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
    assert result.issues[0].details == {
        "exception_type": "ConnectError",
        "phase": "schema",
    }
    assert secret_provider_text not in result.model_dump_json()


async def test_generation_result_captures_the_active_laminar_trace(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def run(
        agent: Any,
        message: Any,
        session: Any,
        phase: str,
    ) -> AgentRunOutcome:
        del agent, message
        assert phase == "schema"
        for _ in range(4):
            session.record_agent_round("schema")
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
        workflow_module,
        "current_laminar_trace_id",
        lambda: "01234567-89ab-cdef-0123-456789abcdef",
    )
    monkeypatch.setattr(generator_module, "start_laminar_span", start)
    monkeypatch.setattr(workflow_module, "start_laminar_span", start)
    monkeypatch.setattr(
        generator_module,
        "finish_laminar_span",
        lambda **kwargs: finishes.append(kwargs),
    )
    monkeypatch.setattr(
        workflow_module,
        "finish_laminar_span",
        lambda **kwargs: finishes.append(kwargs),
    )

    result = await _generator().generate(
        GenerationRequest(command_outputs=["value: one"]),
    )

    assert result.metadata.laminar_trace_id == "01234567-89ab-cdef-0123-456789abcdef"
    assert finishes[0]["outcome"] == "failed"
    assert finishes[0]["output"]["phase"] == "schema"
    assert finishes[-1]["outcome"] == "failed"
    assert finishes[-1]["output"] == result.model_dump(mode="json")
    assert finishes[-1]["trace_metadata"] == {
        "request_id": result.metadata.request_id,
        "model_name": "test-model",
        "prompt_version": result.metadata.prompt_version,
        "command_output_count": 1,
        "schema_sampled_char_count": len("value: one"),
        "ttp_sampled_char_count": 0,
        "agent_rounds": 4,
        "schema_agent_rounds": 4,
        "ttp_agent_rounds": 0,
        "schema_submissions": 0,
        "ttp_submissions": 0,
        "termination_reason": "model_no_tool_retry_limit",
        "status": "failed",
    }


async def test_generator_remains_compatible_when_laminar_is_disabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def run(
        agent: Any,
        message: Any,
        session: Any,
        phase: str,
    ) -> AgentRunOutcome:
        del agent, message
        assert phase == "schema"
        for _ in range(4):
            session.record_agent_round("schema")
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
        workflow_module,
        "current_laminar_trace_id",
        lambda: None,
    )

    result = await _generator().generate(
        GenerationRequest(command_outputs=["value: one"]),
    )

    assert result.status == "failed"
    assert result.metadata.laminar_trace_id is None
    assert initialization_calls == [None]


async def test_successful_workflow_resamples_and_records_phase_metadata(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    command_output = "value: one\nextra: two"
    frozen_schema = _result_schema()
    built_phases: list[str] = []
    fit_prompts: list[str] = []
    span_stack: list[str] = []
    span_starts: list[dict[str, Any]] = []
    span_finishes: list[dict[str, Any]] = []

    def build(**kwargs: Any) -> Any:
        phase = kwargs["phase"]
        built_phases.append(phase)
        return SimpleNamespace(phase=phase)

    async def fit(
        command_outputs: Any,
        *,
        serialize_prompt: Any,
        **_: Any,
    ) -> tuple[list[Any], bool]:
        del command_outputs
        sample_text = "value: one" if not fit_prompts else "one"
        fit_prompts.append(serialize_prompt([sample_text]))
        return [_sample(sample_text, original_char_count=len(command_output))], True

    async def run(
        agent: Any,
        message: Any,
        session: Any,
        phase: str,
    ) -> AgentRunOutcome:
        del message
        assert agent.phase == phase
        session.record_agent_round(phase)
        if phase == "schema":
            session.schema_submissions = 1
            session.frozen_schema = frozen_schema
            return AgentRunOutcome(phase_completed=True)

        session.ttp_submissions = 1
        session.validated_ttp_template = "value: {{ value }}"
        session.records = ({"value": "one"},)
        session.first_ttp_valid = True
        session.last_issues = ()
        session.record_agent_round("ttp")
        session.generation_finished = True
        session.terminal_reason = "success"
        return AgentRunOutcome(phase_completed=True)

    @contextmanager
    def start_span(name: str, **kwargs: Any) -> Any:
        span_starts.append(
            {
                "name": name,
                "parent": span_stack[-1] if span_stack else None,
                **kwargs,
            },
        )
        span_stack.append(name)
        try:
            yield SimpleNamespace(
                enabled=True,
                creates_trace=name == "ttp.generate",
            )
        finally:
            assert span_stack.pop() == name

    def finish_span(**kwargs: Any) -> None:
        span_finishes.append({"span": span_stack[-1], **kwargs})

    monkeypatch.setattr(workflow_module, "build_agent", build)
    monkeypatch.setattr(workflow_module, "_fit_sampled_outputs", fit)
    monkeypatch.setattr(workflow_module, "run_generation_phase", run)
    monkeypatch.setattr(
        workflow_module,
        "validate_schema_proposal",
        lambda *_args, **_kwargs: [],
    )
    monkeypatch.setattr(
        workflow_module,
        "validate_ttp_template",
        lambda *_args, **_kwargs: SimpleNamespace(
            valid=True,
            issues=(),
            records=[{"value": "one"}],
        ),
    )
    monkeypatch.setattr(generator_module, "start_laminar_span", start_span)
    monkeypatch.setattr(generator_module, "finish_laminar_span", finish_span)
    monkeypatch.setattr(workflow_module, "start_laminar_span", start_span)
    monkeypatch.setattr(workflow_module, "finish_laminar_span", finish_span)

    result = await _generator().generate(
        GenerationRequest(command_outputs=[command_output]),
    )

    assert result.status == "success"
    assert built_phases == ["schema", "ttp"]
    assert frozen_schema == result.artifact.result_schema
    assert len(fit_prompts) == 2
    assert fit_prompts[0] != fit_prompts[1]
    assert result.metadata.schema_sampled_char_count == len("value: one")
    assert result.metadata.ttp_sampled_char_count == len("one")
    assert result.metadata.schema_agent_rounds == 1
    assert result.metadata.ttp_agent_rounds == 2
    assert result.metadata.agent_rounds == 3
    assert [(item["name"], item["parent"]) for item in span_starts] == [
        ("ttp.generate", None),
        ("schema.phase", "ttp.generate"),
        ("ttp.phase", "ttp.generate"),
    ]
    assert [item["span"] for item in span_finishes] == [
        "schema.phase",
        "ttp.phase",
        "ttp.generate",
    ]
    assert span_finishes[0]["outcome"] == "success"
    assert span_finishes[1]["outcome"] == "success"


async def test_valid_ttp_candidate_without_finish_does_not_build_artifact(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def run(
        agent: Any,
        message: Any,
        session: Any,
        phase: str,
    ) -> AgentRunOutcome:
        del agent, message
        session.record_agent_round(phase)
        if phase == "schema":
            session.schema_submissions = 1
            session.frozen_schema = _result_schema()
            return AgentRunOutcome(phase_completed=True)

        session.ttp_submissions = 1
        session.last_ttp_template = "value: {{ value }}"
        session.validated_ttp_template = session.last_ttp_template
        session.records = ({"value": "one"},)
        return AgentRunOutcome(phase_completed=False)

    _install_agent_stubs(monkeypatch, run)
    result = await _generator().generate(
        GenerationRequest(command_outputs=["value: one"]),
    )

    assert result.status == "failed"
    assert result.artifact is None
    assert result.metadata.schema_agent_rounds == 1
    assert result.metadata.ttp_agent_rounds == 1
    assert result.metadata.termination_reason == "agent_stopped"
    assert [issue.code for issue in result.issues] == [
        "generation.agent_stopped",
    ]


async def test_malformed_finish_after_candidate_keeps_invalid_tool_classification(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def run(
        agent: Any,
        message: Any,
        session: Any,
        phase: str,
    ) -> AgentRunOutcome:
        del agent, message
        session.record_agent_round(phase)
        if phase == "schema":
            session.schema_submissions = 1
            session.frozen_schema = _result_schema()
            return AgentRunOutcome(phase_completed=True)

        session.record_agent_round("ttp")
        session.ttp_submissions = 1
        session.last_ttp_template = "value: {{ value }}"
        session.validated_ttp_template = session.last_ttp_template
        session.records = ({"value": "one"},)
        session.submission_tool_call_invalids += 1
        return AgentRunOutcome(
            exceeded_max_iters=True,
            ended_after_invalid_tool_call=True,
            submission_tool_call_invalids=(
                session.submission_tool_call_invalids
            ),
        )

    _install_agent_stubs(monkeypatch, run)
    result = await _generator(max_agent_rounds=3).generate(
        GenerationRequest(command_outputs=["value: one"]),
    )

    assert result.status == "failed"
    assert result.metadata.termination_reason == (
        "model_submission_tool_call_invalid"
    )
    assert [issue.code for issue in result.issues] == [
        "model.submission_tool_call_invalid",
    ]


async def test_valid_candidate_at_submission_limit_still_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def run(
        agent: Any,
        message: Any,
        session: Any,
        phase: str,
    ) -> AgentRunOutcome:
        del agent, message
        session.record_agent_round(phase)
        if phase == "schema":
            session.schema_submissions = 1
            session.frozen_schema = _result_schema()
            return AgentRunOutcome(phase_completed=True)

        session.ttp_submissions = session.max_ttp_submissions
        session.last_ttp_template = "value: {{ value }}"
        session.validated_ttp_template = session.last_ttp_template
        session.records = ({"value": "one"},)
        session.terminal_reason = "ttp_submission_limit"
        return AgentRunOutcome(
            phase_completed=False,
            stopped_after_terminal_tool=True,
        )

    _install_agent_stubs(monkeypatch, run)
    result = await _generator().generate(
        GenerationRequest(command_outputs=["value: one"]),
    )

    assert result.status == "failed"
    assert result.artifact is None
    assert result.metadata.ttp_submissions == 9
    assert result.metadata.termination_reason == "ttp_submission_limit"
    assert [issue.code for issue in result.issues] == [
        "generation.ttp_submission_limit",
    ]


async def test_schema_acceptance_on_last_round_does_not_build_ttp_agent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    built_phases: list[str] = []

    def build(**kwargs: Any) -> Any:
        built_phases.append(kwargs["phase"])
        return SimpleNamespace(phase=kwargs["phase"])

    async def estimate(*_: Any, **__: Any) -> int:
        return 1

    async def run(
        agent: Any,
        message: Any,
        session: Any,
        phase: str,
    ) -> AgentRunOutcome:
        del agent, message
        assert phase == "schema"
        session.record_agent_round("schema")
        session.schema_submissions = 1
        session.frozen_schema = _result_schema()
        session.field_evidence = (
            {"path": "/value", "output_index": 0, "excerpt": "one"},
        )
        return AgentRunOutcome(phase_completed=True)

    monkeypatch.setattr(workflow_module, "build_agent", build)
    monkeypatch.setattr(workflow_module, "estimate_initial_model_tokens", estimate)
    monkeypatch.setattr(workflow_module, "run_generation_phase", run)

    result = await _generator(max_agent_rounds=1).generate(
        GenerationRequest(command_outputs=["value: one"]),
    )

    assert result.status == "failed"
    assert built_phases == ["schema"]
    assert result.metadata.schema_agent_rounds == 1
    assert result.metadata.ttp_agent_rounds == 0
    assert result.metadata.agent_rounds == 1
    assert result.metadata.ttp_sampled_char_count == 0
    assert result.metadata.ttp_submissions == 0
    assert result.metadata.termination_reason == "agent_round_limit"
    assert [issue.code for issue in result.issues] == [
        "generation.agent_round_limit",
    ]
    assert result.issues[0].details == {
        "phase": "schema",
        "blocked_phase": "ttp",
    }


async def test_ttp_context_fit_failure_is_phase_tagged_and_not_counted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    built_phases: list[str] = []
    run_phases: list[str] = []
    span_names: list[str] = []
    fit_calls = 0

    def build(**kwargs: Any) -> Any:
        built_phases.append(kwargs["phase"])
        return SimpleNamespace(phase=kwargs["phase"])

    async def fit(*_: Any, **__: Any) -> tuple[list[Any], bool]:
        nonlocal fit_calls
        fit_calls += 1
        sample = _sample("value: one", original_char_count=len("value: one"))
        return [sample], fit_calls == 1

    async def run(
        agent: Any,
        message: Any,
        session: Any,
        phase: str,
    ) -> AgentRunOutcome:
        del agent, message
        run_phases.append(phase)
        assert phase == "schema"
        session.record_agent_round("schema")
        session.schema_submissions = 1
        session.frozen_schema = _result_schema()
        return AgentRunOutcome(phase_completed=True)

    @contextmanager
    def start(name: str, **_: Any) -> Any:
        span_names.append(name)
        yield SimpleNamespace(enabled=True, creates_trace=name == "ttp.generate")

    monkeypatch.setattr(workflow_module, "build_agent", build)
    monkeypatch.setattr(workflow_module, "_fit_sampled_outputs", fit)
    monkeypatch.setattr(workflow_module, "run_generation_phase", run)
    monkeypatch.setattr(generator_module, "start_laminar_span", start)
    monkeypatch.setattr(generator_module, "finish_laminar_span", lambda **_: None)
    monkeypatch.setattr(workflow_module, "start_laminar_span", start)
    monkeypatch.setattr(workflow_module, "finish_laminar_span", lambda **_: None)

    result = await _generator().generate(
        GenerationRequest(command_outputs=["value: one"]),
    )

    assert result.status == "failed"
    assert built_phases == ["schema", "ttp"]
    assert run_phases == ["schema"]
    assert span_names == ["ttp.generate", "schema.phase", "ttp.phase"]
    assert result.metadata.schema_sampled_char_count == len("value: one")
    assert result.metadata.ttp_sampled_char_count == 0
    assert result.metadata.schema_agent_rounds == 1
    assert result.metadata.ttp_agent_rounds == 0
    assert result.metadata.termination_reason == "model_context_budget"
    assert [issue.code for issue in result.issues] == [
        "model.context_budget_exceeded",
    ]
    assert result.issues[0].details == {"phase": "ttp"}


async def test_phase_cancellation_closes_child_and_root_spans(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    finishes: list[dict[str, Any]] = []
    stack: list[str] = []

    monkeypatch.setattr(
        workflow_module,
        "build_agent",
        lambda **kwargs: SimpleNamespace(phase=kwargs["phase"]),
    )

    async def fit(*_: Any, **__: Any) -> tuple[list[Any], bool]:
        return [_sample("value: one", original_char_count=len("value: one"))], True

    async def cancel(
        agent: Any,
        message: Any,
        session: Any,
        phase: str,
    ) -> AgentRunOutcome:
        del agent, message, session
        assert phase == "schema"
        raise asyncio.CancelledError("private cancellation text")

    @contextmanager
    def start(name: str, **_: Any) -> Any:
        events.append(f"enter:{name}")
        stack.append(name)
        try:
            yield SimpleNamespace(enabled=True, creates_trace=name == "ttp.generate")
        finally:
            events.append(f"exit:{name}")
            assert stack.pop() == name

    def finish(**kwargs: Any) -> None:
        events.append(f"finish:{stack[-1]}")
        finishes.append({"span": stack[-1], **kwargs})

    monkeypatch.setattr(workflow_module, "_fit_sampled_outputs", fit)
    monkeypatch.setattr(workflow_module, "run_generation_phase", cancel)
    monkeypatch.setattr(generator_module, "start_laminar_span", start)
    monkeypatch.setattr(generator_module, "finish_laminar_span", finish)
    monkeypatch.setattr(workflow_module, "start_laminar_span", start)
    monkeypatch.setattr(workflow_module, "finish_laminar_span", finish)

    with pytest.raises(asyncio.CancelledError, match="private cancellation text"):
        await _generator().generate(
            GenerationRequest(command_outputs=["value: one"]),
        )

    assert events == [
        "enter:ttp.generate",
        "enter:schema.phase",
        "finish:schema.phase",
        "exit:schema.phase",
        "finish:ttp.generate",
        "exit:ttp.generate",
    ]
    assert [item["span"] for item in finishes] == [
        "schema.phase",
        "ttp.generate",
    ]
    assert all(item["outcome"] == "cancelled" for item in finishes)
    assert all("private cancellation text" not in str(item) for item in finishes)


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
