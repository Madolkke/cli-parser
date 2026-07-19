"""Configuration models for the CLI parser generator."""

from __future__ import annotations

import os
from collections.abc import Mapping
from typing import Self
from urllib.parse import urlparse

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    SecretStr,
    field_validator,
    model_validator,
)


class GenerationPolicy(BaseModel):
    """Request execution and untrusted-input resource limits."""

    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)

    total_timeout_seconds: float = Field(default=300.0, gt=0)
    max_agent_rounds: int = Field(default=12, ge=1)
    max_ttp_submissions: int = Field(default=8, ge=1)
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
    max_evidence_excerpt_chars: int = Field(default=4_096, ge=1, le=4_096)

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
            "max_schema_no_tool_retries": ("CLI_PARSER_MAX_SCHEMA_NO_TOOL_RETRIES"),
            "max_ttp_no_tool_retries": "CLI_PARSER_MAX_TTP_NO_TOOL_RETRIES",
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

    stream: bool = False
    temperature: float = Field(default=0.0, ge=0.0, le=2.0)
    parallel_tool_calls: bool = False
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

    @model_validator(mode="after")
    def completion_fits_context(self) -> Self:
        if self.max_tokens > self.context_size:
            raise ValueError("max_tokens cannot exceed context_size")
        return self

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> Self:
        """Load the required provider values from an environment mapping."""

        source = os.environ if environ is None else environ
        return cls(
            api_key=source.get("OPENAI_API_KEY"),
            model_name=source.get("OPENAI_MODEL"),
            base_url=source.get("OPENAI_BASE_URL"),
        )
