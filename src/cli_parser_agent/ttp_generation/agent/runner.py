"""Event-aware execution of the request-local AgentScope loop."""

from __future__ import annotations

import asyncio
from copy import deepcopy
from dataclasses import dataclass
from typing import Any

from agentscope.event import (
    ExceedMaxItersEvent,
    ModelCallEndEvent,
    ModelCallStartEvent,
    ToolCallStartEvent,
    ToolResultEndEvent,
)
from agentscope.message import ToolResultState, UserMsg
from agentscope.model import FinishedReason

from .prompt import SCHEMA_NO_TOOL_RETRY_PROMPT, TTP_NO_TOOL_RETRY_PROMPT
from .tools import (
    SUBMIT_SCHEMA_TOOL_NAME,
    SUBMIT_TEMPLATE_TOOL_NAME,
    GenerationSession,
)


@dataclass(frozen=True, slots=True)
class AgentRunOutcome:
    """Framework-neutral facts observed across bounded AgentScope replies."""

    exceeded_max_iters: bool = False
    stopped_after_terminal_tool: bool = False
    model_no_tool_retry_limit: bool = False
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


def _retry_message(tool_name: str) -> UserMsg:
    if tool_name == SUBMIT_SCHEMA_TOOL_NAME:
        content = SCHEMA_NO_TOOL_RETRY_PROMPT
    elif tool_name == SUBMIT_TEMPLATE_TOOL_NAME:
        content = TTP_NO_TOOL_RETRY_PROMPT
    else:
        raise RuntimeError("No retry prompt exists for the current phase.")
    return UserMsg(name="user", content=content)


async def run_generation_agent(
    agent: Any,
    message: Any,
    session: GenerationSession,
) -> AgentRunOutcome:
    """Run bounded replies, retrying only sanitized no-tool completions."""

    exceeded_max_iters = False
    stopped_after_terminal_tool = False
    model_no_tool_retry_limit = False
    next_message = message

    while session.agent_rounds < session.max_agent_rounds:
        remaining_rounds = session.max_agent_rounds - session.agent_rounds
        agent.react_config.max_iters = remaining_rounds

        last_checkpoint: _ContextCheckpoint | None = None
        last_expected_tool: str | None = None
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

                if isinstance(event, ModelCallStartEvent):
                    session.agent_rounds += 1
                    last_checkpoint = _checkpoint_context(agent)
                    last_expected_tool = session.current_phase_tool_name()
                    last_call_had_tool = False
                    last_call_interrupted = False

                elif isinstance(event, ModelCallEndEvent):
                    last_call_interrupted = (
                        event.finished_reason == FinishedReason.INTERRUPTED
                    )

                elif isinstance(event, ToolCallStartEvent):
                    session.tool_call_starts += 1
                    last_call_had_tool = True
                    tool_name = event.tool_call_name
                    pending_tool_calls[event.tool_call_id] = (
                        tool_name,
                        _submission_count(session, tool_name),
                    )
                    if tool_name == last_expected_tool:
                        session.reset_no_tool_sequence(tool_name)

                elif isinstance(event, ExceedMaxItersEvent):
                    exceeded_max_iters = True

                if isinstance(event, ToolResultEndEvent):
                    pending = pending_tool_calls.pop(event.tool_call_id, None)
                    if event.state == ToolResultState.ERROR:
                        session.tool_result_errors += 1
                        if pending is not None:
                            tool_name, submissions_before = pending
                            if (
                                tool_name
                                in {
                                    SUBMIT_SCHEMA_TOOL_NAME,
                                    SUBMIT_TEMPLATE_TOOL_NAME,
                                }
                                and _submission_count(session, tool_name)
                                == submissions_before
                            ):
                                session.submission_tool_call_invalids += 1

                    if (
                        session.succeeded
                        or session.terminal_reason == "ttp_worker_unavailable"
                        or session.ttp_submissions >= session.max_ttp_submissions
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

        if stopped_after_terminal_tool or session.succeeded:
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
            and last_expected_tool is not None
            and not last_call_had_tool
        )
        if not no_tool_response:
            break

        _restore_context(agent, last_checkpoint)
        retry_allowed = session.record_no_tool_response(last_expected_tool)
        if not retry_allowed:
            session.terminal_reason = "model_no_tool_retry_limit"
            model_no_tool_retry_limit = True
            break
        if session.agent_rounds >= session.max_agent_rounds:
            exceeded_max_iters = True
            break

        session.record_no_tool_retry(last_expected_tool)
        next_message = _retry_message(last_expected_tool)

    if (
        not session.succeeded
        and session.agent_rounds >= session.max_agent_rounds
        and session.terminal_reason is None
    ):
        exceeded_max_iters = True

    return AgentRunOutcome(
        exceeded_max_iters=exceeded_max_iters,
        stopped_after_terminal_tool=stopped_after_terminal_tool,
        model_no_tool_retry_limit=model_no_tool_retry_limit,
        tool_call_starts=session.tool_call_starts,
        tool_result_errors=session.tool_result_errors,
        submission_tool_call_invalids=session.submission_tool_call_invalids,
    )


__all__ = ["AgentRunOutcome", "run_generation_agent"]
