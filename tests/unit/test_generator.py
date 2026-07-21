from __future__ import annotations

import asyncio
import time

import pytest
from pydantic import ValidationError

from cli_parser_agent import TtpGenerator, TtpGeneratorSettings
from cli_parser_agent.ttp_generation.agent import build_task_prompt
from cli_parser_agent.ttp_generation.generator import (
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
        return len(build_task_prompt(texts).encode("utf-8")) // 4

    sampled, fits = await _fit_sampled_outputs(
        outputs,
        total_char_budget=2_000,
        max_initial_tokens=300,
        estimate_tokens=estimate_tokens,
    )

    prompt = build_task_prompt([item.text for item in sampled])
    assert fits
    assert len(prompt) <= 2_000
    assert await estimate_tokens([item.text for item in sampled]) <= 300
    assert sum(item.sampled_char_count for item in sampled) < 2_000
