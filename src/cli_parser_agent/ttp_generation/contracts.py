"""Framework-independent request and result contracts."""

from __future__ import annotations

import re
from typing import Literal, Self
from uuid import uuid4

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    JsonValue,
    field_validator,
    model_validator,
)

MAX_COMMAND_OUTPUTS = 5
MAX_COMMAND_OUTPUT_BYTES = 1024 * 1024

GenerationStatus = Literal["success", "failed"]
IssueSeverity = Literal["error", "warning"]

_ISSUE_CODE_RE = re.compile(r"^[a-z][a-z0-9_.-]*$")
_FIELD_SEGMENT_RE = re.compile(r"^[a-z][a-z0-9_]*$")


class ContractModel(BaseModel):
    """Common behavior for stable application contracts."""

    model_config = ConfigDict(extra="forbid")


class GenerationRequest(ContractModel):
    """One to five full outputs produced by the same command."""

    command_outputs: list[str] = Field(min_length=1, max_length=MAX_COMMAND_OUTPUTS)

    @field_validator("command_outputs")
    @classmethod
    def outputs_are_nonempty_and_bounded(cls, values: list[str]) -> list[str]:
        for index, value in enumerate(values):
            if not value.strip():
                raise ValueError(f"command_outputs[{index}] must not be empty")
            try:
                encoded_size = len(value.encode("utf-8"))
            except UnicodeEncodeError as exc:
                raise ValueError(
                    f"command_outputs[{index}] is not valid UTF-8 text",
                ) from exc
            if encoded_size > MAX_COMMAND_OUTPUT_BYTES:
                raise ValueError(
                    f"command_outputs[{index}] exceeds "
                    f"{MAX_COMMAND_OUTPUT_BYTES} UTF-8 bytes",
                )
        return values


def _validate_root_object_schema(schema: dict[str, JsonValue]) -> dict[str, JsonValue]:
    if schema.get("type") != "object":
        raise ValueError("result_schema root type must be 'object'")
    return schema


class FieldEvidence(ContractModel):
    """Source evidence for one inferred leaf field in a result schema.

    ``path`` is a JSON-path-like pointer whose object keys are ASCII snake_case and
    whose array positions are represented by ``*`` (for example,
    ``/interfaces/*/name``).
    """

    path: str = Field(min_length=2, max_length=2_048)
    output_index: int = Field(ge=0, lt=MAX_COMMAND_OUTPUTS)
    excerpt: str = Field(min_length=1, max_length=4_096)

    @field_validator("path")
    @classmethod
    def path_uses_supported_segments(cls, value: str) -> str:
        if not value.startswith("/") or value.endswith("/"):
            raise ValueError("evidence path must start with '/' and not end with '/'")
        segments = value[1:].split("/")
        if segments[0] == "*":
            raise ValueError("evidence path must begin with an object field")
        if any(
            segment != "*" and not _FIELD_SEGMENT_RE.fullmatch(segment)
            for segment in segments
        ):
            raise ValueError(
                "evidence path segments must be '*' or ASCII snake_case field names",
            )
        return value


class SchemaSubmission(ContractModel):
    """Internal structured payload accepted by the schema submission tool."""

    result_schema: dict[str, JsonValue]
    evidence: list[FieldEvidence] = Field(min_length=1, max_length=256)
    assumptions: list[str] = Field(default_factory=list)

    _root_schema = field_validator("result_schema")(_validate_root_object_schema)


class ArtifactBundle(ContractModel):
    """A fully validated TTP parser and its actual output contract."""

    ttp_template: str = Field(min_length=1)
    result_schema: dict[str, JsonValue]
    records: list[dict[str, JsonValue]] = Field(min_length=1)
    assumptions: list[str] = Field(default_factory=list)

    _root_schema = field_validator("result_schema")(_validate_root_object_schema)


class ValidationIssue(ContractModel):
    """A stable, safe-to-return description of a validation failure or warning."""

    code: str = Field(min_length=1, max_length=128)
    message: str = Field(min_length=1, max_length=4_096)
    severity: IssueSeverity = "error"
    stage: str | None = Field(default=None, max_length=64)
    path: str | None = Field(default=None, max_length=2_048)
    output_index: int | None = Field(default=None, ge=0, lt=MAX_COMMAND_OUTPUTS)
    details: dict[str, JsonValue] = Field(default_factory=dict)

    @field_validator("code")
    @classmethod
    def code_is_machine_readable(cls, value: str) -> str:
        if not _ISSUE_CODE_RE.fullmatch(value):
            raise ValueError(
                "code must start with a letter and use lowercase ASCII tokens",
            )
        return value


class LastAttempt(ContractModel):
    """The latest candidate retained for diagnostics after a failed generation."""

    ttp_template: str | None = None
    result_schema: dict[str, JsonValue] | None = None
    validated: Literal[False] = False

    @model_validator(mode="after")
    def contains_a_candidate(self) -> Self:
        if self.ttp_template is None and self.result_schema is None:
            raise ValueError("last_attempt must contain a template or schema candidate")
        return self


class GenerationMetadata(ContractModel):
    """Application-observed execution facts; never populated from model claims."""

    request_id: str = Field(default_factory=lambda: str(uuid4()), min_length=1)
    model_name: str = Field(min_length=1)
    prompt_version: str = Field(default="v1", min_length=1)
    command_output_count: int = Field(ge=1, le=MAX_COMMAND_OUTPUTS)
    input_char_count: int = Field(default=0, ge=0)
    sampled_char_count: int = Field(default=0, ge=0)
    elapsed_seconds: float = Field(default=0.0, ge=0.0)
    agent_rounds: int = Field(default=0, ge=0)
    tool_call_starts: int = Field(default=0, ge=0)
    tool_result_errors: int = Field(default=0, ge=0)
    schema_submissions: int = Field(default=0, ge=0)
    ttp_submissions: int = Field(default=0, ge=0)
    schema_no_tool_responses: int = Field(default=0, ge=0)
    ttp_no_tool_responses: int = Field(default=0, ge=0)
    schema_no_tool_retries: int = Field(default=0, ge=0)
    ttp_no_tool_retries: int = Field(default=0, ge=0)
    first_ttp_passed: bool | None = None
    termination_reason: str | None = Field(default=None, max_length=128)

    @model_validator(mode="after")
    def sampled_count_does_not_exceed_input(self) -> Self:
        if self.sampled_char_count > self.input_char_count:
            raise ValueError("sampled_char_count cannot exceed input_char_count")
        return self


class GenerationResult(ContractModel):
    """Unified success/failure return value for the asynchronous generator API."""

    status: GenerationStatus
    artifact: ArtifactBundle | None = None
    issues: list[ValidationIssue] = Field(default_factory=list)
    metadata: GenerationMetadata
    last_attempt: LastAttempt | None = None

    @model_validator(mode="after")
    def status_matches_payload(self) -> Self:
        if self.status == "success":
            if self.artifact is None:
                raise ValueError("a successful result requires artifact")
            if self.last_attempt is not None:
                raise ValueError("a successful result cannot contain last_attempt")
            if any(issue.severity == "error" for issue in self.issues):
                raise ValueError("a successful result cannot contain error issues")
            if len(self.artifact.records) != self.metadata.command_output_count:
                raise ValueError(
                    "artifact records must correspond one-to-one with command outputs",
                )
        else:
            if self.artifact is not None:
                raise ValueError("a failed result cannot contain a validated artifact")
            if not any(issue.severity == "error" for issue in self.issues):
                raise ValueError("a failed result requires at least one error issue")
        return self


# A shorter public spelling without weakening the descriptive class name.
Metadata = GenerationMetadata


__all__ = [
    "ArtifactBundle",
    "FieldEvidence",
    "GenerationMetadata",
    "GenerationRequest",
    "GenerationResult",
    "GenerationStatus",
    "IssueSeverity",
    "LastAttempt",
    "MAX_COMMAND_OUTPUT_BYTES",
    "MAX_COMMAND_OUTPUTS",
    "Metadata",
    "SchemaSubmission",
    "ValidationIssue",
]
