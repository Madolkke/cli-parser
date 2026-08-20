from __future__ import annotations

import asyncio
import time
from contextlib import contextmanager, suppress
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from cli_parser_agent import TtpGenerator, TtpGeneratorSettings
from cli_parser_agent.ttp_generation import workflow as workflow_module
from cli_parser_agent.ttp_generation.agent import build_schema_task_prompt
from cli_parser_agent.ttp_generation.workflow import (
    _fit_sampled_outputs,
    _run_before_deadline,
)


def _settings() -> TtpGeneratorSettings:
    return TtpGeneratorSettings(api_key="secret", model_name="test-model")


@pytest.mark.asyncio
async def test_generate_validates_the_request_before_model_construction() -> None:
    generator = TtpGenerator(settings=_settings())

    with pytest.raises(ValidationError):
        await generator.generate({"command_outputs": []})  # type: ignore[arg-type]


def test_constructor_attempts_optional_laminar_initialization(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from cli_parser_agent.ttp_generation import generator as generator_module

    calls: list[object] = []
    monkeypatch.setattr(
        generator_module,
        "initialize_laminar_from_env",
        lambda environ=None: calls.append(environ) or False,
    )

    TtpGenerator(settings=_settings())

    assert calls == [None]


def test_constructor_snapshots_nested_extra_body() -> None:
    extra_body = {"thinking": {"effort": "high"}}
    settings = TtpGeneratorSettings(
        api_key="secret",
        model_name="test-model",
        extra_body=extra_body,
    )

    generator = TtpGenerator(settings=settings)
    extra_body["thinking"]["effort"] = "low"

    assert generator.settings.extra_body == {"thinking": {"effort": "high"}}


def test_from_env_passes_the_same_mapping_to_laminar(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from lmnr import Instruments, Laminar

    environ = {
        "OPENAI_API_KEY": "secret",
        "OPENAI_MODEL": "test-model",
        "LMNR_PROJECT_API_KEY": "trace-key",
        "LMNR_BASE_URL": "https://laminar.example.test",
    }
    initialized = False
    calls: list[dict[str, object]] = []

    def is_initialized() -> bool:
        return initialized

    def initialize(**kwargs: object) -> None:
        nonlocal initialized
        initialized = True
        calls.append(kwargs)

    monkeypatch.setattr(Laminar, "is_initialized", is_initialized)
    monkeypatch.setattr(Laminar, "initialize", initialize)

    generator = TtpGenerator.from_env(environ=environ)

    assert generator.settings.model_name == "test-model"
    assert calls == [
        {
            "project_api_key": "trace-key",
            "base_url": "https://laminar.example.test",
            "instruments": {Instruments.OPENAI},
        },
    ]


def test_from_env_initializes_once_with_the_supplied_mapping(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from cli_parser_agent.ttp_generation import generator as generator_module

    environ = {
        "OPENAI_API_KEY": "secret",
        "OPENAI_MODEL": "test-model",
        "LMNR_PROJECT_API_KEY": "trace-key",
    }
    calls: list[object] = []
    monkeypatch.setattr(
        generator_module,
        "initialize_laminar_from_env",
        lambda supplied=None: calls.append(supplied) or True,
    )

    TtpGenerator.from_env(environ=environ)

    assert calls == [environ]


def test_from_env_validates_model_configuration_before_initializing_laminar(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from cli_parser_agent.ttp_generation import generator as generator_module

    monkeypatch.setattr(
        generator_module,
        "initialize_laminar_from_env",
        lambda _=None: pytest.fail("invalid model config must fail first"),
    )

    with pytest.raises(ValueError):
        TtpGenerator.from_env(
            environ={"LMNR_PROJECT_API_KEY": "trace-key"},
        )


def test_from_env_loads_model_and_generation_budgets() -> None:
    generator = TtpGenerator.from_env(
        environ={
            "OPENAI_API_KEY": "secret",
            "OPENAI_MODEL": "test-model",
            "CLI_PARSER_GENERATION_TIMEOUT_SECONDS": "30",
            "CLI_PARSER_MAX_AGENT_ITERS": "4",
            "CLI_PARSER_MAX_TEMPLATE_SUBMISSIONS": "2",
        },
    )

    assert generator.settings.model_name == "test-model"
    assert generator.policy.total_timeout_seconds == 30
    assert generator.policy.max_agent_rounds == 4
    assert generator.policy.max_ttp_submissions == 2


@pytest.mark.asyncio
async def test_deadline_watchdog_cancels_and_drains_its_child() -> None:
    cleaned_up = asyncio.Event()

    async def operation() -> None:
        try:
            await asyncio.Event().wait()
        finally:
            cleaned_up.set()

    completed, result = await _run_before_deadline(
        operation,
        deadline_monotonic=time.monotonic() + 0.01,
    )

    assert completed is False
    assert result is None
    assert cleaned_up.is_set()


@pytest.mark.asyncio
async def test_deadline_drain_is_bounded_when_the_child_swallows_cancellation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AgentScope converts cancellation into an ordinary INTERRUPTED return.

    An unresponsive phase task must therefore be abandoned after the grace
    period instead of being awaited indefinitely, otherwise the post-deadline
    drain absorbs a whole extra model round.
    """

    monkeypatch.setattr(workflow_module, "_DRAIN_GRACE_SECONDS", 0.06)
    swallowed: list[int] = []
    released = asyncio.Event()

    async def operation() -> None:
        # Absorb every cancellation the drain delivers, then keep running.
        while not released.is_set():
            try:
                await asyncio.sleep(3600)
            except asyncio.CancelledError:
                swallowed.append(1)

    started = time.monotonic()
    completed, result = await _run_before_deadline(
        operation,
        deadline_monotonic=time.monotonic() + 0.01,
    )
    elapsed = time.monotonic() - started

    assert completed is False
    assert result is None
    assert swallowed, "the child must have absorbed at least one cancellation"
    # Without the bound this await would never return on its own.
    assert elapsed < 1.0

    # Let the abandoned task retire so it cannot outlive the event loop.
    released.set()
    await asyncio.sleep(0.05)


@pytest.mark.asyncio
async def test_deadline_drain_reports_whether_the_child_actually_stopped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    finishes: list[dict[str, object]] = []

    @contextmanager
    def start(name: str, **kwargs: object):
        yield SimpleNamespace(enabled=True, creates_trace=False)

    def finish(**kwargs: object) -> None:
        finishes.append(kwargs)

    monkeypatch.setattr(workflow_module, "start_laminar_span", start)
    monkeypatch.setattr(workflow_module, "finish_laminar_span", finish)
    monkeypatch.setattr(workflow_module, "_DRAIN_GRACE_SECONDS", 0.06)

    async def cooperative() -> None:
        await asyncio.Event().wait()

    await _run_before_deadline(
        cooperative,
        deadline_monotonic=time.monotonic() + 0.01,
    )
    assert finishes[-1]["attributes"]["drained"] is True

    released = asyncio.Event()

    async def stubborn() -> None:
        while not released.is_set():
            with suppress(asyncio.CancelledError):
                await asyncio.sleep(3600)

    await _run_before_deadline(
        stubborn,
        deadline_monotonic=time.monotonic() + 0.01,
    )
    assert finishes[-1]["attributes"]["drained"] is False

    released.set()
    await asyncio.sleep(0.05)


@pytest.mark.asyncio
async def test_deadline_cleanup_span_is_emitted_without_operation_content(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    spans: list[dict[str, object]] = []
    finishes: list[dict[str, object]] = []
    stack: list[str] = []

    @contextmanager
    def start(name: str, **kwargs: object):
        spans.append({"name": name, "parent": stack[-1] if stack else None, **kwargs})
        stack.append(name)
        try:
            yield SimpleNamespace(enabled=True, creates_trace=False)
        finally:
            assert stack.pop() == name

    def finish(**kwargs: object) -> None:
        finishes.append({"span": stack[-1], **kwargs})

    async def operation() -> None:
        await asyncio.Event().wait()

    monkeypatch.setattr(workflow_module, "start_laminar_span", start)
    monkeypatch.setattr(workflow_module, "finish_laminar_span", finish)
    completed, result = await _run_before_deadline(
        operation,
        deadline_monotonic=time.monotonic() + 0.01,
        cleanup_attributes={"phase": "schema", "operation": "context_fit"},
    )

    assert completed is False
    assert result is None
    assert spans[0]["name"] == "generation.deadline_cleanup"
    assert spans[0]["attributes"] == {
        "phase": "schema",
        "operation": "context_fit",
        "trigger": "deadline",
        "remaining_seconds": 0.0,
    }
    assert finishes[0]["outcome"] == "failed"
    assert "operation" not in str(finishes[0]["output"])


@pytest.mark.asyncio
async def test_caller_cancellation_is_propagated_after_child_cleanup() -> None:
    entered = asyncio.Event()
    cleaned_up = asyncio.Event()

    async def operation() -> None:
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            cleaned_up.set()

    task = asyncio.create_task(
        _run_before_deadline(
            operation,
            deadline_monotonic=time.monotonic() + 60,
        ),
    )
    await entered.wait()
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task
    assert cleaned_up.is_set()


@pytest.mark.asyncio
async def test_sampling_fits_final_prompt_for_unicode_and_control_text() -> None:
    outputs = ["界\x00" * 2_000, "尾部\n" * 2_000]

    async def estimate_tokens(texts: list[str]) -> int:
        return len(build_schema_task_prompt(texts).encode("utf-8")) // 4

    sampled, fits = await _fit_sampled_outputs(
        outputs,
        total_char_budget=2_000,
        max_initial_tokens=300,
        serialize_prompt=build_schema_task_prompt,
        estimate_tokens=estimate_tokens,
    )

    prompt = build_schema_task_prompt([item.text for item in sampled])
    assert fits
    assert len(prompt) <= 2_000
    assert await estimate_tokens([item.text for item in sampled]) <= 300
    assert sum(item.sampled_char_count for item in sampled) < 2_000


@pytest.mark.asyncio
async def test_sampling_rejects_marker_only_or_missing_input_views() -> None:
    outputs = [f"value: {index}\n" for index in range(5)]

    async def estimate_tokens(texts: list[str]) -> int:
        del texts
        return 1

    sampled, fits = await _fit_sampled_outputs(
        outputs,
        total_char_budget=1,
        max_initial_tokens=10_000,
        serialize_prompt=build_schema_task_prompt,
        estimate_tokens=estimate_tokens,
    )

    assert fits is False
    assert any(not item.text for item in sampled)


def _template_only_workflow(
    schema: dict[str, object],
    *,
    outputs: list[str] | None = None,
) -> workflow_module._GenerationWorkflow:
    from cli_parser_agent.ttp_generation.contracts import GenerationRequest
    from cli_parser_agent.ttp_generation.progress import ProgressEmitter

    return workflow_module._GenerationWorkflow(
        settings=_settings(),
        policy=workflow_module.GenerationPolicy(),
        request=GenerationRequest(command_outputs=outputs or ["value: one"]),
        request_id="request-1",
        progress=ProgressEmitter(request_id="request-1"),
        injected_schema=schema,
    )


def _closed_schema() -> dict[str, object]:
    return {
        "type": "object",
        "properties": {"value": {"type": "string"}},
        "required": ["value"],
        "additionalProperties": False,
    }


def test_injected_schema_is_frozen_without_running_the_schema_phase() -> None:
    schema = _closed_schema()
    workflow = _template_only_workflow(schema)

    assert workflow.session.frozen_schema is None
    assert workflow._freeze_injected_schema(workflow.injected_schema) is None
    assert workflow.session.schema_is_frozen
    assert workflow.session.frozen_schema == schema
    # Defensive copy: later mutation of the caller's dict must not leak in.
    assert workflow.session.frozen_schema is not schema
    # The Schema phase never ran, so its counters stay at zero.
    assert workflow.session.schema_agent_rounds == 0
    assert workflow.session.schema_submissions == 0


def test_injected_schema_still_enforces_the_closed_subset() -> None:
    # additionalProperties is absent, so the object is open.
    workflow = _template_only_workflow(
        {"type": "object", "properties": {"value": {"type": "string"}}},
    )

    result = workflow._freeze_injected_schema(workflow.injected_schema)

    assert result is not None
    assert result.status == "failed"
    assert result.metadata.termination_reason == "invalid_injected_schema"
    assert [issue.code for issue in result.issues] == ["schema.object_not_closed"]
    assert workflow.session.frozen_schema is None


def test_template_only_acceptance_does_not_require_field_evidence() -> None:
    """Regression guard for the one deliberate validation difference.

    A caller-supplied schema has no per-leaf evidence, so acceptance must run
    the schema-only check.  Restoring ``validate_schema_proposal`` here would
    fail every leaf with ``schema.evidence_missing``.
    """

    workflow = _template_only_workflow(_closed_schema())
    assert workflow._freeze_injected_schema(workflow.injected_schema) is None
    assert workflow.session.field_evidence == ()

    assert workflow._validate_frozen_schema() == []


def test_metadata_round_counts_stay_consistent_without_a_schema_phase() -> None:
    workflow = _template_only_workflow(_closed_schema())
    workflow._freeze_injected_schema(workflow.injected_schema)
    workflow.session.record_agent_round("ttp")
    workflow.session.record_agent_round("ttp")

    metadata = workflow._metadata("success")

    assert metadata.schema_agent_rounds == 0
    assert metadata.ttp_agent_rounds == 2
    assert metadata.agent_rounds == 2
    assert metadata.schema_sampled_char_count == 0
