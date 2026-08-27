"""Tests for WebUI per-run configuration resolution and redaction."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from cli_parser_agent.config import GenerationPolicy, TtpGeneratorSettings
from cli_parser_agent.webui.runtime_config import (
    RuntimeConfigError,
    RuntimeParameters,
    full_config_payload,
    public_config_payload,
    resolve_runtime_config,
)


def _baseline() -> tuple[TtpGeneratorSettings, GenerationPolicy]:
    return (
        TtpGeneratorSettings(
            api_key="baseline-key",
            model_name="baseline-model",
            base_url="https://model.invalid/v1",
            stream=True,
        ),
        GenerationPolicy(),
    )


def test_empty_or_missing_overrides_use_the_startup_baseline() -> None:
    settings, policy = _baseline()

    resolved = resolve_runtime_config(settings, policy, RuntimeParameters())

    assert resolved.source == "env_baseline"
    assert resolved.settings == settings
    assert resolved.policy == policy


def test_standard_settings_and_policy_are_overridden_and_revalidated() -> None:
    settings, policy = _baseline()
    overrides = RuntimeParameters.model_validate(
        {
            "settings": {
                "api_key": "run-key",
                "model_name": "run-model",
                "temperature": 0.4,
                "max_tokens": 4096,
            },
            "policy": {
                "total_timeout_seconds": 1200,
                "max_agent_rounds": 24,
            },
        },
    )

    resolved = resolve_runtime_config(settings, policy, overrides)

    assert resolved.source == "env_baseline+overrides"
    assert resolved.settings.api_key.get_secret_value() == "run-key"
    assert resolved.settings.model_name == "run-model"
    assert resolved.settings.temperature == 0.4
    assert resolved.settings.max_tokens == 4096
    assert resolved.policy.total_timeout_seconds == 1200
    assert resolved.policy.max_agent_rounds == 24


def test_protocol_and_environment_only_fields_are_not_per_run_overrides() -> None:
    settings, policy = _baseline()

    with pytest.raises(RuntimeConfigError, match="parallel_tool_calls"):
        resolve_runtime_config(
            settings,
            policy,
            RuntimeParameters.model_validate(
                {"settings": {"parallel_tool_calls": True}},
            ),
        )

    with pytest.raises(ValidationError):
        RuntimeParameters.model_validate(
            {"settings": {"extra_body": {"temperature": 0.1}}},
        )


def test_full_snapshot_contains_key_but_public_projection_does_not() -> None:
    settings, policy = _baseline()
    resolved = resolve_runtime_config(settings, policy)

    full = full_config_payload(resolved)
    public = public_config_payload(resolved)

    assert full["settings"]["api_key"] == "baseline-key"
    assert "api_key" not in public["settings"]
    assert public["settings"]["api_key_configured"] is True
    assert "baseline-key" not in str(public)
    assert len(public["configuration_fingerprint"]) == 64


def test_invalid_merged_constraints_are_rejected() -> None:
    settings, policy = _baseline()

    with pytest.raises(RuntimeConfigError, match="max_tokens"):
        resolve_runtime_config(
            settings,
            policy,
            RuntimeParameters.model_validate(
                {"settings": {"max_tokens": 200_000, "context_size": 100_000}},
            ),
        )

    with pytest.raises(RuntimeConfigError, match="ttp_validation_timeout_seconds"):
        resolve_runtime_config(
            settings,
            policy,
            RuntimeParameters.model_validate(
                {"policy": {"total_timeout_seconds": 10}},
            ),
        )
