"""Event-aware execution of the request-local AgentScope loop."""

from __future__ import annotations

import asyncio
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
    TextBlockEndEvent,
    TextBlockStartEvent,
    ThinkingBlockEndEvent,
    ThinkingBlockStartEvent,
    ToolCallEndEvent,
    ToolCallStartEvent,
    ToolResultEndEvent,
    ToolResultStartEvent,
)
from agentscope.message import ToolResultState, UserMsg
from agentscope.model import FinishedReason

from ..progress import ProgressEmitter
from .prompt import SCHEMA_NO_TOOL_RETRY_PROMPT, TTP_NO_TOOL_RETRY_PROMPT
from .session import GenerationPhase, GenerationSession
from .tools import (
    FINISH_GENERATION_TOOL_NAME,
    SUBMIT_SCHEMA_TOOL_NAME,
    SUBMIT_TEMPLATE_TOOL_NAME,
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


def _submission_count(session: GenerationSession, tool_name: str) -> int:
    if tool_name == SUBMIT_SCHEMA_TOOL_NAME:
        return session.schema_submissions
    if tool_name == SUBMIT_TEMPLATE_TOOL_NAME:
        return session.ttp_submissions
    return 0


def _retry_message(phase: GenerationPhase) -> UserMsg:
    if phase == "schema":
        content = SCHEMA_NO_TOOL_RETRY_PROMPT
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
            FINISH_GENERATION_TOOL_NAME,
        )
    raise ValueError(f"Unsupported generation phase: {phase!r}")


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
    """Run one isolated phase, retrying only sanitized no-tool completions."""

    expected_tools = _expected_tool_names(phase)
    exceeded_max_iters = False
    stopped_after_terminal_tool = False
    model_no_tool_retry_limit = False
    last_model_call_invalid = False
    last_model_call_event_id: str | None = None
    last_model_call_reply_id: str | None = None
    next_message = message

    if _phase_completed(session, phase):
        return AgentRunOutcome(
            phase_completed=True,
            tool_call_starts=session.tool_call_starts,
            tool_result_errors=session.tool_result_errors,
            submission_tool_call_invalids=session.submission_tool_call_invalids,
        )

    while session.agent_rounds < session.max_agent_rounds:
        remaining_rounds = session.max_agent_rounds - session.agent_rounds
        agent.react_config.max_iters = remaining_rounds

        last_checkpoint: _ContextCheckpoint | None = None
        last_expected_tools: tuple[str, ...] | None = None
        last_call_had_tool = False
        last_call_interrupted = False
        pending_tool_calls: dict[str, tuple[str, int]] = {}
        terminal_checkpoint: _ContextCheckpoint | None = None
        internal_cancel_task: asyncio.Task[Any] | None = None
        internal_cancel_token: object | None = None
        original_reply_max_iters = agent.react_config.max_iters

        stream = agent.reply_stream(next_message)
        try:
            async for event in stream:
                if stopped_after_terminal_tool:
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

                if isinstance(event, ModelCallStartEvent):
                    session.record_agent_round(phase)
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

                elif isinstance(event, ModelCallEndEvent):
                    last_call_interrupted = (
                        event.finished_reason == FinishedReason.INTERRUPTED
                    )

                elif isinstance(event, ToolCallStartEvent):
                    session.tool_call_starts += 1
                    tool_name = event.tool_call_name
                    pending_tool_calls[event.tool_call_id] = (
                        tool_name,
                        _submission_count(session, tool_name),
                    )
                    if last_expected_tools and tool_name in last_expected_tools:
                        last_call_had_tool = True
                        session.reset_no_tool_sequence(phase)

                elif isinstance(event, ExceedMaxItersEvent):
                    exceeded_max_iters = True

                if isinstance(event, ToolResultEndEvent):
                    pending = pending_tool_calls.pop(event.tool_call_id, None)
                    pending_expected = (
                        pending is not None and pending[0] in expected_tools
                    )
                    if event.state == ToolResultState.ERROR:
                        session.tool_result_errors += 1
                        if pending is not None:
                            tool_name, submissions_before = pending
                            if (
                                tool_name
                                in {
                                    SUBMIT_SCHEMA_TOOL_NAME,
                                    SUBMIT_TEMPLATE_TOOL_NAME,
                                    FINISH_GENERATION_TOOL_NAME,
                                }
                                and _submission_count(session, tool_name)
                                == submissions_before
                            ):
                                session.submission_tool_call_invalids += 1
                                last_model_call_invalid = True
                    elif pending_expected:
                        last_model_call_invalid = False

                    if (
                        _terminal_tool_observed(
                            session,
                            phase,
                        )
                        and not stopped_after_terminal_tool
                    ):
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
        except asyncio.CancelledError as error:
            if internal_cancel_task is None:
                raise
            remaining_cancellations = internal_cancel_task.uncancel()
            internal_cancel_task = None
            internal_cancel = (
                bool(error.args) and error.args[0] is internal_cancel_token
            )
            if not internal_cancel or remaining_cancellations:
                raise
        except BaseException:
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

        _restore_context(agent, last_checkpoint)
        if progress is not None:
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
        retry_allowed = session.record_no_tool_response(phase)
        if not retry_allowed:
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
        next_message = _retry_message(phase)

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
        ended_after_invalid_tool_call=last_model_call_invalid,
        tool_call_starts=session.tool_call_starts,
        tool_result_errors=session.tool_result_errors,
        submission_tool_call_invalids=session.submission_tool_call_invalids,
    )


__all__ = ["AgentRunOutcome", "run_generation_phase"]
