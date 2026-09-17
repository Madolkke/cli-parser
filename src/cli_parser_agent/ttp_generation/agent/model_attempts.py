"""Count provider attempts without replacing AgentScope's retry policy."""

from __future__ import annotations

import asyncio
import inspect
import logging
import time
from collections.abc import AsyncGenerator, Awaitable, Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, TypeVar, cast
from urllib.parse import urlsplit

import openai
from agentscope.message import Msg, ThinkingBlock
from agentscope.model import ChatResponse, OpenAIChatModel
from agentscope.tool import ToolChoice
from lmnr import Laminar
from opentelemetry import context as otel_context
from opentelemetry import trace as otel_trace

from ...observability import finish_laminar_span
from ..progress import ProgressEmitter
from .session import GenerationPhase, GenerationSession

_T = TypeVar("_T")
_call_streams: ContextVar[list[_ObservedStream] | None] = ContextVar(
    "ttp_model_attempt_streams",
    default=None,
)


@dataclass(slots=True)
class _ProviderReplyFacts:
    """Request-local facts only; provider bodies never enter recovery state."""

    round_index: int
    attempt_index: int
    finished_reason: str = "unknown"
    reasoning_present: bool = False
    text_present: bool = False
    tool_calls_present: bool = False
    completed: bool = False

    def observe(self, choice: Any, message: Any) -> None:
        reason = getattr(choice, "finish_reason", None)
        if reason is not None:
            self.finished_reason = (
                reason
                if reason
                in {"length", "stop", "tool_calls", "content_filter", "function_call"}
                else "unknown"
            )
        reasoning = getattr(message, "reasoning_content", None)
        if not isinstance(reasoning, str):
            reasoning = getattr(message, "reasoning", None)
        self.reasoning_present |= isinstance(reasoning, str) and bool(reasoning)
        self.text_present |= bool(getattr(message, "content", None)) or bool(
            getattr(message, "audio", None)
        )
        self.tool_calls_present |= bool(getattr(message, "tool_calls", None)) or bool(
            getattr(message, "function_call", None)
        )


class _ProviderStreamFacts:
    """Observe raw chunks while leaving parsing and closing to AgentScope."""

    def __init__(self, source: Any, facts: _ProviderReplyFacts) -> None:
        self.source = source
        self.facts = facts
        self.iterator: Any = None

    async def __aenter__(self) -> _ProviderStreamFacts:
        source = await self.source.__aenter__()
        self.iterator = source.__aiter__()
        return self

    async def __aexit__(self, *args: Any) -> Any:
        return await self.source.__aexit__(*args)

    def __aiter__(self) -> _ProviderStreamFacts:
        return self

    async def __anext__(self) -> Any:
        chunk = await anext(self.iterator)
        choices = getattr(chunk, "choices", None)
        if choices:
            self.facts.observe(choices[0], choices[0].delta)
        return chunk


class _RetryLogFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        return not (
            _call_streams.get() is not None
            and record.msg
            == ("Attempt %d failed for model %s: %s. Retrying in %.1fs...")
        )


# AgentScope 2.0 logs raw retry exception text. Suppress only that message
# while our adapter is active; safe attempt events carry its useful facts.
logging.getLogger("as").addFilter(_RetryLogFilter())


def _error_category(error: BaseException) -> str:
    if isinstance(error, asyncio.CancelledError):
        return "cancelled"
    if isinstance(error, (openai.APITimeoutError, TimeoutError)):
        return "timeout"
    for error_type, category in (
        (openai.APIConnectionError, "connection"),
        (openai.RateLimitError, "rate_limit"),
        (openai.AuthenticationError, "authentication"),
        (openai.PermissionDeniedError, "permission"),
        (openai.BadRequestError, "request"),
        (openai.InternalServerError, "server"),
        (openai.APIResponseValidationError, "response"),
    ):
        if isinstance(error, error_type):
            return category
    return "unknown"


@dataclass(slots=True)
class _Attempt:
    attributes: dict[str, Any]
    progress: ProgressEmitter | None
    phase: GenerationPhase
    started: float = field(default_factory=time.monotonic)
    span: Any = None
    finished: bool = False

    def __post_init__(self) -> None:
        if Laminar.is_initialized():
            context = otel_context.get_current()
            parent = otel_trace.get_current_span(context).get_span_context()
            self.span = Laminar.start_span(
                "model.attempt",
                input=self.attributes,
                span_type="DEFAULT",
                context=context if parent.is_valid else None,
                tags=["model-attempt", f"{self.phase}-attempt"],
                attributes=self.attributes,
            )
        self._emit({"event": "started", **self.attributes})

    def _emit(self, value: dict[str, Any]) -> None:
        if self.progress is not None:
            self.progress.custom(
                "cli_parser.model.attempt",
                value,
                phase=self.phase,
                sensitive=False,
            )

    @contextmanager
    def active(self) -> Iterator[None]:
        if self.span is None:
            yield
        else:
            with Laminar.use_span(
                self.span,
                record_exception=False,
                set_status_on_exception=False,
            ):
                yield

    def finish(self, outcome: str, error_category: str = "none") -> None:
        if self.finished:
            return
        self.finished = True
        value = {
            **self.attributes,
            "outcome": outcome,
            "error_category": error_category,
            "elapsed_seconds": max(0.0, time.monotonic() - self.started),
        }
        try:
            with self.active():
                if self.span is not None:
                    finish_laminar_span(
                        output=value,
                        outcome=(
                            "success"
                            if outcome == "success"
                            else "cancelled"
                            if outcome in {"cancelled", "closed"}
                            else "exception"
                        ),
                        attributes=value,
                    )
        finally:
            if self.span is not None:
                self.span.end()
            self._emit({"event": "finished", **value})

    def failed(self, error: BaseException) -> None:
        category = _error_category(error)
        self.finish(
            "cancelled" if category == "cancelled" else "exception",
            category,
        )


class _ObservedStream:
    """Attach the attempt only while advancing or closing its source stream."""

    def __init__(self, source: AsyncGenerator[Any, None], attempt: _Attempt):
        self.source = source
        self.attempt = attempt

    def __aiter__(self) -> _ObservedStream:
        return self

    async def __anext__(self) -> Any:
        try:
            with self.attempt.active():
                return await anext(self.source)
        except StopAsyncIteration:
            self.attempt.finish("success")
            raise
        except BaseException as error:
            self.attempt.failed(error)
            raise

    async def aclose(self) -> None:
        try:
            with self.attempt.active():
                await self.source.aclose()
        except BaseException as error:
            self.attempt.failed(error)
            raise
        else:
            self.attempt.finish("closed", "closed")


@dataclass(slots=True)
class ModelAttemptRecorder:
    """Safe request-local counters and lifecycle around an ordinary coroutine."""

    session: GenerationSession
    phase: GenerationPhase
    progress: ProgressEmitter | None = None
    _round_attempts: dict[int, int] = field(default_factory=dict)

    async def call(self, operation: Callable[[], Awaitable[_T]]) -> _T:
        round_index = self.session.agent_rounds
        attempt_index = self._round_attempts.get(round_index, 0) + 1
        self._round_attempts[round_index] = attempt_index
        self.session.model_attempts_observed += 1
        is_retry = attempt_index > 1
        if is_retry:
            self.session.model_retries_observed += 1
        attempt = _Attempt(
            attributes={
                "phase": self.phase,
                "round_index": round_index,
                "attempt_index": attempt_index,
                "request_attempt_index": self.session.model_attempts_observed,
                "is_retry": is_retry,
            },
            progress=self.progress,
            phase=self.phase,
        )
        try:
            with attempt.active():
                result = await operation()
        except BaseException as error:
            attempt.failed(error)
            raise
        if inspect.isasyncgen(result):
            observed = _ObservedStream(result, attempt)
            streams = _call_streams.get()
            if streams is not None:
                streams.append(observed)
            return cast(_T, observed)
        attempt.finish("success")
        return result


async def _close_stream_on_exit(
    source: AsyncGenerator[_T, None],
    streams: list[_ObservedStream],
) -> AsyncGenerator[_T, None]:
    async def consume() -> AsyncGenerator[_T | None, None]:
        try:
            # Prime only this wrapper so closing before the first model
            # chunk still reaches finally; no provider data is prefetched.
            yield None
            async for chunk in source:
                yield chunk
        finally:
            try:
                await source.aclose()
            finally:
                # AgentScope 2.0's accumulation wrapper does not forward
                # explicit close to its source generator.
                for stream in streams:
                    await stream.aclose()

    wrapped = consume()
    await anext(wrapped)
    return cast(AsyncGenerator[_T, None], wrapped)


class ObservedOpenAIChatModel(OpenAIChatModel):
    """Observe attempts and align token estimation with the OpenAI formatter."""

    def __init__(
        self,
        *,
        attempt_recorder: ModelAttemptRecorder,
        **kwargs: Any,
    ) -> None:
        super().__init__(**kwargs)
        self._attempt_recorder = attempt_recorder
        self._provider_reply_facts: _ProviderReplyFacts | None = None
        self._reasoning_length_streak = 0
        self._last_recovery_round = -1
        self._schema_reasoning_recovery = False

    def prepare_schema_reasoning_recovery(self) -> bool:
        """Use only an existing retry after three observed reasoning-only limits."""

        recorder = self._attempt_recorder
        facts = self._provider_reply_facts
        if (
            self._schema_reasoning_recovery
            or recorder.phase != "schema"
            or urlsplit(self.credential.base_url or "").hostname != "api.deepseek.com"
            or self.parameters.thinking_enable
            or self.parameters.reasoning_effort is not None
            or self.extra_body is not None
            or facts is None
            or not facts.completed
            or facts.round_index != recorder.session.agent_rounds
            or facts.attempt_index != recorder.session.model_attempts_observed
            or facts.round_index == self._last_recovery_round
        ):
            return False
        qualifies = (
            facts.finished_reason == "length"
            and facts.reasoning_present
            and not facts.text_present
            and not facts.tool_calls_present
        )
        if not qualifies:
            self._reasoning_length_streak = 0
        elif facts.round_index == self._last_recovery_round + 1:
            self._reasoning_length_streak += 1
        else:
            self._reasoning_length_streak = 1
        self._last_recovery_round = facts.round_index
        if self._reasoning_length_streak < 3:
            return False
        self._schema_reasoning_recovery = True
        if recorder.progress is not None:
            recorder.progress.custom(
                "cli_parser.schema.reasoning_recovery",
                {
                    "reason": "consecutive_reasoning_only_length",
                    "consecutive_responses": self._reasoning_length_streak,
                    "after_round_index": facts.round_index,
                    "after_attempt_index": facts.attempt_index,
                    "mode": "thinking_disabled",
                    "runtime_policy": "schema-reasoning-recovery-v1",
                },
                phase="schema",
                sensitive=False,
            )
        return True

    def _parse_completion_response(
        self, start_datetime: datetime, response: Any, audio_format: str = "wav"
    ) -> ChatResponse:
        facts = self._provider_reply_facts
        result = super()._parse_completion_response(
            start_datetime, response, audio_format
        )
        if facts is not None:
            if response.choices:
                facts.observe(response.choices[0], response.choices[0].message)
            facts.completed = True
        return result

    async def _parse_stream_response(
        self, start_datetime: datetime, response: Any
    ) -> AsyncGenerator[ChatResponse, None]:
        facts = self._provider_reply_facts
        source = (
            _ProviderStreamFacts(response, facts) if facts is not None else response
        )
        parsed = super()._parse_stream_response(start_datetime, source)
        try:
            async for chunk in parsed:
                yield chunk
        finally:
            await parsed.aclose()
        if facts is not None:
            facts.completed = True

    async def count_tokens(
        self,
        messages: list[Msg],
        tools: list[dict] | None,
    ) -> int:
        """Exclude unsent Thinking from counting copies, preserving history."""

        # The locked OpenAI formatter skips ThinkingBlock, while the generic
        # AgentScope estimator counts it. Keep every other estimate unchanged;
        # this remains a UTF-8 approximation, not a provider tokenizer.
        counting_messages = [
            message.model_copy(
                update={
                    "content": [
                        block
                        for block in message.get_content_blocks()
                        if not isinstance(block, ThinkingBlock)
                    ],
                },
            )
            for message in messages
        ]
        return await super().count_tokens(counting_messages, tools)

    async def _call_api(
        self,
        model_name: str,
        messages: list[Msg],
        tools: list[dict] | None = None,
        tool_choice: ToolChoice | None = None,
        **kwargs: Any,
    ) -> ChatResponse | AsyncGenerator[ChatResponse, None]:
        self._provider_reply_facts = _ProviderReplyFacts(
            round_index=self._attempt_recorder.session.agent_rounds,
            attempt_index=self._attempt_recorder.session.model_attempts_observed + 1,
        )
        # AgentScope 2.0 maps Parameters.max_tokens to the OpenAI-specific
        # max_completion_tokens. DeepSeek's official API documents max_tokens
        # instead. Suppress the incompatible SDK argument without modifying
        # Parameters, history, or the request-local model's configured budget.
        if urlsplit(self.credential.base_url or "").hostname == "api.deepseek.com":
            if self.extra_body and {"max_tokens", "max_completion_tokens"}.intersection(
                self.extra_body
            ):
                raise ValueError("Output limits must be configured through max_tokens")
            kwargs["max_completion_tokens"] = openai.NOT_GIVEN
            if self.parameters.max_tokens is not None:
                kwargs["max_tokens"] = self.parameters.max_tokens
            if self._schema_reasoning_recovery:
                kwargs["extra_body"] = {"thinking": {"type": "disabled"}}
        return await self._attempt_recorder.call(
            lambda: super(ObservedOpenAIChatModel, self)._call_api(
                model_name,
                messages=messages,
                tools=tools,
                tool_choice=tool_choice,
                **kwargs,
            ),
        )

    async def __call__(
        self,
        messages: list[Msg],
        tools: list[dict] | None = None,
        tool_choice: ToolChoice | None = None,
        **kwargs: Any,
    ) -> ChatResponse | AsyncGenerator[ChatResponse, None]:
        streams: list[_ObservedStream] = []
        token = _call_streams.set(streams)
        try:
            result = await super().__call__(
                messages,
                tools=tools,
                tool_choice=tool_choice,
                **kwargs,
            )
        finally:
            _call_streams.reset(token)
        if not inspect.isasyncgen(result):
            return result
        return await _close_stream_on_exit(result, streams)
