"""Optional Laminar tracing initialization and request-local helpers."""

from __future__ import annotations

import os
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any, Literal

from lmnr import Instruments, Laminar
from opentelemetry import context as otel_context
from opentelemetry import trace as otel_trace
from opentelemetry.trace import Status, StatusCode

LaminarSpanType = Literal["DEFAULT", "LLM", "TOOL"]
LaminarSpanOutcome = Literal["success", "failed", "cancelled", "exception"]


@dataclass(frozen=True, slots=True)
class LaminarSpanScope:
    """Request-local facts about one optional Laminar span."""

    enabled: bool
    creates_trace: bool


def _optional_port(source: Mapping[str, str], name: str) -> int | None:
    """Parse one optional ASCII decimal TCP port from an environment mapping."""

    if name not in source:
        return None

    value = source[name]
    if not value or not value.isascii() or not value.isdecimal():
        raise ValueError(f"{name} must be a decimal integer from 1 to 65535")

    port = int(value, 10)
    if not 1 <= port <= 65_535:
        raise ValueError(f"{name} must be a decimal integer from 1 to 65535")
    return port


def initialize_laminar_from_env(
    environ: Mapping[str, str] | None = None,
) -> bool:
    """Initialize Laminar once when a project API key is available.

    An existing caller-owned Laminar configuration always wins. When an explicit
    environment mapping is supplied, only that mapping is used to resolve the
    project key, base URL, and ports.
    """

    if Laminar.is_initialized():
        return True

    source = os.environ if environ is None else environ
    project_api_key = source.get("LMNR_PROJECT_API_KEY", "").strip()
    if not project_api_key:
        return False

    base_url = source.get("LMNR_BASE_URL", "").strip()
    http_port = _optional_port(source, "LMNR_HTTP_PORT")
    grpc_port = _optional_port(source, "LMNR_GRPC_PORT")
    port_options = {
        name: value
        for name, value in (
            ("http_port", http_port),
            ("grpc_port", grpc_port),
        )
        if value is not None
    }
    Laminar.initialize(
        project_api_key=project_api_key,
        base_url=base_url or None,
        instruments={Instruments.OPENAI},
        **port_options,
    )
    return True


def current_laminar_trace_id() -> str | None:
    """Return the active Laminar trace ID without exposing SDK types."""

    if not Laminar.is_initialized():
        return None
    trace_id = Laminar.get_trace_id()
    return None if trace_id is None else str(trace_id)


@contextmanager
def start_laminar_span(
    name: str,
    *,
    input: Any = None,
    span_type: LaminarSpanType = "DEFAULT",
    tags: Sequence[str] = (),
    attributes: Mapping[str, Any] | None = None,
) -> Iterator[LaminarSpanScope]:
    """Start a span while preserving an active Laminar or OpenTelemetry trace.

    The returned ``creates_trace`` flag is decided before the span starts so a
    caller can restrict trace-wide metadata writes to a trace it owns. The SDK
    context manager is always closed as a normal exit; this prevents it from
    automatically recording exception text when application code re-raises.
    """

    if not Laminar.is_initialized():
        yield LaminarSpanScope(enabled=False, creates_trace=False)
        return

    global_context = otel_context.get_current()
    global_span_context = otel_trace.get_current_span(
        context=global_context,
    ).get_span_context()
    has_global_parent = global_span_context.is_valid
    has_laminar_parent = current_laminar_trace_id() is not None
    creates_trace = not (has_global_parent or has_laminar_parent)

    manager = Laminar.start_as_current_span(
        name,
        input=input,
        span_type=span_type,
        context=global_context if has_global_parent else None,
        tags=list(tags),
        attributes=None if attributes is None else dict(attributes),
    )
    manager.__enter__()
    try:
        yield LaminarSpanScope(enabled=True, creates_trace=creates_trace)
    except BaseException:
        manager.__exit__(None, None, None)
        raise
    else:
        manager.__exit__(None, None, None)


def finish_laminar_span(
    *,
    output: Any,
    outcome: LaminarSpanOutcome,
    attributes: Mapping[str, Any] | None = None,
    trace_metadata: Mapping[str, Any] | None = None,
) -> None:
    """Finalize the active span with bounded categorical status information."""

    if not Laminar.is_initialized():
        return

    Laminar.set_span_output(output)
    if attributes:
        Laminar.set_span_attributes(dict(attributes))
    Laminar.add_span_tags(["success" if outcome == "success" else "failed"])
    if trace_metadata is not None:
        Laminar.set_trace_metadata(dict(trace_metadata))

    span = Laminar.get_current_span()
    if span is None:
        return
    if outcome == "success":
        span.set_status(Status(StatusCode.OK))
    else:
        span.set_status(Status(StatusCode.ERROR, outcome))


def set_laminar_trace_metadata(metadata: Mapping[str, Any]) -> None:
    """Attach bounded application metadata to the active trace."""

    if Laminar.is_initialized():
        Laminar.set_trace_metadata(dict(metadata))


def add_laminar_span_tags(tags: Sequence[str]) -> None:
    """Add stable, low-cardinality tags to the active span."""

    if Laminar.is_initialized():
        Laminar.add_span_tags(list(tags))


__all__ = ["initialize_laminar_from_env"]
