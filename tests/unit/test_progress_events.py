from __future__ import annotations

import asyncio
import json
from copy import deepcopy
from typing import Any

import pytest
from agentscope.agent import Agent, ReActConfig
from agentscope.event import (
    CustomEvent,
    ExceedMaxItersEvent,
    ModelCallStartEvent,
    TextBlockDeltaEvent,
    ThinkingBlockDeltaEvent,
)
from agentscope.message import (
    TextBlock,
    ThinkingBlock,
    ToolCallBlock,
    UserMsg,
)
from agentscope.model import ChatResponse, ChatUsage
from agentscope.state import AgentState
from agentscope.tool import Toolkit

from cli_parser_agent import (
    ArtifactBundle,
    GenerationMetadata,
    GenerationPolicy,
    GenerationRequest,
    GenerationResult,
    ProgressObserver,
    TtpGenerator,
    TtpGeneratorSettings,
)
from cli_parser_agent.ttp_generation import workflow as workflow_module
from cli_parser_agent.ttp_generation.agent import AgentRunOutcome
from cli_parser_agent.ttp_generation.agent import runner as runner_module
from cli_parser_agent.ttp_generation.agent.builder import build_agent
from cli_parser_agent.ttp_generation.agent.runner import run_generation_phase
from cli_parser_agent.ttp_generation.agent.session import (
    GenerationSession,
    ValidatorOutcome,
)
from cli_parser_agent.ttp_generation.agent.tools import (
    SUBMIT_SCHEMA_TOOL_NAME,
    build_submission_tools,
)
from cli_parser_agent.ttp_generation.progress import ProgressEmitter


def _schema() -> dict[str, Any]:
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "type": "object",
        "properties": {"value": {"type": "string"}},
        "required": ["value"],
        "additionalProperties": False,
    }


def _settings() -> TtpGeneratorSettings:
    return TtpGeneratorSettings(api_key="secret-key", model_name="test-model")


def _success_result(request_id: str) -> GenerationResult:
    return GenerationResult(
        status="success",
        artifact=ArtifactBundle(
            ttp_template="value: {{ value }}",
            result_schema=_schema(),
            records=[{"value": "one"}],
            assumptions=[],
        ),
        metadata=GenerationMetadata(
            request_id=request_id,
            model_name="test-model",
            command_output_count=1,
            termination_reason="success",
        ),
    )


def test_progress_emitter_copies_events_and_stamps_metadata() -> None:
    observed: list[Any] = []
    emitter = ProgressEmitter(request_id="request-1", observer=observed.append)
    original = TextBlockDeltaEvent(
        reply_id="reply-1",
        block_id="block-1",
        delta="secret output",
        metadata={"provider": "openai-compatible"},
    )

    emitter.emit(original, phase="schema", sensitive=True)
    emitter.custom(
        "cli_parser.no_tool.retry",
        {"retry_number": 1, "max_retries": 3},
        phase="schema",
        sensitive=False,
    )

    assert original.metadata == {"provider": "openai-compatible"}
    assert observed[0] is not original
    assert observed[0].delta == "secret output"
    assert [event.metadata["sequence"] for event in observed] == [1, 2]
    for event in observed:
        assert event.metadata["request_id"] == "request-1"
        assert event.metadata["elapsed_seconds"] >= 0
        assert event.metadata["phase"] == "schema"
    assert observed[0].metadata["sensitive"] is True
    assert observed[1].metadata["sensitive"] is False


def test_progress_emitter_disables_when_event_copy_fails() -> None:
    class Uncopyable:
        def __deepcopy__(self, memo: object) -> object:
            del memo
            raise RuntimeError("copy details must stay private")

    observed: list[Any] = []
    emitter = ProgressEmitter(request_id="request-1", observer=observed.append)
    event = TextBlockDeltaEvent(
        reply_id="reply-1",
        block_id="block-1",
        delta="value",
        metadata={"opaque": Uncopyable()},
    )

    emitter.emit(event, phase="schema", sensitive=True)

    assert observed == []
    assert not emitter.enabled


def test_progress_emitter_disables_a_failing_observer() -> None:
    calls = 0

    def broken(_: Any) -> None:
        nonlocal calls
        calls += 1
        raise asyncio.CancelledError("observer must not stop generation")

    emitter = ProgressEmitter(request_id="request-1", observer=broken)
    emitter.custom(
        "cli_parser.generation.started",
        {},
        phase="generation",
        sensitive=False,
    )
    emitter.custom(
        "cli_parser.generation.completed",
        {},
        phase="generation",
        sensitive=False,
    )

    assert calls == 1
    assert not emitter.enabled


def test_progress_emitters_keep_concurrent_request_sequences_isolated() -> None:
    first: list[Any] = []
    second: list[Any] = []
    emitters = (
        ProgressEmitter(request_id="first", observer=first.append),
        ProgressEmitter(request_id="second", observer=second.append),
    )

    for emitter in emitters:
        emitter.custom(
            "cli_parser.phase.started",
            {},
            phase="schema",
            sensitive=False,
        )

    assert first[0].metadata["request_id"] == "first"
    assert second[0].metadata["request_id"] == "second"
    assert first[0].metadata["sequence"] == 1
    assert second[0].metadata["sequence"] == 1


async def test_generate_emits_request_lifecycle_and_accepts_keyword_observer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    generator = TtpGenerator(settings=_settings())
    observed: list[Any] = []

    async def complete(
        request: GenerationRequest,
        *,
        request_id: str,
        progress: ProgressEmitter,
    ) -> GenerationResult:
        assert progress.request_id == request_id
        assert request.command_outputs == ["value: one"]
        return _success_result(request_id)

    monkeypatch.setattr(generator, "_generate", complete)
    observer: ProgressObserver = observed.append
    result = await generator.generate(
        GenerationRequest(command_outputs=["value: one"]),
        observer=observer,
    )

    assert result.status == "success"
    lifecycle = [event for event in observed if isinstance(event, CustomEvent)]
    assert [event.name for event in lifecycle] == [
        "cli_parser.generation.started",
        "cli_parser.generation.completed",
    ]
    assert lifecycle[0].value["request"]["command_outputs"] == ["value: one"]
    assert lifecycle[1].value["result"]["status"] == "success"
    assert {
        event.metadata["request_id"] for event in lifecycle
    } == {result.metadata.request_id}


async def test_generate_emits_cancelled_without_exception_event(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    generator = TtpGenerator(settings=_settings())
    observed: list[Any] = []

    async def cancel(
        request: GenerationRequest,
        *,
        request_id: str,
        progress: ProgressEmitter,
    ) -> GenerationResult:
        del request, request_id, progress
        raise asyncio.CancelledError

    monkeypatch.setattr(generator, "_generate", cancel)
    with pytest.raises(asyncio.CancelledError):
        await generator.generate(
            GenerationRequest(command_outputs=["value: one"]),
            observer=observed.append,
        )

    names = [event.name for event in observed if isinstance(event, CustomEvent)]
    assert names == [
        "cli_parser.generation.started",
        "cli_parser.generation.cancelled",
    ]


async def test_generate_exception_event_records_only_the_exception_type(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    generator = TtpGenerator(settings=_settings())
    observed: list[Any] = []

    async def fail(
        request: GenerationRequest,
        *,
        request_id: str,
        progress: ProgressEmitter,
    ) -> GenerationResult:
        del request, request_id, progress
        raise RuntimeError("secret exception body")

    monkeypatch.setattr(generator, "_generate", fail)
    with pytest.raises(RuntimeError, match="secret exception body"):
        await generator.generate(
            GenerationRequest(command_outputs=["value: one"]),
            observer=observed.append,
        )

    event = observed[-1]
    assert isinstance(event, CustomEvent)
    assert event.name == "cli_parser.generation.exception"
    assert event.value == {"status": "failed", "exception_type": "RuntimeError"}
    assert "secret exception body" not in event.model_dump_json()


def _response(*blocks: Any) -> ChatResponse:
    return ChatResponse(
        content=list(blocks),
        is_last=True,
        usage=ChatUsage(input_tokens=11, output_tokens=7, time=0.01),
    )


class _ScriptedModel:
    model = "scripted-model"
    context_size = 128_000

    def __init__(self, responses: list[ChatResponse]) -> None:
        self.responses = list(responses)
        self.calls: list[dict[str, Any]] = []

    async def __call__(
        self,
        messages: list[Any],
        tools: list[dict[str, Any]] | None = None,
        tool_choice: Any = None,
    ) -> ChatResponse:
        self.calls.append(
            {
                "messages": deepcopy(messages),
                "tools": deepcopy(tools),
                "tool_choice": tool_choice,
            },
        )
        return self.responses.pop(0)

    async def count_tokens(
        self,
        messages: list[Any],
        tools: list[dict[str, Any]] | None = None,
    ) -> int:
        del messages, tools
        return 1


async def test_runner_forwards_model_events_and_emits_debug_context() -> None:
    secret = "discarded-secret"
    schema_call = ToolCallBlock(
        id="schema-call",
        name=SUBMIT_SCHEMA_TOOL_NAME,
        input=json.dumps(
            {
                "result_schema": _schema(),
                "evidence": [
                    {
                        "path": "/value",
                        "output_index": 0,
                        "excerpt": "one",
                    },
                ],
                "assumptions": [],
            },
        ),
    )
    model = _ScriptedModel(
        [
            _response(
                ThinkingBlock(thinking=f"thinking {secret}"),
                TextBlock(text=f"text {secret}"),
            ),
            _response(schema_call),
        ],
    )
    session = GenerationSession(
        command_outputs=("value: one",),
        schema_validator=lambda _: ValidatorOutcome(valid=True),
        template_validator=lambda _: ValidatorOutcome(valid=False),
    )
    observed: list[Any] = []
    progress = ProgressEmitter(request_id="runner-request", observer=observed.append)
    agent = Agent(
        name="schema_generator",
        system_prompt="schema system prompt",
        model=model,  # type: ignore[arg-type]
        toolkit=Toolkit(
            tools=build_submission_tools(
                session,
                "schema",
                progress=progress,
            ),
        ),
        state=AgentState(),
        react_config=ReActConfig(
            max_iters=session.max_agent_rounds,
            interruption_raise_cancelled_error=True,
        ),
    )

    outcome = await run_generation_phase(
        agent,
        UserMsg(name="user", content="value: one"),
        session,
        "schema",
        progress=progress,
    )

    assert outcome.phase_completed
    assert not any(isinstance(event, ExceedMaxItersEvent) for event in observed)
    assert any(isinstance(event, ModelCallStartEvent) for event in observed)
    assert any(isinstance(event, ThinkingBlockDeltaEvent) for event in observed)
    assert any(isinstance(event, TextBlockDeltaEvent) for event in observed)
    custom = [event for event in observed if isinstance(event, CustomEvent)]
    names = [event.name for event in custom]
    assert names.count("cli_parser.model.context_snapshot") == 2
    assert "cli_parser.model.output_discarded" in names
    assert "cli_parser.no_tool.retry" in names
    assert "cli_parser.tool.result" in names

    snapshots = [
        event.value
        for event in custom
        if event.name == "cli_parser.model.context_snapshot"
    ]
    assert snapshots[0]["system_prompt"] == "schema system prompt"
    assert snapshots[0]["tool_schemas"][0]["function"]["name"] == (
        SUBMIT_SCHEMA_TOOL_NAME
    )
    assert secret not in json.dumps(snapshots[1], ensure_ascii=False)

    tool_result = next(
        event.value
        for event in custom
        if event.name == "cli_parser.tool.result"
    )
    assert tool_result["tool_name"] == SUBMIT_SCHEMA_TOOL_NAME
    assert tool_result["output"]["accepted"] is True
    assert tool_result["output"]["schema_submission"] == 1
    assert [event.metadata["sequence"] for event in observed] == list(
        range(1, len(observed) + 1),
    )
    assert all(event.metadata["phase"] == "schema" for event in observed)


async def test_real_agent_context_snapshot_excludes_model_api_key() -> None:
    session = GenerationSession(
        command_outputs=("value: one",),
        schema_validator=lambda _: ValidatorOutcome(valid=False),
        template_validator=lambda _: ValidatorOutcome(valid=False),
    )
    observed: list[Any] = []
    progress = ProgressEmitter(request_id="safe-context", observer=observed.append)
    agent = build_agent(
        settings=_settings(),
        policy=GenerationPolicy(),
        session=session,
        phase="schema",
        progress=progress,
    )
    agent.state.context.append(UserMsg(name="user", content="value: one"))

    await runner_module._emit_context_snapshot(
        progress,
        agent,
        ModelCallStartEvent(reply_id="reply", model_name="test-model"),
        "schema",
    )

    serialized = json.dumps(observed[0].model_dump(mode="json"))
    assert "secret-key" not in serialized
    assert observed[0].value["model"]["parameters"][
        "parallel_tool_calls"
    ] is False
    assert observed[0].value["tool_schemas"][0]["function"]["name"] == (
        SUBMIT_SCHEMA_TOOL_NAME
    )


async def test_workflow_emits_phase_sampling_and_final_validation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    generator = TtpGenerator(settings=_settings())
    observed: list[Any] = []

    monkeypatch.setattr(
        workflow_module,
        "build_agent",
        lambda **kwargs: type("AgentStub", (), {"phase": kwargs["phase"]})(),
    )

    async def estimate(*_: Any, **__: Any) -> int:
        return 1

    async def run(
        agent: Any,
        message: Any,
        session: GenerationSession,
        phase: str,
        *,
        progress: ProgressEmitter,
    ) -> AgentRunOutcome:
        del agent, message
        assert progress.enabled
        session.record_agent_round(phase)  # type: ignore[arg-type]
        if phase == "schema":
            session.schema_submissions = 1
            session.frozen_schema = _schema()
            session.field_evidence = (
                {"path": "/value", "output_index": 0, "excerpt": "one"},
            )
            return AgentRunOutcome(phase_completed=True)
        session.ttp_submissions = 1
        session.validated_ttp_template = "value: {{ value }}"
        session.records = ({"value": "one"},)
        session.generation_finished = True
        session.terminal_reason = "success"
        return AgentRunOutcome(phase_completed=True)

    monkeypatch.setattr(workflow_module, "estimate_initial_model_tokens", estimate)
    monkeypatch.setattr(workflow_module, "run_generation_phase", run)
    monkeypatch.setattr(
        workflow_module,
        "validate_schema_proposal",
        lambda *_args, **_kwargs: [],
    )
    monkeypatch.setattr(
        workflow_module,
        "validate_ttp_template",
        lambda *_args, **_kwargs: type(
            "Acceptance",
            (),
            {"valid": True, "issues": (), "records": [{"value": "one"}]},
        )(),
    )

    result = await generator.generate(
        GenerationRequest(command_outputs=["value: one"]),
        observer=observed.append,
    )

    assert result.status == "success"
    custom = [event for event in observed if isinstance(event, CustomEvent)]
    names = [event.name for event in custom]
    assert names == [
        "cli_parser.generation.started",
        "cli_parser.phase.started",
        "cli_parser.phase.sampling_completed",
        "cli_parser.phase.input_prepared",
        "cli_parser.phase.completed",
        "cli_parser.phase.started",
        "cli_parser.phase.sampling_completed",
        "cli_parser.phase.input_prepared",
        "cli_parser.phase.completed",
        "cli_parser.final_validation.started",
        "cli_parser.final_validation.completed",
        "cli_parser.generation.completed",
    ]
    sampling = [
        event
        for event in custom
        if event.name == "cli_parser.phase.sampling_completed"
    ]
    assert [event.metadata["phase"] for event in sampling] == ["schema", "ttp"]
    assert sampling[0].value["sampled_outputs"][0]["text"] == "value: one"
    final = next(
        event
        for event in custom
        if event.name == "cli_parser.final_validation.completed"
    )
    assert final.value == {
        "status": "success",
        "valid": True,
        "issues": [],
        "records": [{"value": "one"}],
    }
    assert final.metadata["phase"] == "acceptance"
    assert "secret-key" not in json.dumps(
        [event.model_dump(mode="json") for event in observed],
    )
