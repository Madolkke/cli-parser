"""Configuration models for the CLI parser generator."""

from __future__ import annotations

import hashlib
import json
import math
import os
import ssl
from collections.abc import Mapping
from typing import Any, Literal, Self
from urllib.parse import urlparse

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    JsonValue,
    SecretStr,
    field_validator,
    model_validator,
)

INSECURE_SKIP_TLS_VERIFY_ENV = "CLI_PARSER_INSECURE_SKIP_TLS_VERIFY"
_TRUE_ENV_VALUES = frozenset({"1", "true", "yes", "on"})
_FALSE_ENV_VALUES = frozenset({"0", "false", "no", "off"})
_SENSITIVE_EXTRA_BODY_KEYS = frozenset(
    {
        "api_key",
        "authorization",
        "client_secret",
        "credential",
        "password",
        "private_key",
        "secret",
        "token",
    },
)


def _validate_extra_body_value(value: Any, *, path: str = "extra_body") -> None:
    """Reject non-JSON values and credential-shaped keys recursively."""

    if value is None or isinstance(value, str | bool | int):
        return
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError(f"{path} must not contain non-finite numbers")
        return
    if isinstance(value, list):
        for index, item in enumerate(value):
            _validate_extra_body_value(item, path=f"{path}[{index}]")
        return
    if isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str):
                raise ValueError(f"{path} keys must be strings")
            normalized_key = key.casefold().replace("-", "_")
            if normalized_key in _SENSITIVE_EXTRA_BODY_KEYS or any(
                normalized_key.endswith(f"_{suffix}")
                for suffix in ("api_key", "credential", "password", "secret", "token")
            ):
                raise ValueError(f"{path} must not contain credential fields")
            _validate_extra_body_value(item, path=f"{path}.{key}")
        return
    raise ValueError(f"{path} must contain only JSON-compatible values")


def model_extra_body_sha256(extra_body: Mapping[str, JsonValue] | None) -> str:
    """Return a stable, content-free fingerprint for a model extra body."""

    if extra_body is None:
        return ""
    encoded = json.dumps(
        extra_body,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def tls_verification_enabled(environ: Mapping[str, str] | None = None) -> bool:
    """Return whether outbound HTTPS clients must verify server certificates."""

    source = os.environ if environ is None else environ
    raw_value = source.get(INSECURE_SKIP_TLS_VERIFY_ENV)
    if raw_value is None or not raw_value.strip():
        return True

    value = raw_value.strip().lower()
    if value in _TRUE_ENV_VALUES:
        return False
    if value in _FALSE_ENV_VALUES:
        return True
    raise ValueError(
        f"{INSECURE_SKIP_TLS_VERIFY_ENV} must be one of "
        "1, true, yes, on, 0, false, no, or off",
    )


def tls_ssl_context(*, verify_tls: bool) -> ssl.SSLContext | None:
    """Return an explicit unverified context only for opted-in compatibility."""

    if verify_tls:
        return None
    context = ssl.create_default_context()
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE
    return context


class GenerationPolicy(BaseModel):
    """Request execution and untrusted-input resource limits."""

    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)

    total_timeout_seconds: float = Field(default=900.0, gt=0)
    max_agent_rounds: int = Field(default=13, ge=1)
    max_ttp_submissions: int = Field(default=9, ge=1)
    max_schema_no_tool_retries: int = Field(default=3, ge=0)
    max_ttp_no_tool_retries: int = Field(default=3, ge=0)
    ttp_validation_timeout_seconds: float = Field(default=20.0, gt=0)

    model_input_char_budget: int = Field(default=240_000, ge=1, le=240_000)
    # These values are configurable downward only. The defaults are the
    # implementation's audited hard ceilings for untrusted generated input.
    max_ttp_template_bytes: int = Field(default=64 * 1024, ge=1, le=64 * 1024)
    max_ttp_group_depth: int = Field(default=16, ge=1, le=16)
    max_ttp_regex_chars: int = Field(default=2_048, ge=1, le=2_048)
    max_ttp_argument_chars: int = Field(default=4_096, ge=1, le=4_096)
    max_parse_result_bytes: int = Field(
        default=8 * 1024 * 1024,
        ge=1,
        le=8 * 1024 * 1024,
    )

    max_schema_bytes: int = Field(default=64 * 1024, ge=1, le=64 * 1024)
    max_schema_depth: int = Field(default=16, ge=1, le=16)
    max_schema_properties: int = Field(default=256, ge=1, le=256)

    @model_validator(mode="after")
    def validation_timeout_fits_total_budget(self) -> Self:
        if self.ttp_validation_timeout_seconds > self.total_timeout_seconds:
            raise ValueError(
                "ttp_validation_timeout_seconds cannot exceed total_timeout_seconds",
            )
        return self

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> Self:
        """Load execution-budget overrides while retaining safety defaults."""

        source = os.environ if environ is None else environ
        names = {
            "total_timeout_seconds": "CLI_PARSER_GENERATION_TIMEOUT_SECONDS",
            "max_agent_rounds": "CLI_PARSER_MAX_AGENT_ITERS",
            "max_ttp_submissions": "CLI_PARSER_MAX_TEMPLATE_SUBMISSIONS",
            "max_schema_no_tool_retries": "CLI_PARSER_MAX_SCHEMA_NO_TOOL_RETRIES",
            "max_ttp_no_tool_retries": "CLI_PARSER_MAX_TTP_NO_TOOL_RETRIES",
            "ttp_validation_timeout_seconds": (
                "CLI_PARSER_TTP_VALIDATION_TIMEOUT_SECONDS"
            ),
            "model_input_char_budget": "CLI_PARSER_MODEL_INPUT_CHAR_BUDGET",
            "max_ttp_template_bytes": "CLI_PARSER_MAX_TTP_TEMPLATE_BYTES",
            "max_ttp_group_depth": "CLI_PARSER_MAX_TTP_GROUP_DEPTH",
            "max_ttp_regex_chars": "CLI_PARSER_MAX_TTP_REGEX_CHARS",
            "max_ttp_argument_chars": "CLI_PARSER_MAX_TTP_ARGUMENT_CHARS",
            "max_parse_result_bytes": "CLI_PARSER_MAX_PARSE_RESULT_BYTES",
            "max_schema_bytes": "CLI_PARSER_MAX_SCHEMA_BYTES",
            "max_schema_depth": "CLI_PARSER_MAX_SCHEMA_DEPTH",
            "max_schema_properties": "CLI_PARSER_MAX_SCHEMA_PROPERTIES",
        }
        overrides = {
            field_name: source[environment_name]
            for field_name, environment_name in names.items()
            if environment_name in source
        }
        return cls.model_validate(overrides)


class TtpGeneratorSettings(BaseModel):
    """OpenAI-compatible model settings.

    Secrets are intentionally represented by ``SecretStr`` so diagnostics and model
    dumps do not expose credentials by default.
    """

    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)

    api_key: SecretStr
    model_name: str
    base_url: str | None = None
    verify_tls: bool = Field(default_factory=tls_verification_enabled)

    stream: bool = False
    temperature: float = Field(default=0.0, ge=0.0, le=2.0)
    parallel_tool_calls: bool = False
    thinking_enable: bool | None = None
    reasoning_effort: Literal[
        "none",
        "minimal",
        "low",
        "medium",
        "high",
        "xhigh",
    ] | None = None
    extra_body: dict[str, JsonValue] | None = None
    max_tokens: int = Field(default=8_192, ge=1)
    context_size: int = Field(default=128_000, ge=1)
    model_max_retries: int = Field(default=2, ge=0)
    model_timeout_seconds: float = Field(default=60.0, gt=0)

    @field_validator("api_key")
    @classmethod
    def api_key_is_not_empty(cls, value: SecretStr) -> SecretStr:
        if not value.get_secret_value().strip():
            raise ValueError("api_key must not be empty")
        return value

    @field_validator("model_name")
    @classmethod
    def model_name_is_not_empty(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("model_name must not be empty")
        return value

    @field_validator("base_url")
    @classmethod
    def base_url_is_http(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = value.strip()
        if not value:
            return None
        parsed = urlparse(value)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError("base_url must be an absolute HTTP(S) URL")
        return value.rstrip("/")

    @field_validator("extra_body", mode="before")
    @classmethod
    def extra_body_is_safe_json_object(cls, value: Any) -> Any:
        if value is None:
            return None
        if not isinstance(value, dict):
            raise ValueError("extra_body must be a JSON object")
        _validate_extra_body_value(value)
        return value

    @model_validator(mode="after")
    def completion_fits_context(self) -> Self:
        if self.max_tokens > self.context_size:
            raise ValueError("max_tokens cannot exceed context_size")
        return self

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> Self:
        """Load the required provider values from an environment mapping."""

        source = os.environ if environ is None else environ
        names = {
            "stream": "CLI_PARSER_MODEL_STREAM",
            "temperature": "CLI_PARSER_MODEL_TEMPERATURE",
            "parallel_tool_calls": "CLI_PARSER_MODEL_PARALLEL_TOOL_CALLS",
            "thinking_enable": "CLI_PARSER_MODEL_THINKING_ENABLE",
            "reasoning_effort": "CLI_PARSER_MODEL_REASONING_EFFORT",
            "max_tokens": "CLI_PARSER_MODEL_MAX_TOKENS",
            "context_size": "CLI_PARSER_MODEL_CONTEXT_SIZE",
            "model_max_retries": "CLI_PARSER_MODEL_MAX_RETRIES",
            "model_timeout_seconds": "CLI_PARSER_MODEL_TIMEOUT_SECONDS",
        }
        overrides = {
            field_name: source[environment_name]
            for field_name, environment_name in names.items()
            if environment_name in source
        }
        return cls(
            api_key=source.get("OPENAI_API_KEY"),
            model_name=source.get("OPENAI_MODEL"),
            base_url=source.get("OPENAI_BASE_URL"),
            verify_tls=tls_verification_enabled(source),
            **overrides,
        )
