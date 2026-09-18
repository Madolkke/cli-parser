"""Event-aware execution of the request-local AgentScope loop."""

from __future__ import annotations

import asyncio
import time
from copy import deepcopy
from dataclasses import dataclass
from typing import Any

from agentscope.event import (
    DataBlockEndEvent,
    DataBlockStartEvent,
    ExceedMaxItersEvent,
    ModelCallEndEvent,
    ModelCallStartEvent,
    ReplyEndEvent,
    ReplyStartEvent,
    TextBlockDeltaEvent,
    TextBlockEndEvent,
    TextBlockStartEvent,
    ThinkingBlockDeltaEvent,
    ThinkingBlockEndEvent,
    ThinkingBlockStartEvent,
    ToolCallDeltaEvent,
    ToolCallEndEvent,
    ToolCallStartEvent,
    ToolResultEndEvent,
    ToolResultStartEvent,
)
from agentscope.message import (
    AssistantMsg,
    TextBlock,
    ThinkingBlock,
    ToolCallBlock,
    ToolResultBlock,
    ToolResultState,
    UserMsg,
)
from agentscope.model import FinishedReason

from ...observability import finish_laminar_span, start_laminar_span
from ..progress import ProgressEmitter
from .prompt import (
    SCHEMA_NO_TOOL_RETRY_PROMPT,
    TTP_NO_TOOL_RETRY_PROMPT,
)
from .protocol import (
    MAX_CONSECUTIVE_PROTOCOL_FAILURES,
    SCHEMA_PROTOCOL_TOOLS,
    TTP_PROTOCOL_TOOLS,
    ProtocolBoundaryTracker,
    ProtocolFailure,
    protocol_repair_text,
    track_tool_boundaries,
)
from .session import GenerationPhase, GenerationSession
from .tools import (
    FINISH_GENERATION_TOOL_NAME,
    SUBMIT_SCHEMA_TOOL_NAME,
    SUBMIT_TEMPLATE_TOOL_NAME,
    TEST_TEMPLATE_TOOL_NAME,
)

_NON_SENSITIVE_EVENT_TYPES = (
    ReplyStartEvent,
    ReplyEndEvent,
    ModelCallStartEvent,
    ModelCallEndEvent,
    TextBlockStartEvent,
    TextBlockEndEvent,
    ThinkingBlockStartEvent,
    ThinkingBlockEndEvent,
    DataBlockStartEvent,
    DataBlockEndEvent,
    ToolCallStartEvent,
    ToolCallEndEvent,
    ToolResultStartEvent,
    ToolResultEndEvent,
    ExceedMaxItersEvent,
)


@dataclass(frozen=True, slots=True)
class AgentRunOutcome:
    """Framework-neutral facts observed during one generation phase."""

    phase_completed: bool = False
    exceeded_max_iters: bool = False
    stopped_after_terminal_tool: bool = False
    model_no_tool_retry_limit: bool = False
    protocol_retry_limit: bool = False
    ended_after_invalid_tool_call: bool = False
    tool_call_starts: int = 0
    tool_result_errors: int = 0
    submission_tool_call_invalids: int = 0


@dataclass(frozen=True, slots=True)
class _ContextCheckpoint:
    """Minimal state needed to remove one untrusted free-text completion."""

    context_length: int
    last_message: Any | None


def _checkpoint_context(agent: Any) -> _ContextCheckpoint:
    context = agent.state.context
    return _ContextCheckpoint(
        context_length=len(context),
        last_message=deepcopy(context[-1]) if context else None,
    )


def _restore_context(agent: Any, checkpoint: _ContextCheckpoint) -> None:
    """Restore context and usage to the point before one model call."""

    context = agent.state.context
    if len(context) < checkpoint.context_length:
        raise RuntimeError("Agent context shrank after the model call.")
    del context[checkpoint.context_length :]
    if checkpoint.context_length:
        context[-1] = deepcopy(checkpoint.last_message)


def _reasoning_delta(
    agent: Any, checkpoint: _ContextCheckpoint, round_index: int
) -> AssistantMsg | None:
    """Copy only this request's completed, pure-reasoning length response."""

    accepts = getattr(agent.model, "is_completed_reasoning_only_length", None)
    if not callable(accepts) or not accepts(round_index):
        return None
    context = agent.state.context
    if len(context) < checkpoint.context_length:
        return None
    blocks = []
    if checkpoint.last_message is not None:
        current = context[checkpoint.context_length - 1]
        original = checkpoint.last_message
        before = original.get_content_blocks()
        after = current.get_content_blocks()
        if (
            current.id != original.id
            or current.role != original.role
            or current.name != original.name
            or after[: len(before)] != before
        ):
            return None
        added = after[len(before) :]
        if added and (current.role != "assistant" or current.name != agent.name):
            return None
        blocks.extend(added)
    for message in context[checkpoint.context_length :]:
        if message.role != "assistant" or message.name != agent.name:
            return None
        blocks.extend(message.get_content_blocks())
    if not blocks or not all(isinstance(block, ThinkingBlock) for block in blocks):
        return None
    if not any(block.thinking for block in blocks):
        return None
    return AssistantMsg(name=agent.name, content=deepcopy(blocks))


_SUPERSEDED_TTP_RESULT = "该次提交的匹配结果已被后续提交取代"


def _compact_ttp_history(
    agent: Any,
    session: GenerationSession,
    *,
    progress: ProgressEmitter | None = None,
) -> None:
    """Collapse only superseded submission results, keeping all test evidence."""

    if session.ttp_submissions < 2:
        return
    context = agent.state.context
    target_names = {SUBMIT_TEMPLATE_TOOL_NAME, TEST_TEMPLATE_TOOL_NAME}
    try:
        calls: dict[str, ToolCallBlock] = {}
        results: dict[str, tuple[int, ToolResultBlock]] = {}
        for message_index, message in enumerate(context):
            for block in getattr(message, "content", ()):
                if isinstance(block, ToolCallBlock) and block.name in target_names:
                    if block.id in calls:
                        session.ttp_history_compaction_skips += 1
                        return
                    calls[block.id] = block
                elif isinstance(block, ToolResultBlock) and block.name in target_names:
                    if block.id in results:
                        session.ttp_history_compaction_skips += 1
                        return
                    results[block.id] = (message_index, block)

        # Wait for complete, unambiguous call/result pairs before rewriting.
        if set(calls) != set(results) or any(
            call.name != results[call_id][1].name for call_id, call in calls.items()
        ):
            session.ttp_history_compaction_skips += 1
            return
        submission_ids = [
            call_id
            for call_id, call in calls.items()
            if call.name == SUBMIT_TEMPLATE_TOOL_NAME
        ]
        if len(submission_ids) < 2:
            return

        replacements: dict[int, dict[str, ToolResultBlock]] = {}
        result_chars = 0
        compacted = 0
        for call_id in submission_ids[:-1]:
            message_index, result = results[call_id]
            if isinstance(result.output, str):
                text = result.output
                output = _SUPERSEDED_TTP_RESULT
            elif isinstance(result.output, list) and all(
                isinstance(block, TextBlock) for block in result.output
            ):
                text = "".join(block.text for block in result.output)
                output = [TextBlock(text=_SUPERSEDED_TTP_RESULT)]
            else:
                session.ttp_history_compaction_skips += 1
                return
            if text == _SUPERSEDED_TTP_RESULT:
                continue
            replacements.setdefault(message_index, {})[call_id] = result.model_copy(
                update={"output": output},
            )
            result_chars += len(text)
            compacted += 1
        if not compacted:
            return

        updated_messages = {}
        for message_index, replacement_by_id in replacements.items():
            message = context[message_index]
            updated_messages[message_index] = message.model_copy(
                update={
                    "content": [
                        replacement_by_id.get(block.id, block)
                        if isinstance(block, ToolResultBlock)
                        else block
                        for block in message.content
                    ],
                },
            )
        for message_index, message in updated_messages.items():
            context[message_index] = message

        session.ttp_history_compaction_events += 1
        session.ttp_history_compacted_interactions += compacted
        session.ttp_history_compacted_result_chars += result_chars
        if progress is not None and progress.enabled:
            progress.custom(
                "cli_parser.ttp.history_compacted",
                {
                    "compacted_interactions": compacted,
                    "retained_interactions": 1
                    + sum(
                        call.name == TEST_TEMPLATE_TOOL_NAME for call in calls.values()
                    ),
                    "compacted_input_chars": 0,
                    "compacted_result_chars": result_chars,
                },
                phase="ttp",
                sensitive=False,
            )
    except Exception:
        session.ttp_history_compaction_skips += 1


def _retry_message(
    phase: GenerationPhase, expected_tools: tuple[str, ...] | None = None
) -> UserMsg:
    if phase == "schema":
        content = (
            protocol_repair_text("no_tool", expected_tools)
            if expected_tools and SUBMIT_SCHEMA_TOOL_NAME not in expected_tools
            else SCHEMA_NO_TOOL_RETRY_PROMPT
        )
    elif phase == "ttp":
        content = TTP_NO_TOOL_RETRY_PROMPT
    else:
        raise RuntimeError("No retry prompt exists for the current phase.")
    return UserMsg(name="user", content=content)


def _expected_tool_names(phase: GenerationPhase) -> tuple[str, ...]:
    if phase == "schema":
        return (SUBMIT_SCHEMA_TOOL_NAME,)
    if phase == "ttp":
        return (
            SUBMIT_TEMPLATE_TOOL_NAME,
            TEST_TEMPLATE_TOOL_NAME,
            FINISH_GENERATION_TOOL_NAME,
        )
    raise ValueError(f"Unsupported generation phase: {phase!r}")


async def _registered_phase_tools(
    agent: Any, phase: GenerationPhase
) -> tuple[str, ...]:
    toolkit = getattr(agent, "toolkit", None)
    if toolkit is None:
        return _expected_tool_names(phase)
    allowed = SCHEMA_PROTOCOL_TOOLS if phase == "schema" else TTP_PROTOCOL_TOOLS
    schemas = await toolkit.get_tool_schemas()
    return tuple(
        name
        for schema in schemas
        if (name := schema.get("function", {}).get("name")) in allowed
    )


def _phase_completed(session: GenerationSession, phase: GenerationPhase) -> bool:
    if phase == "schema":
        return session.schema_is_frozen
    return session.succeeded


def _terminal_tool_observed(
    session: GenerationSession,
    phase: GenerationPhase,
) -> bool:
    """Return whether the current reply must stop after a tool result."""

    if _phase_completed(session, phase):
        return True
    if session.terminal_reason == "generation_timeout":
        return True
    return phase == "ttp" and (
        session.terminal_reason == "ttp_worker_unavailable"
        or session.ttp_submissions >= session.max_ttp_submissions
    )


def _jsonable(value: Any) -> Any:
    """Serialize known AgentScope state without falling back to object repr."""

    model_dump = getattr(value, "model_dump", None)
    if callable(model_dump):
        return model_dump(mode="json")
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return {"type": type(value).__name__}


def _safe_model_config(agent: Any) -> dict[str, Any]:
    """Return only non-credential model settings used by this request."""

    model = agent.model
    output: dict[str, Any] = {
        "model_name": str(getattr(model, "model", "")),
    }
    for name in ("stream", "max_retries", "context_size"):
        value = getattr(model, name, None)
        if value is not None:
            output[name] = _jsonable(value)
    parameters = getattr(model, "parameters", None)
    if parameters is not None:
        output["parameters"] = _jsonable(parameters)
    return output


async def _emit_context_snapshot(
    progress: ProgressEmitter,
    agent: Any,
    event: ModelCallStartEvent,
    phase: GenerationPhase,
) -> None:
    """Capture formatter-preparation inputs without exposing credentials."""

    if not progress.enabled:
        return
    try:
        tool_schemas = await agent.toolkit.get_tool_schemas()
        value = {
            "reply_id": event.reply_id,
            "model_call_event_id": event.id,
            "system_prompt": _jsonable(getattr(agent, "_system_prompt", "")),
            "context": _jsonable(agent.state.context),
            "tool_schemas": _jsonable(tool_schemas),
            "model": _safe_model_config(agent),
        }
    except Exception as error:
        value = {
            "reply_id": event.reply_id,
            "model_call_event_id": event.id,
            "available": False,
            "exception_type": type(error).__name__,
        }
    progress.custom(
        "cli_parser.model.context_snapshot",
        value,
        phase=phase,
        sensitive=True,
    )


async def run_generation_phase(
    agent: Any,
    message: Any,
    session: GenerationSession,
    phase: GenerationPhase,
    *,
    progress: ProgressEmitter | None = None,
) -> AgentRunOutcome:
    """Run one isolated phase with a request-local protocol boundary tracker."""

    tracker = ProtocolBoundaryTracker()
    with track_tool_boundaries(tracker):
        return await _run_generation_phase(
            agent, message, session, phase, tracker=tracker, progress=progress
        )


async def _run_generation_phase(
    agent: Any,
    message: Any,
    session: GenerationSession,
    phase: GenerationPhase,
    *,
    tracker: ProtocolBoundaryTracker,
    progress: ProgressEmitter | None = None,
) -> AgentRunOutcome:
    """Repair protocol failures without retaining rejected calls or outputs."""

    expected_tools = await _registered_phase_tools(agent, phase)
    exceeded_max_iters = False
    stopped_after_terminal_tool = False
    model_no_tool_retry_limit = False
    protocol_retry_limit = False
    consecutive_protocol_failures = 0
    last_model_call_invalid = False
    last_model_call_event_id: str | None = None
    last_model_call_reply_id: str | None = None
    next_message = message
    round_span_manager: Any | None = None
    round_span_attributes: dict[str, Any] = {}
    round_tool_names: set[str] = set()
    round_finished_reason: str = ""
    round_outcome: str = "success"

    def record_protocol_failure(failure: ProtocolFailure) -> bool:
        nonlocal consecutive_protocol_failures, protocol_retry_limit
        consecutive_protocol_failures += 1
        protocol_retry_limit = (
            consecutive_protocol_failures >= MAX_CONSECUTIVE_PROTOCOL_FAILURES
        )
        if progress is not None:
            progress.custom(
                "cli_parser.protocol.repair",
                {
                    "category": failure,
                    "consecutive_failures": consecutive_protocol_failures,
                    "repair_limit": MAX_CONSECUTIVE_PROTOCOL_FAILURES - 1,
                    "stopped": protocol_retry_limit,
                    "termination_reason": (
                        "protocol_retry_limit" if protocol_retry_limit else None
                    ),
                },
                phase=phase,
                sensitive=False,
            )
        return not protocol_retry_limit

    def observe_boundary(category: str) -> None:
        if progress is not None:
            progress.custom(
                "cli_parser.protocol.boundary",
                {"category": category},
                phase=phase,
                sensitive=False,
            )

    def remaining_seconds() -> float:
        return session.remaining_seconds()

    def raise_pending_external_cancellation(
        internal_task: asyncio.Task[Any] | None = None,
    ) -> None:
        # AgentScope may consume CancelledError into an ordinary reply before
        # yielding its next event. The task's outstanding cancellation still
        # forbids tool execution or another provider request. Our own terminal
        # tool cancellation is accounted for separately and must keep its
        # existing token-aware cleanup path.
        task = asyncio.current_task()
        if task is not None and task.cancelling() > int(task is internal_task):
            raise asyncio.CancelledError()

    def stop_for_deadline() -> bool:
        """Refuse a new round that cannot finish before the shared deadline."""

        if session.has_time_for_another_round():
            return False
        session.terminal_reason = "generation_timeout"
        if progress is not None:
            progress.custom(
                "cli_parser.round.skipped",
                {
                    "reason": "insufficient_remaining_time",
                    "remaining_seconds": remaining_seconds(),
                },
                phase=phase,
                sensitive=False,
            )
        return True

    def close_round() -> None:
        nonlocal round_span_manager
        if round_span_manager is None:
            return
        finish_laminar_span(
            output={
                "status": round_outcome,
                "phase": phase,
                "tool_call": bool(round_tool_names),
            },
            outcome=round_outcome,  # type: ignore[arg-type]
            attributes={
                **round_span_attributes,
                "tool_call": bool(round_tool_names),
                "tool_names": ",".join(sorted(round_tool_names)),
                "finished_reason": round_finished_reason,
                "schema_no_tool_retries": session.schema_no_tool_retries,
                "ttp_no_tool_retries": session.ttp_no_tool_retries,
                "remaining_rounds": max(
                    0,
                    session.max_agent_rounds - session.agent_rounds,
                ),
                "remaining_seconds": remaining_seconds(),
                "stream_enabled": session.stream_enabled,
                "stream_chunk_count": session._model_call_chunks,
                "stream_tool_call_delta_count": session._model_call_tool_deltas,
            },
        )
        round_span_manager.__exit__(None, None, None)
        round_span_manager = None

    if _phase_completed(session, phase):
        return AgentRunOutcome(
            phase_completed=True,
            tool_call_starts=session.tool_call_starts,
            tool_result_errors=session.tool_result_errors,
            submission_tool_call_invalids=session.submission_tool_call_invalids,
        )

    while session.agent_rounds < session.max_agent_rounds:
        raise_pending_external_cancellation()
        if stop_for_deadline():
            break
        remaining_rounds = session.max_agent_rounds - session.agent_rounds
        agent.react_config.max_iters = remaining_rounds

        last_checkpoint: _ContextCheckpoint | None = None
        last_expected_tools: tuple[str, ...] | None = None
        last_call_had_tool = False
        last_call_interrupted = False
        pending_tool_calls: dict[str, str] = {}
        terminal_checkpoint: _ContextCheckpoint | None = None
        internal_cancel_task: asyncio.Task[Any] | None = None
        internal_cancel_token: object | None = None
        original_reply_max_iters = agent.react_config.max_iters

        stream = agent.reply_stream(next_message)
        try:
            async for event in stream:
                raise_pending_external_cancellation(internal_cancel_task)
                if stopped_after_terminal_tool:
                    # Cancellation can arrive just after AgentScope has
                    # yielded the next model-call marker. That marker is
                    # before the provider await, so closing here prevents an
                    # unnecessary model request without consuming another
                    # scripted/API response.
                    if isinstance(event, ModelCallStartEvent):
                        break
                    # AgentScope emits this event before logging its ordinary
                    # max-iteration warning. Stop at this safe suspension
                    # point; the reply's cleanup has no terminal event to
                    # yield during async-generator finalization.
                    if isinstance(event, ExceedMaxItersEvent):
                        break
                    continue

                if progress is not None:
                    progress.emit(
                        event,
                        phase=phase,
                        sensitive=not isinstance(
                            event,
                            _NON_SENSITIVE_EVENT_TYPES,
                        ),
                    )

                if session.stream_enabled and isinstance(
                    event,
                    TextBlockDeltaEvent | ThinkingBlockDeltaEvent | ToolCallDeltaEvent,
                ):
                    now = time.monotonic()
                    if session._model_call_first_delta_at is None:
                        session._model_call_first_delta_at = now
                        if session._model_call_started_at is not None:
                            first_delta = now - session._model_call_started_at
                            if session.stream_first_delta_seconds is None:
                                session.stream_first_delta_seconds = first_delta
                    session._model_call_chunks += 1
                    if isinstance(event, ToolCallDeltaEvent):
                        session._model_call_tool_deltas += 1

                if isinstance(event, ModelCallStartEvent):
                    if session.agent_rounds >= session.max_agent_rounds:
                        exceeded_max_iters = True
                        break
                    if phase == "ttp":
                        _compact_ttp_history(
                            agent,
                            session,
                            progress=progress,
                        )
                    close_round()
                    session.record_agent_round(phase)
                    session._model_call_started_at = time.monotonic()
                    session._model_call_first_delta_at = None
                    session._model_call_chunks = 0
                    session._model_call_tool_deltas = 0
                    round_span_attributes = {
                        "phase": phase,
                        "round_index": session.agent_rounds,
                        "expected_tools": ",".join(expected_tools),
                        "remaining_rounds": max(
                            0,
                            session.max_agent_rounds - session.agent_rounds,
                        ),
                        "remaining_seconds": remaining_seconds(),
                    }
                    round_tool_names = set()
                    round_finished_reason = ""
                    round_outcome = "success"
                    round_span_manager = start_laminar_span(
                        "agent.round",
                        input={
                            "phase": phase,
                            "round_index": session.agent_rounds,
                        },
                        tags=("agent-round", f"{phase}-round"),
                        attributes=round_span_attributes,
                    )
                    round_span_manager.__enter__()
                    last_model_call_event_id = event.id
                    last_model_call_reply_id = event.reply_id
                    if progress is not None:
                        await _emit_context_snapshot(
                            progress,
                            agent,
                            event,
                            phase,
                        )
                    last_checkpoint = _checkpoint_context(agent)
                    last_expected_tools = expected_tools
                    last_call_had_tool = False
                    last_model_call_invalid = False
                    last_call_interrupted = False
                    tracker.reset()

                elif isinstance(event, ModelCallEndEvent):
                    round_finished_reason = str(event.finished_reason)
                    last_call_interrupted = (
                        event.finished_reason == FinishedReason.INTERRUPTED
                    )
                    session.model_calls_observed += 1
                    if session._model_call_started_at is not None:
                        session.stream_model_call_elapsed_seconds += (
                            time.monotonic() - session._model_call_started_at
                        )
                    session.stream_chunk_count += session._model_call_chunks
                    session.stream_tool_call_delta_count += (
                        session._model_call_tool_deltas
                    )
                    session._model_call_started_at = None
                    session._model_call_first_delta_at = None
                    session._model_call_chunks = 0
                    session._model_call_tool_deltas = 0
                    if session.stream_enabled and (
                        event.input_tokens or event.output_tokens
                    ):
                        session.stream_usage_seen = True
                    if event.input_tokens:
                        session.input_tokens_total += event.input_tokens
                        session.input_tokens_last = event.input_tokens
                    if event.output_tokens:
                        session.output_tokens_total += event.output_tokens

                elif isinstance(event, ToolCallStartEvent):
                    session.tool_call_starts += 1
                    tool_name = event.tool_call_name
                    if tool_name == FINISH_GENERATION_TOOL_NAME:
                        session.finish_called = True
                    round_tool_names.add(
                        tool_name
                        if tool_name in expected_tools
                        else "unrecognized_tool"
                    )
                    pending_tool_calls[event.tool_call_id] = tool_name
                    last_call_had_tool = True

                elif isinstance(event, ExceedMaxItersEvent):
                    exceeded_max_iters = True
                    # AgentScope may emit the max-iteration marker before the
                    # reply generator yields its final cleanup events. Stop at
                    # that safe suspension point instead of allowing a stale
                    # ModelCallStartEvent to trigger one more provider call.
                    break

                if isinstance(event, ToolResultEndEvent):
                    pending = pending_tool_calls.pop(event.tool_call_id, None)
                    pending_expected = pending is not None and pending in expected_tools
                    if event.state == ToolResultState.ERROR:
                        session.tool_result_errors += 1
                    boundary = tracker.boundaries.get(pending or "")
                    failure: ProtocolFailure | None = None
                    if event.state == ToolResultState.INTERRUPTED:
                        last_call_interrupted = True
                    elif pending is not None and not pending_expected:
                        failure = "wrong_tool"
                    elif pending_expected and boundary == "rejected":
                        failure = "product_arguments"
                    elif (
                        pending_expected
                        and boundary is None
                        and event.state == ToolResultState.ERROR
                    ):
                        # AgentScope rejects an unavailable tool or invalid
                        # arguments before entering our request-local tool.
                        # Execution failures have an explicit boundary marker.
                        failure = "framework_arguments"
                    elif pending_expected and boundary in {"valid", "execution_failed"}:
                        consecutive_protocol_failures = 0
                        session.reset_no_tool_sequence(phase)
                        last_model_call_invalid = False
                        observe_boundary(
                            "execution_error"
                            if boundary == "execution_failed"
                            or event.state == ToolResultState.ERROR
                            else "business_rejected"
                            if (event.metadata or {}).get("accepted") is False
                            else "arguments_valid"
                        )
                    elif pending_expected and boundary == "entered":
                        # An unexpected failure between entry and argument
                        # validation is infrastructure, not model input.
                        observe_boundary("execution_error")

                    if failure is not None:
                        if failure != "wrong_tool":
                            session.submission_tool_call_invalids += 1
                            last_model_call_invalid = True
                        if last_checkpoint is None:
                            raise RuntimeError(
                                "Protocol result has no context checkpoint."
                            )
                        # Remove the whole rejected completion with both call
                        # and result. Initial input and earlier legitimate
                        # tool evidence remain at the checkpoint unchanged.
                        _restore_context(agent, last_checkpoint)
                        repair_allowed = record_protocol_failure(failure)
                        if repair_allowed:
                            agent.state.context.append(
                                UserMsg(
                                    name="user",
                                    content=protocol_repair_text(
                                        failure, expected_tools
                                    ),
                                )
                            )

                    if (
                        phase == "ttp"
                        and pending is not None
                        and pending
                        in {SUBMIT_TEMPLATE_TOOL_NAME, TEST_TEMPLATE_TOOL_NAME}
                        and event.state == ToolResultState.SUCCESS
                    ):
                        _compact_ttp_history(
                            agent,
                            session,
                            progress=progress,
                        )

                    # A tool result is the safe suspension point;
                    # ``_terminal_tool_observed`` stops the reply once
                    # the timeout reason is set.
                    if (
                        session.terminal_reason is None
                        and not session.has_time_for_another_round()
                    ):
                        session.terminal_reason = "generation_timeout"
                        if progress is not None:
                            progress.custom(
                                "cli_parser.round.skipped",
                                {
                                    "reason": "insufficient_remaining_time",
                                    "remaining_seconds": remaining_seconds(),
                                },
                                phase=phase,
                                sensitive=False,
                            )

                    if (
                        protocol_retry_limit or _terminal_tool_observed(session, phase)
                    ) and not stopped_after_terminal_tool:
                        stopped_after_terminal_tool = True
                        terminal_checkpoint = _checkpoint_context(agent)
                        agent.react_config.max_iters = agent.state.cur_iter + 1
                        internal_cancel_task = asyncio.current_task()
                        if internal_cancel_task is None:
                            raise RuntimeError(
                                "The reply stream must run inside an asyncio task.",
                            )
                        internal_cancel_token = object()
                        if not internal_cancel_task.cancel(internal_cancel_token):
                            raise RuntimeError(
                                "Failed to interrupt the terminal reply stream.",
                            )
            raise_pending_external_cancellation(internal_cancel_task)
        except asyncio.CancelledError as error:
            if internal_cancel_task is None:
                round_outcome = "cancelled"
                raise
            remaining_cancellations = internal_cancel_task.uncancel()
            internal_cancel_task = None
            internal_cancel = (
                bool(error.args) and error.args[0] is internal_cancel_token
            )
            if not internal_cancel or remaining_cancellations:
                round_outcome = "cancelled"
                raise
        except BaseException:
            round_outcome = "exception"
            if internal_cancel_task is not None:
                internal_cancel_task.uncancel()
                internal_cancel_task = None
            raise
        else:
            if internal_cancel_task is not None:
                try:
                    # A fully synchronous reply can reach its natural end
                    # before the task has a chance to receive our cancellation.
                    # Deliver it now before balancing the cancellation count;
                    # on Python 3.11/3.12, uncanceling an undelivered request
                    # does not clear the task's pending _must_cancel state.
                    await asyncio.sleep(0)
                except asyncio.CancelledError as error:
                    remaining_cancellations = internal_cancel_task.uncancel()
                    internal_cancel_task = None
                    internal_cancel = (
                        bool(error.args) and error.args[0] is internal_cancel_token
                    )
                    if not internal_cancel or remaining_cancellations:
                        raise
                else:
                    internal_cancel_task.uncancel()
                    internal_cancel_task = None
        finally:
            close_round()
            agent.react_config.max_iters = original_reply_max_iters
            if terminal_checkpoint is not None:
                _restore_context(agent, terminal_checkpoint)

        if stopped_after_terminal_tool or _phase_completed(session, phase):
            break
        if session.terminal_reason in {
            "generation_timeout",
            "ttp_submission_limit",
            "ttp_worker_unavailable",
        }:
            break
        if exceeded_max_iters or last_call_interrupted:
            break

        no_tool_response = (
            last_checkpoint is not None
            and last_expected_tools is not None
            and not last_call_had_tool
        )
        if not no_tool_response:
            break

        retained_reasoning = (
            _reasoning_delta(agent, last_checkpoint, session.agent_rounds)
            if phase == "schema"
            else None
        )
        _restore_context(agent, last_checkpoint)
        if progress is not None and retained_reasoning is None:
            progress.custom(
                "cli_parser.model.output_discarded",
                {
                    "reply_id": last_model_call_reply_id,
                    "model_call_event_id": last_model_call_event_id,
                    "reason": "submission_tool_not_called",
                },
                phase=phase,
                sensitive=False,
            )
        protocol_repair_allowed = record_protocol_failure("no_tool")
        retry_allowed = session.record_no_tool_response(phase)
        if not retry_allowed or not protocol_repair_allowed:
            session.terminal_reason = "model_no_tool_retry_limit"
            model_no_tool_retry_limit = True
            break
        if session.agent_rounds >= session.max_agent_rounds:
            exceeded_max_iters = True
            break
        session.record_no_tool_retry(phase)
        if progress is not None:
            retry_number = (
                session.schema_no_tool_retries
                if phase == "schema"
                else session.ttp_no_tool_retries
            )
            max_retries = (
                session.max_schema_no_tool_retries
                if phase == "schema"
                else session.max_ttp_no_tool_retries
            )
            progress.custom(
                "cli_parser.no_tool.retry",
                {
                    "retry_number": retry_number,
                    "max_retries": max_retries,
                },
                phase=phase,
                sensitive=False,
            )
        next_message = _retry_message(phase, expected_tools)
        if retained_reasoning is not None:
            agent.state.context.append(retained_reasoning)
            if progress is not None:
                progress.custom(
                    "cli_parser.schema.reasoning_history",
                    {
                        "runtime_policy": "schema-reasoning-history-v1",
                        "status": "retained",
                        "round_index": session.agent_rounds,
                        "reasoning_chars": sum(
                            len(block.thinking)
                            for block in retained_reasoning.get_content_blocks()
                        ),
                    },
                    phase=phase,
                    sensitive=False,
                )

    phase_completed = _phase_completed(session, phase)
    if (
        not phase_completed
        and session.agent_rounds >= session.max_agent_rounds
        and session.terminal_reason is None
    ):
        exceeded_max_iters = True

    return AgentRunOutcome(
        phase_completed=phase_completed,
        exceeded_max_iters=exceeded_max_iters,
        stopped_after_terminal_tool=stopped_after_terminal_tool,
        model_no_tool_retry_limit=model_no_tool_retry_limit,
        protocol_retry_limit=protocol_retry_limit,
        ended_after_invalid_tool_call=last_model_call_invalid,
        tool_call_starts=session.tool_call_starts,
        tool_result_errors=session.tool_result_errors,
        submission_tool_call_invalids=session.submission_tool_call_invalids,
    )


__all__ = ["AgentRunOutcome", "run_generation_phase"]
