"""Attempt lifecycle tests use plain coroutines, never substitute model replies."""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import AsyncGenerator, Iterator
from contextlib import contextmanager
from typing import Any

import httpx
import openai
import pytest
from agentscope.credential import OpenAICredential
from agentscope.event import CustomEvent
from agentscope.model import FinishedReason, OpenAIChatModel
from lmnr import Laminar
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from cli_parser_agent.ttp_generation.agent.model_attempts import (
    ModelAttemptRecorder,
    ObservedOpenAIChatModel,
    _call_streams,
    _close_stream_on_exit,
)
from cli_parser_agent.ttp_generation.agent.session import GenerationSession
from cli_parser_agent.ttp_generation.progress import ProgressEmitter


def _session() -> GenerationSession:
    def unused(_: Any) -> Any:
        raise AssertionError("attempt recording must not invoke validators")

    return GenerationSession(
        command_outputs=("value: one",),
        schema_validator=unused,
        template_validator=unused,
    )


def _recorder(
    events: list[Any],
    *,
    session: GenerationSession | None = None,
    phase: str = "ttp",
) -> ModelAttemptRecorder:
    return ModelAttemptRecorder(
        session=session or _session(),
        phase=phase,  # type: ignore[arg-type]
        progress=ProgressEmitter("attempt-test", events.append),
    )


def _finished(events: list[Any]) -> list[dict[str, Any]]:
    assert all(isinstance(event, CustomEvent) for event in events)
    assert all(event.name == "cli_parser.model.attempt" for event in events)
    assert all(event.metadata["sensitive"] is False for event in events)
    return [event.value for event in events if event.value["event"] == "finished"]


@pytest.fixture(autouse=True)
def _disable_laminar(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(Laminar, "is_initialized", lambda: False)


@pytest.fixture
def spans(monkeypatch: pytest.MonkeyPatch) -> Iterator[InMemorySpanExporter]:
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    tracer = provider.get_tracer("attempt-tests")
    monkeypatch.setattr(Laminar, "is_initialized", lambda: True)

    def start(name: str, **kwargs: Any) -> Any:
        assert kwargs["span_type"] == "DEFAULT"
        return tracer.start_span(
            name, context=kwargs["context"], attributes=kwargs["attributes"]
        )

    @contextmanager
    def use(span: Any, **kwargs: Any) -> Iterator[None]:
        assert kwargs == {"record_exception": False, "set_status_on_exception": False}
        with trace.use_span(span, **kwargs):
            yield

    monkeypatch.setattr(Laminar, "start_span", start)
    monkeypatch.setattr(Laminar, "use_span", use)
    monkeypatch.setattr(Laminar, "get_current_span", trace.get_current_span)
    monkeypatch.setattr(
        Laminar,
        "set_span_output",
        lambda output: trace.get_current_span().set_attribute(
            "output", json.dumps(output)
        ),
    )
    monkeypatch.setattr(
        Laminar,
        "set_span_attributes",
        lambda attributes: trace.get_current_span().set_attributes(attributes),
    )
    monkeypatch.setattr(Laminar, "add_span_tags", lambda _: None)
    yield exporter
    provider.shutdown()


async def test_attempts_count_only_entries_and_reset_retries_per_phase_round() -> None:
    events: list[Any] = []
    recorder = _recorder(events)
    recorder.session.record_agent_round("ttp")
    failure = TimeoutError("secret provider body")

    async def failed() -> None:
        raise failure

    async def success() -> str:
        return "sensitive model content"

    with pytest.raises(TimeoutError) as raised:
        await recorder.call(failed)
    assert raised.value is failure
    assert recorder.session.model_attempts_observed == 1
    assert recorder.session.model_retries_observed == 0
    assert await recorder.call(success) == "sensitive model content"
    assert recorder.session.model_retries_observed == 1
    recorder.session.record_agent_round("ttp")
    await recorder.call(success)
    schema = _recorder(events, session=recorder.session, phase="schema")
    await schema.call(success)
    assert recorder.session.model_attempts_observed == 4
    assert recorder.session.model_retries_observed == 1
    assert [event["attempt_index"] for event in _finished(events)] == [1, 2, 1, 1]
    assert [event["request_attempt_index"] for event in _finished(events)] == [
        1,
        2,
        3,
        4,
    ]
    assert _finished(events)[0]["error_category"] == "timeout"
    assert "secret" not in json.dumps(
        [event.model_dump(mode="json") for event in events]
    )
    assert "sensitive model content" not in json.dumps(
        [event.value for event in events]
    )


async def test_cancellation_propagates_without_creating_a_retry() -> None:
    events: list[Any] = []
    recorder = _recorder(events)
    entered = asyncio.Event()

    async def waiting() -> None:
        entered.set()
        await asyncio.Event().wait()

    task = asyncio.create_task(recorder.call(waiting))
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert recorder.session.model_attempts_observed == 1
    assert recorder.session.model_retries_observed == 0
    assert _finished(events)[0]["outcome"] == "cancelled"


async def test_adapter_counts_inherited_retry_loop_without_model_reply_substitutes(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    events: list[Any] = []
    recorder = _recorder(events)
    recorder.session.record_agent_round("ttp")
    failure = openai.APIConnectionError(
        message="secret provider failure",
        request=httpx.Request("POST", "https://invalid.test"),
    )

    async def failed_operation(*_: Any, **__: Any) -> Any:
        raise failure

    monkeypatch.setattr(OpenAIChatModel, "_call_api", failed_operation)
    model = ObservedOpenAIChatModel(
        attempt_recorder=recorder,
        credential=OpenAICredential(api_key="unused-key"),
        model="test-model",
        max_retries=2,
        retry_delay=0,
    )
    with (
        caplog.at_level(logging.WARNING, logger="as"),
        pytest.raises(openai.APIConnectionError) as raised,
    ):
        await model([])
    assert raised.value is failure
    assert recorder.session.model_attempts_observed == 3
    assert recorder.session.model_retries_observed == 2
    assert len(_finished(events)) == 3
    assert _call_streams.get() is None
    assert "secret provider failure" not in caplog.text


async def test_adapter_retains_agentscope_interrupted_response_on_cancel(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[Any] = []
    recorder = _recorder(events)

    async def cancelled_operation(*_: Any, **__: Any) -> Any:
        raise asyncio.CancelledError()

    monkeypatch.setattr(OpenAIChatModel, "_call_api", cancelled_operation)
    model = ObservedOpenAIChatModel(
        attempt_recorder=recorder,
        credential=OpenAICredential(api_key="unused-key"),
        model="test-model",
        max_retries=2,
    )
    response = await model([])
    baseline = await OpenAIChatModel(
        credential=OpenAICredential(api_key="unused-key"),
        model="test-model",
        max_retries=2,
    )([])
    assert response["finished_reason"] == FinishedReason.INTERRUPTED
    assert response.finished_reason == baseline.finished_reason
    assert recorder.session.model_attempts_observed == 1
    assert recorder.session.model_retries_observed == 0
    assert _finished(events)[0]["outcome"] == "cancelled"
    assert _call_streams.get() is None


@pytest.mark.parametrize("close_before_consumption", [False, True])
async def test_adapter_closes_attempt_behind_agentscope_stream_wrapper(
    monkeypatch: pytest.MonkeyPatch,
    close_before_consumption: bool,
) -> None:
    events: list[Any] = []
    recorder = _recorder(events)
    failure = openai.APIConnectionError(
        message="secret streaming failure",
        request=httpx.Request("POST", "https://invalid.test"),
    )

    async def source() -> AsyncGenerator[Any, None]:
        raise failure
        yield  # pragma: no cover

    async def open_source(*_: Any, **__: Any) -> Any:
        return source()

    monkeypatch.setattr(OpenAIChatModel, "_call_api", open_source)
    model = ObservedOpenAIChatModel(
        attempt_recorder=recorder,
        credential=OpenAICredential(api_key="unused-key"),
        model="test-model",
        max_retries=2,
    )
    stream = await model([])
    assert _call_streams.get() is None
    assert not _finished(events)
    if not close_before_consumption:
        with pytest.raises(openai.APIConnectionError) as raised:
            await anext(stream)
        assert raised.value is failure
    await stream.aclose()
    assert _finished(events)[0]["outcome"] == (
        "closed" if close_before_consumption else "exception"
    )
    assert recorder.session.model_attempts_observed == 1
    # AgentScope retries only opening failures, never stream consumption errors.
    assert recorder.session.model_retries_observed == 0
    assert len(_finished(events)) == 1


@pytest.mark.parametrize("ending", ["success", "exception", "cancelled", "closed"])
async def test_stream_lifecycle_restores_context_between_chunks(
    spans: InMemorySpanExporter,
    ending: str,
) -> None:
    events: list[Any] = []
    recorder = _recorder(events)
    before = trace.get_current_span()
    active_spans: list[Any] = []
    closed = False

    async def source() -> AsyncGenerator[str, None]:
        nonlocal closed
        try:
            active_spans.append(trace.get_current_span())
            yield "first"
            active_spans.append(trace.get_current_span())
            if ending == "exception":
                raise ValueError("secret stream body")
            if ending == "cancelled":
                raise asyncio.CancelledError()
            yield "second"
        finally:
            closed = True
            active_spans.append(trace.get_current_span())

    async def open_source() -> Any:
        active_spans.append(trace.get_current_span())
        return source()

    stream = await recorder.call(open_source)
    assert trace.get_current_span() is before
    assert not spans.get_finished_spans()
    assert await anext(stream) == "first"
    assert trace.get_current_span() is before
    assert not _finished(events)
    if ending == "closed":
        await stream.aclose()
    elif ending in {"exception", "cancelled"}:
        error = ValueError if ending == "exception" else asyncio.CancelledError
        with pytest.raises(error):
            await anext(stream)
    else:
        assert await anext(stream) == "second"
        with pytest.raises(StopAsyncIteration):
            await anext(stream)
    await stream.aclose()
    assert closed
    assert trace.get_current_span() is before
    finished = spans.get_finished_spans()
    assert len(finished) == 1
    assert finished[0].name == "model.attempt"
    assert not finished[0].events
    assert all(item.get_span_context() == finished[0].context for item in active_spans)
    assert _finished(events)[0]["outcome"] == ending
    assert len(_finished(events)) == 1
    assert "secret" not in json.dumps(dict(finished[0].attributes))
    assert all(
        "token" not in key and "cost" not in key for key in finished[0].attributes
    )


@pytest.mark.parametrize("consume_first", [False, True])
async def test_outer_stream_close_reaches_inner_attempt(consume_first: bool) -> None:
    events: list[Any] = []
    recorder = _recorder(events)

    async def source() -> AsyncGenerator[int, None]:
        yield 1
        yield 2

    async def open_source() -> Any:
        return source()

    inner = await recorder.call(open_source)

    async def accumulator() -> AsyncGenerator[int, None]:
        async for item in inner:
            yield item

    outer = await _close_stream_on_exit(accumulator(), [inner])
    if consume_first:
        assert await anext(outer) == 1
    await outer.aclose()
    assert _finished(events)[0]["outcome"] == "closed"
    assert len(_finished(events)) == 1


async def test_concurrent_requests_keep_context_counts_and_spans_isolated(
    spans: InMemorySpanExporter,
) -> None:
    async def run(phase: str) -> tuple[GenerationSession, list[Any], Any]:
        events: list[Any] = []
        recorder = _recorder(events, phase=phase)
        recorder.session.record_agent_round(phase)  # type: ignore[arg-type]

        async def operation() -> Any:
            context = trace.get_current_span().get_span_context()
            await asyncio.sleep(0)
            assert trace.get_current_span().get_span_context() == context
            return context

        result = await recorder.call(operation)
        return recorder.session, events, result

    left, right = await asyncio.gather(run("schema"), run("ttp"))
    assert left[2] != right[2]
    assert len(spans.get_finished_spans()) == 2
    for session, events, _ in (left, right):
        assert session.model_attempts_observed == 1
        assert session.model_retries_observed == 0
        assert _finished(events)[0]["request_attempt_index"] == 1


async def test_retry_log_suppression_is_limited_to_this_adapter_context() -> None:
    logger = logging.getLogger("as")

    def retry_record() -> logging.LogRecord:
        return logger.makeRecord(
            logger.name,
            logging.WARNING,
            __file__,
            0,
            "Attempt %d failed for model %s: %s. Retrying in %.1fs...",
            (1, "model", "secret provider body", 1.0),
            None,
        )

    async def outside() -> bool:
        return logger.filter(retry_record())

    outside_task = asyncio.create_task(outside())
    token = _call_streams.set([])
    try:
        assert not logger.filter(retry_record())
        normal = logger.makeRecord(
            logger.name, logging.WARNING, __file__, 0, "safe fact", (), None
        )
        assert logger.filter(normal)
        assert await outside_task
    finally:
        _call_streams.reset(token)
    assert logger.filter(retry_record())
