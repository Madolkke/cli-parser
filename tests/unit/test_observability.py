"""Deterministic tests for optional Laminar initialization helpers."""

from __future__ import annotations

from contextlib import contextmanager
from types import SimpleNamespace
from typing import Any
from uuid import UUID

import pytest
from lmnr import Instruments, Laminar
from opentelemetry.trace import StatusCode

from cli_parser_agent import initialize_laminar_from_env, observability


def _force_uninitialized(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(Laminar, "is_initialized", lambda: False)


def test_missing_or_blank_project_key_leaves_laminar_disabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _force_uninitialized(monkeypatch)
    monkeypatch.setattr(
        Laminar,
        "initialize",
        lambda **_: pytest.fail("Laminar must not initialize without a key"),
    )

    assert initialize_laminar_from_env({}) is False
    assert (
        initialize_laminar_from_env(
            {
                "LMNR_PROJECT_API_KEY": " \t ",
                "LMNR_BASE_URL": "https://laminar.example.test",
            },
        )
        is False
    )


def test_project_key_initializes_only_the_openai_instrument(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _force_uninitialized(monkeypatch)
    calls: list[dict[str, object]] = []
    monkeypatch.setattr(Laminar, "initialize", lambda **kwargs: calls.append(kwargs))

    assert (
        initialize_laminar_from_env(
            {"LMNR_PROJECT_API_KEY": "  project-key  "},
        )
        is True
    )

    assert calls == [
        {
            "project_api_key": "project-key",
            "base_url": None,
            "instruments": {Instruments.OPENAI},
        },
    ]


def test_custom_base_url_is_read_from_the_supplied_mapping(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _force_uninitialized(monkeypatch)
    calls: list[dict[str, object]] = []
    monkeypatch.setenv("LMNR_PROJECT_API_KEY", "process-key")
    monkeypatch.setenv("LMNR_BASE_URL", "https://process.example.test")
    monkeypatch.setattr(Laminar, "initialize", lambda **kwargs: calls.append(kwargs))

    assert (
        initialize_laminar_from_env(
            {
                "LMNR_PROJECT_API_KEY": "mapping-key",
                "LMNR_BASE_URL": "  https://mapping.example.test  ",
            },
        )
        is True
    )

    assert calls == [
        {
            "project_api_key": "mapping-key",
            "base_url": "https://mapping.example.test",
            "instruments": {Instruments.OPENAI},
        },
    ]


def test_self_hosted_ports_are_forwarded_to_laminar(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _force_uninitialized(monkeypatch)
    calls: list[dict[str, object]] = []
    monkeypatch.setattr(Laminar, "initialize", lambda **kwargs: calls.append(kwargs))

    assert (
        initialize_laminar_from_env(
            {
                "LMNR_PROJECT_API_KEY": "project-key",
                "LMNR_BASE_URL": "http://127.0.0.1",
                "LMNR_HTTP_PORT": "8000",
                "LMNR_GRPC_PORT": "8001",
            },
        )
        is True
    )

    assert calls == [
        {
            "project_api_key": "project-key",
            "base_url": "http://127.0.0.1",
            "http_port": 8000,
            "grpc_port": 8001,
            "instruments": {Instruments.OPENAI},
        },
    ]


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("LMNR_HTTP_PORT", ""),
        ("LMNR_HTTP_PORT", " 8000 "),
        ("LMNR_HTTP_PORT", "+8000"),
        ("LMNR_HTTP_PORT", "0"),
        ("LMNR_GRPC_PORT", "65536"),
        ("LMNR_GRPC_PORT", "\u0668\u0660\u0660\u0661"),
    ],
)
def test_invalid_self_hosted_port_is_rejected(
    monkeypatch: pytest.MonkeyPatch,
    name: str,
    value: str,
) -> None:
    _force_uninitialized(monkeypatch)
    monkeypatch.setattr(
        Laminar,
        "initialize",
        lambda **_: pytest.fail("invalid ports must fail before initialization"),
    )

    with pytest.raises(ValueError, match=rf"^{name} must be"):
        initialize_laminar_from_env(
            {
                "LMNR_PROJECT_API_KEY": "project-key",
                name: value,
            },
        )


def test_explicit_mapping_never_falls_back_to_process_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _force_uninitialized(monkeypatch)
    monkeypatch.setenv("LMNR_PROJECT_API_KEY", "process-key")
    monkeypatch.setattr(
        Laminar,
        "initialize",
        lambda **_: pytest.fail("explicit empty mapping must disable Laminar"),
    )

    assert initialize_laminar_from_env({}) is False


def test_existing_initialization_wins_without_reconfiguration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(Laminar, "is_initialized", lambda: True)
    monkeypatch.setattr(
        Laminar,
        "initialize",
        lambda **_: pytest.fail("existing Laminar configuration must be retained"),
    )

    assert initialize_laminar_from_env({}) is True


def test_initialization_error_is_propagated(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _force_uninitialized(monkeypatch)

    def fail_initialize(**_: object) -> None:
        raise RuntimeError("initialization failed")

    monkeypatch.setattr(Laminar, "initialize", fail_initialize)

    with pytest.raises(RuntimeError, match="initialization failed"):
        initialize_laminar_from_env({"LMNR_PROJECT_API_KEY": "project-key"})


def test_current_trace_id_is_exposed_as_a_framework_neutral_string(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    trace_id = UUID("01234567-89ab-cdef-0123-456789abcdef")
    monkeypatch.setattr(Laminar, "is_initialized", lambda: True)
    monkeypatch.setattr(Laminar, "get_trace_id", lambda: trace_id)

    assert observability.current_laminar_trace_id() == str(trace_id)


def test_trace_helpers_are_noops_when_laminar_is_disabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _force_uninitialized(monkeypatch)
    monkeypatch.setattr(
        Laminar,
        "get_trace_id",
        lambda: pytest.fail("disabled tracing must not inspect the span"),
    )
    monkeypatch.setattr(
        Laminar,
        "set_trace_metadata",
        lambda _: pytest.fail("disabled tracing must not write metadata"),
    )
    monkeypatch.setattr(
        Laminar,
        "add_span_tags",
        lambda _: pytest.fail("disabled tracing must not add tags"),
    )

    assert observability.current_laminar_trace_id() is None
    observability.set_laminar_trace_metadata({"status": "success"})
    observability.add_laminar_span_tags(["success"])


def test_manual_span_detects_a_new_trace_before_starting(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[dict[str, Any]] = []

    @contextmanager
    def start(name: str, **kwargs: Any) -> Any:
        kwargs["name"] = name
        calls.append(kwargs)
        yield object()

    monkeypatch.setattr(Laminar, "is_initialized", lambda: True)
    monkeypatch.setattr(Laminar, "start_as_current_span", start)
    monkeypatch.setattr(observability, "current_laminar_trace_id", lambda: None)
    monkeypatch.setattr(observability.otel_context, "get_current", lambda: object())
    monkeypatch.setattr(
        observability.otel_trace,
        "get_current_span",
        lambda **_: SimpleNamespace(
            get_span_context=lambda: SimpleNamespace(is_valid=False),
        ),
    )

    with observability.start_laminar_span(
        "ttp.generate",
        input={"command_outputs": ["value: one"]},
        tags=("ttp-generation",),
    ) as scope:
        assert scope.enabled is True
        assert scope.creates_trace is True

    assert calls[0]["name"] == "ttp.generate"
    assert calls[0]["context"] is None


def test_manual_span_preserves_an_upstream_opentelemetry_trace(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[dict[str, Any]] = []
    upstream_context = object()

    @contextmanager
    def start(name: str, **kwargs: Any) -> Any:
        kwargs["name"] = name
        calls.append(kwargs)
        yield object()

    monkeypatch.setattr(Laminar, "is_initialized", lambda: True)
    monkeypatch.setattr(Laminar, "start_as_current_span", start)
    monkeypatch.setattr(observability, "current_laminar_trace_id", lambda: None)
    monkeypatch.setattr(
        observability.otel_context,
        "get_current",
        lambda: upstream_context,
    )
    monkeypatch.setattr(
        observability.otel_trace,
        "get_current_span",
        lambda **_: SimpleNamespace(
            get_span_context=lambda: SimpleNamespace(is_valid=True),
        ),
    )

    with observability.start_laminar_span("ttp.generate") as scope:
        assert scope.creates_trace is False

    assert calls[0]["context"] is upstream_context


def test_manual_span_hides_application_exceptions_from_the_sdk_context(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    exits: list[tuple[object, object, object]] = []

    class Manager:
        def __enter__(self) -> object:
            return object()

        def __exit__(self, *args: object) -> None:
            exits.append(args)

    monkeypatch.setattr(Laminar, "is_initialized", lambda: True)
    monkeypatch.setattr(
        Laminar,
        "start_as_current_span",
        lambda *_, **__: Manager(),
    )
    monkeypatch.setattr(observability, "current_laminar_trace_id", lambda: None)
    monkeypatch.setattr(
        observability.otel_trace,
        "get_current_span",
        lambda **_: SimpleNamespace(
            get_span_context=lambda: SimpleNamespace(is_valid=False),
        ),
    )

    with (
        pytest.raises(RuntimeError, match="private failure text"),
        observability.start_laminar_span("ttp.generate"),
    ):
        raise RuntimeError("private failure text")

    assert exits == [(None, None, None)]


@pytest.mark.parametrize(
    ("outcome", "expected_status", "expected_description", "expected_tag"),
    [
        ("success", StatusCode.OK, None, "success"),
        ("failed", StatusCode.ERROR, "failed", "failed"),
        ("cancelled", StatusCode.ERROR, "cancelled", "failed"),
        ("exception", StatusCode.ERROR, "exception", "failed"),
    ],
)
def test_finish_manual_span_records_status_without_exception_text(
    monkeypatch: pytest.MonkeyPatch,
    outcome: observability.LaminarSpanOutcome,
    expected_status: StatusCode,
    expected_description: str | None,
    expected_tag: str,
) -> None:
    outputs: list[Any] = []
    attributes: list[dict[str, Any]] = []
    tags: list[list[str]] = []
    metadata: list[dict[str, Any]] = []
    statuses: list[Any] = []
    span = SimpleNamespace(set_status=lambda value: statuses.append(value))
    monkeypatch.setattr(Laminar, "is_initialized", lambda: True)
    monkeypatch.setattr(Laminar, "set_span_output", outputs.append)
    monkeypatch.setattr(Laminar, "set_span_attributes", attributes.append)
    monkeypatch.setattr(Laminar, "add_span_tags", tags.append)
    monkeypatch.setattr(Laminar, "set_trace_metadata", metadata.append)
    monkeypatch.setattr(Laminar, "get_current_span", lambda: span)

    observability.finish_laminar_span(
        output={"exception_type": "RuntimeError"},
        outcome=outcome,
        attributes={"status": outcome},
        trace_metadata={"request_id": "request-1"},
    )

    assert outputs == [{"exception_type": "RuntimeError"}]
    assert attributes == [{"status": outcome}]
    assert tags == [[expected_tag]]
    assert metadata == [{"request_id": "request-1"}]
    assert statuses[0].status_code is expected_status
    assert statuses[0].description == expected_description
