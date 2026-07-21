"""AgentScope tools and request-local state for TTP generation."""

from __future__ import annotations

import asyncio
import inspect
import json
import time
from collections.abc import Awaitable, Callable, Mapping, Sequence
from copy import deepcopy
from dataclasses import asdict, dataclass, is_dataclass
from enum import Enum
from typing import Any, Protocol

from agentscope.message import TextBlock, ToolResultState
from agentscope.permission import (
    PermissionBehavior,
    PermissionContext,
    PermissionDecision,
)
from agentscope.tool import ParamsBase, ToolBase, ToolChunk
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from ...observability import finish_laminar_span, start_laminar_span
from ..validation import ParseCapture, build_parse_capture

SUBMIT_SCHEMA_TOOL_NAME = "submit_result_schema"
SUBMIT_TEMPLATE_TOOL_NAME = "submit_ttp_template"


class FieldEvidenceInput(ParamsBase):
    """为候选 Schema 的一个叶子路径提交的原文证据。"""

    model_config = ConfigDict(extra="forbid")

    path: str = Field(
        min_length=1,
        max_length=2_048,
        description=(
            "JSON 风格的数据路径；array 条目使用 *，例如 /interfaces/*/name。"
        ),
    )
    output_index: int = Field(
        ge=0,
        le=4,
        description="包含该证据的命令输出索引，从 0 开始。",
    )
    excerpt: str = Field(
        min_length=1,
        max_length=4096,
        description="从对应命令输出中原样复制的一段连续 excerpt。",
    )


class SchemaSubmissionInput(ParamsBase):
    """submit_result_schema 接受的完整参数。"""

    model_config = ConfigDict(extra="forbid")

    result_schema: dict[str, Any] = Field(
        description="描述单个 record 的完整 Draft 2020-12 JSON Schema。",
    )
    evidence: list[FieldEvidenceInput] = Field(
        min_length=1,
        max_length=256,
        description=(
            "Schema 声明的每个叶子字段必须恰好有一条 evidence；不要因多个"
            "样例而重复同一个 path。"
        ),
    )
    assumptions: list[str] = Field(
        default_factory=list,
        max_length=64,
        description=("简短且保守的中文 assumptions；不需要时优先提交空列表。"),
    )


class TemplateSubmissionInput(ParamsBase):
    """submit_ttp_template 接受的完整参数。"""

    model_config = ConfigDict(extra="forbid")

    ttp_template: str = Field(
        min_length=1,
        max_length=65_536,
        description="需要针对全部命令输出验证的完整共享 TTP 模板。",
    )


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
    """Small adapter result understood by the AgentScope layer."""

    valid: bool
    issues: tuple[Any, ...] = ()
    records: tuple[dict[str, Any], ...] = ()


class SchemaValidator(Protocol):
    """Validate a proposed schema without depending on AgentScope."""

    def __call__(
        self,
        candidate: SchemaCandidate,
    ) -> ValidatorOutcome | Any | Awaitable[ValidatorOutcome | Any]: ...


class TemplateValidator(Protocol):
    """Validate a proposed template without depending on AgentScope."""

    def __call__(
        self,
        candidate: TemplateCandidate,
    ) -> ValidatorOutcome | Any | Awaitable[ValidatorOutcome | Any]: ...


@dataclass(slots=True)
class GenerationSession:
    """Mutable state owned by exactly one generation request.

    The session is deliberately separate from ``AgentState``: AgentScope owns
    conversation state, while this object is the only accepted artifact
    channel. The service layer must read candidates from here, never from the
    agent's free-text reply.
    """

    command_outputs: tuple[str, ...] | Sequence[str]
    schema_validator: SchemaValidator
    template_validator: TemplateValidator
    max_ttp_submissions: int = 8
    max_agent_rounds: int = 12
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
        total_timeout_seconds: float = 300,
        max_ttp_submissions: int = 8,
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
    def succeeded(self) -> bool:
        """Whether a template and its records passed tool validation."""

        return self.validated_ttp_template is not None

    def apply_policy(self, policy: Any) -> None:
        """Apply policy limits once when the AgentScope agent is built."""

        max_submissions = int(policy.max_ttp_submissions)
        max_agent_rounds = int(policy.max_agent_rounds)
        max_schema_no_tool_retries = int(policy.max_schema_no_tool_retries)
        max_ttp_no_tool_retries = int(policy.max_ttp_no_tool_retries)
        timeout = float(policy.total_timeout_seconds)
        if max_submissions < 1:
            raise ValueError("policy.max_ttp_submissions must be positive")
        if max_agent_rounds < 1:
            raise ValueError("policy.max_agent_rounds must be positive")
        if max_schema_no_tool_retries < 0:
            raise ValueError(
                "policy.max_schema_no_tool_retries must be non-negative",
            )
        if max_ttp_no_tool_retries < 0:
            raise ValueError(
                "policy.max_ttp_no_tool_retries must be non-negative",
            )
        if timeout <= 0:
            raise ValueError("policy.total_timeout_seconds must be positive")

        self.max_ttp_submissions = max_submissions
        self.max_agent_rounds = max_agent_rounds
        self.max_schema_no_tool_retries = max_schema_no_tool_retries
        self.max_ttp_no_tool_retries = max_ttp_no_tool_retries
        if self.deadline_monotonic is None:
            self.deadline_monotonic = time.monotonic() + timeout

    def current_phase_tool_name(self) -> str | None:
        """Return the only submission tool visible in the current phase."""

        if self.succeeded:
            self.terminal_reason = "success"
            return None
        if self.terminal_reason is not None:
            return None
        if (
            self.deadline_monotonic is not None
            and time.monotonic() >= self.deadline_monotonic
        ):
            self.terminal_reason = "generation_timeout"
            return None
        if not self.schema_is_frozen:
            return SUBMIT_SCHEMA_TOOL_NAME
        if self.ttp_submissions >= self.max_ttp_submissions:
            self.terminal_reason = "ttp_submission_limit"
            return None
        return SUBMIT_TEMPLATE_TOOL_NAME

    def reset_no_tool_sequence(self, tool_name: str) -> None:
        """Reset the current phase's consecutive no-tool response count."""

        if tool_name == SUBMIT_SCHEMA_TOOL_NAME:
            self._schema_consecutive_no_tool_responses = 0
        elif tool_name == SUBMIT_TEMPLATE_TOOL_NAME:
            self._ttp_consecutive_no_tool_responses = 0

    def record_no_tool_response(self, tool_name: str) -> bool:
        """Record a no-tool response and return whether another retry is allowed."""

        if tool_name == SUBMIT_SCHEMA_TOOL_NAME:
            self.schema_no_tool_responses += 1
            self._schema_consecutive_no_tool_responses += 1
            return (
                self._schema_consecutive_no_tool_responses
                <= self.max_schema_no_tool_retries
            )
        if tool_name == SUBMIT_TEMPLATE_TOOL_NAME:
            self.ttp_no_tool_responses += 1
            self._ttp_consecutive_no_tool_responses += 1
            return (
                self._ttp_consecutive_no_tool_responses <= self.max_ttp_no_tool_retries
            )
        return False

    def record_no_tool_retry(self, tool_name: str) -> None:
        """Record an actual follow-up model request for a no-tool response."""

        if tool_name == SUBMIT_SCHEMA_TOOL_NAME:
            self.schema_no_tool_retries += 1
        elif tool_name == SUBMIT_TEMPLATE_TOOL_NAME:
            self.ttp_no_tool_retries += 1


def _normalize_outcome(value: Any) -> ValidatorOutcome:
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


async def _run_validator(
    validator: Callable[[Any], Any],
    candidate: Any,
) -> ValidatorOutcome:
    value = validator(candidate)
    if inspect.isawaitable(value):
        value = await value
    return _normalize_outcome(value)


def _jsonable(value: Any) -> Any:
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json")
    if is_dataclass(value):
        return asdict(value)
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, Mapping):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_jsonable(item) for item in value]
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return str(value)


def _result_chunk(
    *,
    phase: str,
    accepted: bool,
    issues: Sequence[Any] = (),
    **details: Any,
) -> ToolChunk:
    payload = {
        "phase": phase,
        "accepted": accepted,
        "issues": _jsonable(tuple(issues)),
        **details,
    }
    return ToolChunk(
        content=[
            TextBlock(
                text=json.dumps(
                    payload,
                    ensure_ascii=True,
                    separators=(",", ":"),
                ),
            ),
        ],
        state=ToolResultState.SUCCESS,
        metadata={"phase": phase, "accepted": accepted},
    )


def _tool_chunk_payload(chunk: ToolChunk) -> dict[str, Any]:
    """Recover the JSON payload produced by ``_result_chunk`` for tracing."""

    if len(chunk.content) == 1 and isinstance(chunk.content[0], TextBlock):
        try:
            payload = json.loads(chunk.content[0].text)
        except (TypeError, ValueError):
            pass
        else:
            if isinstance(payload, dict):
                return payload
    return {"status": "tool_result_unavailable"}


async def _run_traced_tool_call(
    *,
    name: str,
    input: Mapping[str, Any],
    operation: Callable[[], Awaitable[ToolChunk]],
) -> ToolChunk:
    """Run one submission tool while closing its span before re-raising."""

    result: ToolChunk | None = None
    pending_error: BaseException | None = None
    with start_laminar_span(
        name,
        input=dict(input),
        span_type="TOOL",
    ):
        try:
            result = await operation()
        except asyncio.CancelledError as error:
            pending_error = error
            finish_laminar_span(
                output={
                    "status": "cancelled",
                    "exception_type": type(error).__name__,
                },
                outcome="cancelled",
                attributes={"exception_type": type(error).__name__},
            )
        except BaseException as error:
            pending_error = error
            finish_laminar_span(
                output={
                    "status": "failed",
                    "exception_type": type(error).__name__,
                },
                outcome="exception",
                attributes={"exception_type": type(error).__name__},
            )
        else:
            payload = _tool_chunk_payload(result)
            attributes: dict[str, Any] = {
                "phase": str(payload.get("phase", "")),
                "accepted": payload.get("accepted") is True,
            }
            for key in ("schema_submission", "ttp_submission"):
                value = payload.get(key)
                if isinstance(value, int) and not isinstance(value, bool):
                    attributes[key] = value
            finish_laminar_span(
                output=payload,
                outcome="success",
                attributes=attributes,
            )

    if pending_error is not None:
        raise pending_error
    if result is None:  # pragma: no cover - guarded by the branches above
        raise RuntimeError("submission tool completed without a result")
    return result


def _unavailable_capture() -> ParseCapture:
    return build_parse_capture(())


def _safe_boundary_issue(*, phase: str, failure: str) -> dict[str, str]:
    """Return fixed feedback for errors that may contain candidate data."""

    if phase == "schema":
        code_prefix = "schema"
        subject = "Schema"
    else:
        code_prefix = "ttp"
        subject = "TTP template"

    if failure == "input":
        return {
            "code": f"{code_prefix}.submission_invalid",
            "stage": phase,
            "message": f"{subject} submission does not satisfy the tool contract.",
        }
    return {
        "code": f"{code_prefix}.validator_failed",
        "stage": phase,
        "message": f"{subject} validation could not be completed.",
    }


class _SubmissionToolBase(ToolBase):
    is_concurrency_safe = False
    is_read_only = True

    def __init__(self, session: GenerationSession) -> None:
        super().__init__()
        self.session = session

    async def check_permissions(
        self,
        tool_input: dict[str, Any],
        context: PermissionContext,
    ) -> PermissionDecision:
        return PermissionDecision(
            behavior=PermissionBehavior.ALLOW,
            message="Allowed request-local deterministic validation.",
            decision_reason=(
                "The tool only validates a candidate and updates its isolated "
                "in-memory generation session."
            ),
        )


class SubmitResultSchemaTool(_SubmissionToolBase):
    """Validate and permanently freeze the first accepted result schema."""

    name = SUBMIT_SCHEMA_TOOL_NAME
    description = (
        "提交完整的结果 JSON Schema、每个叶子字段恰好一条 evidence，以及"
        "必要的 assumptions。Schema 一旦通过便永久冻结；被拒绝后可以修正并"
        "重新提交。"
    )
    input_schema = SchemaSubmissionInput.model_json_schema()

    async def call(
        self,
        result_schema: dict[str, Any],
        evidence: list[dict[str, Any]],
        assumptions: list[str] | None = None,
    ) -> ToolChunk:
        return await _run_traced_tool_call(
            name=self.name,
            input={
                "result_schema": result_schema,
                "evidence": evidence,
                "assumptions": assumptions,
            },
            operation=lambda: self._call(
                result_schema=result_schema,
                evidence=evidence,
                assumptions=assumptions,
            ),
        )

    async def _call(
        self,
        result_schema: dict[str, Any],
        evidence: list[dict[str, Any]],
        assumptions: list[str] | None,
    ) -> ToolChunk:
        if self.session.schema_is_frozen:
            return _result_chunk(
                phase="schema",
                accepted=False,
                issues=(
                    {
                        "code": "schema_already_frozen",
                        "stage": "schema",
                        "message": "The accepted schema cannot be replaced.",
                    },
                ),
                frozen=True,
            )

        try:
            submission = SchemaSubmissionInput(
                result_schema=result_schema,
                evidence=evidence,
                assumptions=[] if assumptions is None else assumptions,
            )
        except ValidationError:
            issues = (_safe_boundary_issue(phase="schema", failure="input"),)
            self.session.last_issues = issues
            return _result_chunk(
                phase="schema",
                accepted=False,
                issues=issues,
                frozen=False,
                schema_submission=self.session.schema_submissions,
                next_action="correct_and_resubmit_schema",
            )

        candidate = SchemaCandidate(
            result_schema=deepcopy(submission.result_schema),
            evidence=tuple(
                item.model_dump(mode="python") for item in submission.evidence
            ),
            assumptions=tuple(submission.assumptions),
            command_outputs=tuple(self.session.command_outputs),
        )

        self.session.schema_submissions += 1
        self.session.last_result_schema = deepcopy(candidate.result_schema)
        try:
            outcome = await _run_validator(
                self.session.schema_validator,
                candidate,
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            outcome = ValidatorOutcome(
                valid=False,
                issues=(
                    _safe_boundary_issue(
                        phase="schema",
                        failure="validator",
                    ),
                ),
            )
        self.session.last_issues = outcome.issues

        if outcome.valid:
            self.session.frozen_schema = deepcopy(candidate.result_schema)
            self.session.field_evidence = deepcopy(candidate.evidence)
            self.session.assumptions = candidate.assumptions

        return _result_chunk(
            phase="schema",
            accepted=outcome.valid,
            issues=outcome.issues,
            frozen=outcome.valid,
            schema_submission=self.session.schema_submissions,
            next_action=(
                "submit_ttp_template"
                if outcome.valid
                else "correct_and_resubmit_schema"
            ),
        )


class SubmitTtpTemplateTool(_SubmissionToolBase):
    """Validate a TTP template against every full command output."""

    name = SUBMIT_TEMPLATE_TOOL_NAME
    description = (
        "只提交完整的共享 TTP 模板。系统会使用每份完整命令输出和已冻结的 "
        "JSON Schema 对它进行验证。"
    )
    input_schema = TemplateSubmissionInput.model_json_schema()

    async def call(self, ttp_template: str) -> ToolChunk:
        return await _run_traced_tool_call(
            name=self.name,
            input={"ttp_template": ttp_template},
            operation=lambda: self._call(ttp_template),
        )

    async def _call(self, ttp_template: str) -> ToolChunk:
        if not self.session.schema_is_frozen:
            return _result_chunk(
                phase="template",
                accepted=False,
                capture=_unavailable_capture(),
                issues=(
                    {
                        "code": "schema_not_frozen",
                        "stage": "template",
                        "message": "A valid result schema must be frozen first.",
                    },
                ),
            )
        if self.session.succeeded:
            return _result_chunk(
                phase="template",
                accepted=False,
                capture=_unavailable_capture(),
                issues=(
                    {
                        "code": "generation_already_succeeded",
                        "stage": "template",
                        "message": "A validated template is already stored.",
                    },
                ),
            )
        if self.session.ttp_submissions >= self.session.max_ttp_submissions:
            self.session.terminal_reason = "ttp_submission_limit"
            return _result_chunk(
                phase="template",
                accepted=False,
                capture=_unavailable_capture(),
                issues=(
                    {
                        "code": "ttp_submission_limit",
                        "stage": "template",
                        "message": "The template submission limit is exhausted.",
                    },
                ),
            )

        try:
            submission = TemplateSubmissionInput(ttp_template=ttp_template)
        except ValidationError:
            issues = (_safe_boundary_issue(phase="template", failure="input"),)
            self.session.last_issues = issues
            return _result_chunk(
                phase="template",
                accepted=False,
                capture=_unavailable_capture(),
                issues=issues,
                ttp_submission=self.session.ttp_submissions,
                remaining_submissions=max(
                    0,
                    self.session.max_ttp_submissions - self.session.ttp_submissions,
                ),
                next_action="correct_and_resubmit_template",
            )

        if submission.ttp_template == self.session.last_ttp_template:
            self.session.ttp_submissions += 1
            issues = (
                {
                    "code": "ttp.unchanged_submission",
                    "stage": "template",
                    "message": (
                        "The template is identical to the previous rejected "
                        "submission and must be changed before resubmission."
                    ),
                    "details": {"required_action": "modify_template"},
                },
            )
            self.session.last_issues = issues
            return _result_chunk(
                phase="template",
                accepted=False,
                capture=_unavailable_capture(),
                issues=issues,
                ttp_submission=self.session.ttp_submissions,
                remaining_submissions=max(
                    0,
                    self.session.max_ttp_submissions - self.session.ttp_submissions,
                ),
                next_action="correct_and_resubmit_template",
            )

        self.session.ttp_submissions += 1
        self.session.last_ttp_template = submission.ttp_template

        candidate = TemplateCandidate(
            ttp_template=submission.ttp_template,
            result_schema=deepcopy(self.session.frozen_schema),
            command_outputs=tuple(self.session.command_outputs),
        )
        try:
            outcome = await _run_validator(
                self.session.template_validator,
                candidate,
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            outcome = ValidatorOutcome(
                valid=False,
                issues=(
                    _safe_boundary_issue(
                        phase="template",
                        failure="validator",
                    ),
                ),
            )

        capture_records: Sequence[Any] = ()
        if len(outcome.records) == len(self.session.command_outputs) and all(
            isinstance(item, dict) for item in outcome.records
        ):
            capture_records = outcome.records
        capture = build_parse_capture(capture_records)

        issues = list(outcome.issues)
        records: tuple[dict[str, Any], ...] = ()
        accepted = outcome.valid
        if accepted:
            if len(outcome.records) != len(self.session.command_outputs):
                accepted = False
                issues.append(
                    {
                        "code": "record_count_mismatch",
                        "stage": "template",
                        "message": (
                            "Validator must return exactly one record for each "
                            "command output."
                        ),
                    },
                )
            elif not all(isinstance(item, dict) for item in outcome.records):
                accepted = False
                issues.append(
                    {
                        "code": "record_root_not_object",
                        "stage": "template",
                        "message": "Every parsed record must be a JSON object.",
                    },
                )
            else:
                records = tuple(deepcopy(item) for item in outcome.records)

        if self.session.first_ttp_valid is None:
            self.session.first_ttp_valid = accepted
        self.session.last_issues = tuple(issues)

        if accepted:
            self.session.validated_ttp_template = submission.ttp_template
            self.session.records = records
            self.session.terminal_reason = "success"

        return _result_chunk(
            phase="template",
            accepted=accepted,
            capture=capture,
            issues=issues,
            ttp_submission=self.session.ttp_submissions,
            remaining_submissions=max(
                0,
                self.session.max_ttp_submissions - self.session.ttp_submissions,
            ),
            next_action=("finish" if accepted else "correct_and_resubmit_template"),
        )


def build_submission_tools(
    session: GenerationSession,
) -> list[ToolBase]:
    """Build the only two tools available to the generation agent."""

    return [
        SubmitResultSchemaTool(session),
        SubmitTtpTemplateTool(session),
    ]


__all__ = [
    "FieldEvidenceInput",
    "GenerationSession",
    "SchemaCandidate",
    "SchemaValidator",
    "SubmitResultSchemaTool",
    "SubmitTtpTemplateTool",
    "TemplateCandidate",
    "TemplateValidator",
    "ValidatorOutcome",
    "SUBMIT_SCHEMA_TOOL_NAME",
    "SUBMIT_TEMPLATE_TOOL_NAME",
    "build_submission_tools",
]
