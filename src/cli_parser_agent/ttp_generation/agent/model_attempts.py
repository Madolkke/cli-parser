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
from typing import Any, TypeVar, cast

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
