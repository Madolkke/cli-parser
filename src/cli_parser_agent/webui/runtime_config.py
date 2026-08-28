"""Per-run WebUI configuration and safe configuration projections."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator

from ..config import GenerationPolicy, TtpGeneratorSettings


class SettingsOverrides(BaseModel):
    """Optional model-setting overrides accepted by one WebUI run."""

    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    api_key: str | None = None
    model_name: str | None = None
    base_url: str | None = None
    verify_tls: bool | None = None
    stream: bool | None = None
    temperature: float | None = Field(default=None, ge=0.0, le=2.0)
    parallel_tool_calls: bool | None = None
    thinking_enable: bool | None = None
    reasoning_effort: Literal[
        "none",
        "minimal",
        "low",
        "medium",
        "high",
        "xhigh",
    ] | None = None
    max_tokens: int | None = Field(default=None, ge=1)
    context_size: int | None = Field(default=None, ge=1)
    model_max_retries: int | None = Field(default=None, ge=0)
    model_timeout_seconds: float | None = Field(default=None, gt=0)

    @field_validator("api_key", "model_name", "base_url", mode="before")
    @classmethod
    def empty_strings_are_inherited(cls, value: Any) -> Any:
        if isinstance(value, str):
            value = value.strip()
            return value or None
        return value


class PolicyOverrides(BaseModel):
    """Optional generation-policy overrides accepted by one WebUI run."""

    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    total_timeout_seconds: float | None = Field(default=None, gt=0)
    max_agent_rounds: int | None = Field(default=None, ge=1)
    max_ttp_submissions: int | None = Field(default=None, ge=1)
    max_schema_no_tool_retries: int | None = Field(default=None, ge=0)
    max_ttp_no_tool_retries: int | None = Field(default=None, ge=0)
    ttp_validation_timeout_seconds: float | None = Field(default=None, gt=0)
    model_input_char_budget: int | None = Field(default=None, ge=1, le=240_000)
    max_ttp_template_bytes: int | None = Field(default=None, ge=1, le=64 * 1024)
    max_ttp_group_depth: int | None = Field(default=None, ge=1, le=16)
    max_ttp_regex_chars: int | None = Field(default=None, ge=1, le=2_048)
    max_ttp_argument_chars: int | None = Field(default=None, ge=1, le=4_096)
    max_parse_result_bytes: int | None = Field(
        default=None,
        ge=1,
        le=8 * 1024 * 1024,
    )
    max_schema_bytes: int | None = Field(default=None, ge=1, le=64 * 1024)
    max_schema_depth: int | None = Field(default=None, ge=1, le=16)
    max_schema_properties: int | None = Field(default=None, ge=1, le=256)


class RuntimeParameters(BaseModel):
    """Optional per-run settings and policy overrides."""

    model_config = ConfigDict(extra="forbid")

    settings: SettingsOverrides | None = None
    policy: PolicyOverrides | None = None


@dataclass(frozen=True)
class ResolvedRuntimeConfig:
    """Validated immutable configuration used by one generation task."""

    settings: TtpGeneratorSettings
    policy: GenerationPolicy
    source: str


class RuntimeConfigError(ValueError):
    """A WebUI configuration override cannot be resolved safely."""


def resolve_runtime_config(
    base_settings: TtpGeneratorSettings,
    base_policy: GenerationPolicy,
    overrides: RuntimeParameters | None = None,
) -> ResolvedRuntimeConfig:
    """Merge explicit overrides into the startup baseline and revalidate."""

    settings_values = _settings_values(base_settings)
    policy_values = base_policy.model_dump(mode="python")
    settings_overrides = overrides.settings if overrides else None
    policy_overrides = overrides.policy if overrides else None

    if settings_overrides is not None:
        explicit = settings_overrides.model_dump(exclude_unset=True, mode="python")
        if explicit.get("parallel_tool_calls") is True:
            raise RuntimeConfigError(
                "parallel_tool_calls must remain false for the WebUI Agent protocol",
            )
        settings_values.update(
            {key: value for key, value in explicit.items() if value is not None},
        )
    if policy_overrides is not None:
        policy_values.update(
            {
                key: value
                for key, value in policy_overrides.model_dump(
                    exclude_unset=True,
                    mode="python",
                ).items()
                if value is not None
            },
        )

    # The protocol-critical setting cannot be changed through either the API or
    # a malformed baseline object supplied by a test/integration adapter.
    settings_values["parallel_tool_calls"] = False
    try:
        settings = TtpGeneratorSettings.model_validate(settings_values)
        policy = GenerationPolicy.model_validate(policy_values)
    except (TypeError, ValueError) as error:
        raise RuntimeConfigError(str(error)) from error
    source = (
        "env_baseline"
        if not overrides or not _has_overrides(overrides)
        else "env_baseline+overrides"
    )
    return ResolvedRuntimeConfig(settings=settings, policy=policy, source=source)


def full_config_payload(config: ResolvedRuntimeConfig) -> dict[str, Any]:
    """Serialize the exact local snapshot, including the explicitly allowed Key."""

    settings = _settings_values(config.settings)
    return {
        "version": 1,
        "source": config.source,
        "settings": settings,
        "policy": config.policy.model_dump(mode="json"),
    }


def public_config_payload(config: ResolvedRuntimeConfig) -> dict[str, Any]:
    """Return a browser-safe configuration projection with no credential value."""

    payload = full_config_payload(config)
    return public_config_snapshot(payload)


def public_config_snapshot(payload: dict[str, Any]) -> dict[str, Any]:
    """Redact one persisted configuration before returning it to a client."""

    settings = dict(payload["settings"])
    api_key = str(settings.pop("api_key", ""))
    settings["api_key_configured"] = bool(api_key)
    settings["parallel_tool_calls"] = False
    safe = {
        "version": payload["version"],
        "source": payload["source"],
        "settings": settings,
        "policy": payload["policy"],
    }
    safe["configuration_fingerprint"] = configuration_fingerprint(safe)
    return safe


def configuration_fingerprint(public_payload: dict[str, Any]) -> str:
    """Fingerprint a redacted configuration without hashing the API Key."""

    encoded = json.dumps(
        public_payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _settings_values(settings: TtpGeneratorSettings) -> dict[str, Any]:
    values = settings.model_dump(mode="python")
    api_key = values.get("api_key")
    if isinstance(api_key, SecretStr):
        values["api_key"] = api_key.get_secret_value()
    return values


def _has_overrides(overrides: RuntimeParameters) -> bool:
    return bool(
        (overrides.settings and overrides.settings.model_fields_set)
        or (overrides.policy and overrides.policy.model_fields_set),
    )


__all__ = [
    "PolicyOverrides",
    "ResolvedRuntimeConfig",
    "RuntimeConfigError",
    "RuntimeParameters",
    "SettingsOverrides",
    "configuration_fingerprint",
    "full_config_payload",
    "public_config_payload",
    "public_config_snapshot",
    "resolve_runtime_config",
]
