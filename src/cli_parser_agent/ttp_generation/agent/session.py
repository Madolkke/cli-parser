"""Framework-neutral request-local state for the two generation phases."""

from __future__ import annotations

import inspect
import time
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Literal, Protocol, TypeAlias

GenerationPhase = Literal["schema", "ttp"]


@dataclass(frozen=True, slots=True)
class SchemaCandidate:
    """Framework-neutral input passed to an injected schema validator."""

    result_schema: dict[str, Any]
    evidence: tuple[dict[str, Any], ...]
    assumptions: tuple[str, ...]
    command_outputs: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class TemplateCandidate:
    """Framework-neutral input passed to an injected TTP validator."""

    ttp_template: str
    result_schema: dict[str, Any]
    command_outputs: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ValidatorOutcome:
    """Normalized validator result understood by the AgentScope adapter."""

    valid: bool
    issues: tuple[Any, ...] = ()
    records: tuple[dict[str, Any], ...] = ()


class ValidatorOutcomeAttributes(Protocol):
    """Minimum attribute-based validator result accepted at runtime."""

    valid: bool


ValidatorOutcomeLike: TypeAlias = (
    ValidatorOutcome | Mapping[str, Any] | ValidatorOutcomeAttributes
)
ValidatorReturn: TypeAlias = ValidatorOutcomeLike | Awaitable[ValidatorOutcomeLike]


class SchemaValidator(Protocol):
    """Validate a proposed schema without depending on AgentScope."""

    def __call__(self, candidate: SchemaCandidate) -> ValidatorReturn: ...


class TemplateValidator(Protocol):
    """Validate a proposed template without depending on AgentScope."""

    def __call__(self, candidate: TemplateCandidate) -> ValidatorReturn: ...


@dataclass(slots=True)
class GenerationSession:
    """Mutable artifact and budget state owned by one generation request.

    AgentScope owns each phase's conversation state. This object is the only
    artifact channel shared by the otherwise isolated Schema and TTP agents.
    """

    command_outputs: tuple[str, ...] | Sequence[str]
    schema_validator: SchemaValidator
    template_validator: TemplateValidator
    max_ttp_submissions: int = 9
    max_agent_rounds: int = 13
    max_schema_no_tool_retries: int = 3
    max_ttp_no_tool_retries: int = 3
    deadline_monotonic: float | None = None

    frozen_schema: dict[str, Any] | None = None
    field_evidence: tuple[dict[str, Any], ...] = ()
    assumptions: tuple[str, ...] = ()
    last_result_schema: dict[str, Any] | None = None
    schema_submissions: int = 0
    ttp_submissions: int = 0
    agent_rounds: int = 0
    schema_agent_rounds: int = 0
    ttp_agent_rounds: int = 0
    tool_call_starts: int = 0
    tool_result_errors: int = 0
    submission_tool_call_invalids: int = 0
    schema_no_tool_responses: int = 0
    ttp_no_tool_responses: int = 0
    schema_no_tool_retries: int = 0
    ttp_no_tool_retries: int = 0
    _schema_consecutive_no_tool_responses: int = 0
    _ttp_consecutive_no_tool_responses: int = 0
    first_ttp_valid: bool | None = None
    last_ttp_template: str | None = None
    validated_ttp_template: str | None = None
    records: tuple[dict[str, Any], ...] = ()
    generation_finished: bool = False
    last_issues: tuple[Any, ...] = ()
    terminal_reason: str | None = None

    def __post_init__(self) -> None:
        self.command_outputs = tuple(self.command_outputs)
        if not self.command_outputs:
            raise ValueError("command_outputs must contain at least one item")
        if self.max_ttp_submissions < 1:
            raise ValueError("max_ttp_submissions must be positive")
        if self.max_agent_rounds < 1:
            raise ValueError("max_agent_rounds must be positive")
        if self.max_schema_no_tool_retries < 0:
            raise ValueError("max_schema_no_tool_retries must be non-negative")
        if self.max_ttp_no_tool_retries < 0:
            raise ValueError("max_ttp_no_tool_retries must be non-negative")

    @classmethod
    def create(
        cls,
        *,
        command_outputs: Sequence[str],
        schema_validator: SchemaValidator,
        template_validator: TemplateValidator,
        total_timeout_seconds: float = 360,
        max_ttp_submissions: int = 9,
    ) -> GenerationSession:
        """Create a session and start its wall-clock budget."""

        if total_timeout_seconds <= 0:
            raise ValueError("total_timeout_seconds must be positive")
        return cls(
            command_outputs=tuple(command_outputs),
            schema_validator=schema_validator,
            template_validator=template_validator,
            max_ttp_submissions=max_ttp_submissions,
            deadline_monotonic=(time.monotonic() + total_timeout_seconds),
        )

    @property
    def schema_is_frozen(self) -> bool:
        """Whether a valid schema has been accepted permanently."""

        return self.frozen_schema is not None

    @property
    def has_validated_ttp_candidate(self) -> bool:
        """Whether at least one validated TTP candidate is stored."""

        return self.validated_ttp_template is not None

    @property
    def succeeded(self) -> bool:
        """Whether the model explicitly finished with a validated candidate."""

        return self.generation_finished and self.has_validated_ttp_candidate

    def reset_no_tool_sequence(self, phase: GenerationPhase) -> None:
        """Reset the phase's consecutive no-tool response count."""

        if phase == "schema":
            self._schema_consecutive_no_tool_responses = 0
        elif phase == "ttp":
            self._ttp_consecutive_no_tool_responses = 0
        else:
            raise ValueError(f"unsupported generation phase: {phase!r}")

    def record_agent_round(self, phase: GenerationPhase) -> None:
        """Record one model round while preserving per-phase accounting."""

        if phase == "schema":
            self.schema_agent_rounds += 1
        elif phase == "ttp":
            self.ttp_agent_rounds += 1
        else:
            raise ValueError(f"unsupported generation phase: {phase!r}")
        self.agent_rounds = self.schema_agent_rounds + self.ttp_agent_rounds

    def record_no_tool_response(self, phase: GenerationPhase) -> bool:
        """Record a no-tool response and report whether a retry is allowed."""

        if phase == "schema":
            self.schema_no_tool_responses += 1
            self._schema_consecutive_no_tool_responses += 1
            return (
                self._schema_consecutive_no_tool_responses
                <= self.max_schema_no_tool_retries
            )
        if phase == "ttp":
            self.ttp_no_tool_responses += 1
            self._ttp_consecutive_no_tool_responses += 1
            return (
                self._ttp_consecutive_no_tool_responses <= self.max_ttp_no_tool_retries
            )
        raise ValueError(f"unsupported generation phase: {phase!r}")

    def record_no_tool_retry(self, phase: GenerationPhase) -> None:
        """Record an actual follow-up model request after a no-tool response."""

        if phase == "schema":
            self.schema_no_tool_retries += 1
        elif phase == "ttp":
            self.ttp_no_tool_retries += 1
        else:
            raise ValueError(f"unsupported generation phase: {phase!r}")


def normalize_validator_outcome(value: ValidatorOutcomeLike) -> ValidatorOutcome:
    """Normalize dataclass, mapping, and attribute-based validator results."""

    if isinstance(value, ValidatorOutcome):
        return value

    if isinstance(value, Mapping):
        valid = value.get("valid")
        issues = value.get("issues", ())
        records = value.get("records", ())
    else:
        valid = getattr(value, "valid", None)
        issues = getattr(value, "issues", ())
        records = getattr(value, "records", ())

    if not isinstance(valid, bool):
        raise TypeError("validator result must expose a boolean 'valid' field")

    return ValidatorOutcome(
        valid=valid,
        issues=tuple(issues or ()),
        records=tuple(records or ()),
    )


async def run_validator(
    validator: Callable[[Any], ValidatorReturn],
    candidate: Any,
) -> ValidatorOutcome:
    """Run a synchronous or asynchronous validator and normalize its result."""

    value = validator(candidate)
    if inspect.isawaitable(value):
        value = await value
    return normalize_validator_outcome(value)


__all__ = [
    "GenerationPhase",
    "GenerationSession",
    "SchemaCandidate",
    "SchemaValidator",
    "TemplateCandidate",
    "TemplateValidator",
    "ValidatorOutcome",
    "ValidatorOutcomeAttributes",
    "ValidatorOutcomeLike",
]
