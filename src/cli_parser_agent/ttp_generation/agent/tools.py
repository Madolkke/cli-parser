"""AgentScope submission tools for TTP generation."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable, Mapping, Sequence
from copy import deepcopy
from dataclasses import asdict, is_dataclass
from enum import Enum
from typing import Any

from agentscope.message import TextBlock, ToolResultState
from agentscope.permission import (
    PermissionBehavior,
    PermissionContext,
    PermissionDecision,
)
from agentscope.tool import ParamsBase, ToolBase, ToolChunk
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from ...observability import finish_laminar_span, start_laminar_span
from ..progress import ProgressEmitter
from ..validation import ParseCapture, build_parse_capture
from .session import (
    GenerationPhase,
    GenerationSession,
    SchemaCandidate,
    SchemaValidator,
    TemplateCandidate,
    TemplateValidator,
    ValidatorOutcome,
    ValidatorOutcomeAttributes,
    ValidatorOutcomeLike,
    run_validator,
)

SUBMIT_SCHEMA_TOOL_NAME = "submit_result_schema"
SUBMIT_TEMPLATE_TOOL_NAME = "submit_ttp_template"
FINISH_GENERATION_TOOL_NAME = "finish_generation"


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


class FinishGenerationInput(ParamsBase):
    """finish_generation 接受的空参数对象。"""

    model_config = ConfigDict(extra="forbid")


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
    progress: ProgressEmitter | None,
    phase: GenerationPhase,
) -> ToolChunk:
    """Run one submission tool while closing its span before re-raising."""

    with start_laminar_span(
        name,
        input=dict(input),
        span_type="TOOL",
    ):
        try:
            result = await operation()
        except asyncio.CancelledError as error:
            if progress is not None and progress.enabled:
                progress.custom(
                    "cli_parser.tool.result",
                    {
                        "tool_name": name,
                        "input": _jsonable(input),
                        "output": {"status": "cancelled"},
                    },
                    phase=phase,
                    sensitive=True,
                )
            finish_laminar_span(
                output={
                    "status": "cancelled",
                    "exception_type": type(error).__name__,
                },
                outcome="cancelled",
                attributes={"exception_type": type(error).__name__},
            )
            raise
        except BaseException as error:
            if progress is not None and progress.enabled:
                progress.custom(
                    "cli_parser.tool.result",
                    {
                        "tool_name": name,
                        "input": _jsonable(input),
                        "output": {
                            "status": "failed",
                            "exception_type": type(error).__name__,
                        },
                    },
                    phase=phase,
                    sensitive=True,
                )
            finish_laminar_span(
                output={
                    "status": "failed",
                    "exception_type": type(error).__name__,
                },
                outcome="exception",
                attributes={"exception_type": type(error).__name__},
            )
            raise
        else:
            payload = _tool_chunk_payload(result)
            if progress is not None and progress.enabled:
                progress.custom(
                    "cli_parser.tool.result",
                    {
                        "tool_name": name,
                        "input": _jsonable(input),
                        "output": payload,
                    },
                    phase=phase,
                    sensitive=True,
                )
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


def _already_terminated_issue() -> dict[str, str]:
    """Return fixed feedback without exposing the terminal failure details."""

    return {
        "code": "generation_already_terminated",
        "stage": "template",
        "message": "Generation cannot continue after a terminal failure.",
    }


class _SubmissionToolBase(ToolBase):
    is_concurrency_safe = False
    is_read_only = True

    def __init__(
        self,
        session: GenerationSession,
        progress: ProgressEmitter | None = None,
    ) -> None:
        super().__init__()
        self.session = session
        self.progress = progress

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
            progress=self.progress,
            phase="schema",
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
            outcome = await run_validator(
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
                "finish_schema" if outcome.valid else "correct_and_resubmit_schema"
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
            progress=self.progress,
            phase="ttp",
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
                validated_candidate_available=False,
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
                        "message": "Generation has already been explicitly finished.",
                    },
                ),
                validated_candidate_available=True,
            )
        if (
            self.session.terminal_reason is not None
            and self.session.terminal_reason != "ttp_submission_limit"
        ):
            issues = (_already_terminated_issue(),)
            self.session.last_issues = issues
            return _result_chunk(
                phase="template",
                accepted=False,
                capture=_unavailable_capture(),
                issues=issues,
                validated_candidate_available=(
                    self.session.has_validated_ttp_candidate
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
                validated_candidate_available=(
                    self.session.has_validated_ttp_candidate
                ),
            )

        try:
            submission = TemplateSubmissionInput(ttp_template=ttp_template)
        except ValidationError:
            issues = (_safe_boundary_issue(phase="template", failure="input"),)
            self.session.last_issues = issues
            candidate_available = self.session.has_validated_ttp_candidate
            return _result_chunk(
                phase="template",
                accepted=False,
                capture=_unavailable_capture(),
                issues=issues,
                validated_candidate_available=candidate_available,
                ttp_submission=self.session.ttp_submissions,
                remaining_submissions=max(
                    0,
                    self.session.max_ttp_submissions - self.session.ttp_submissions,
                ),
                next_action=(
                    "finish_or_correct_and_resubmit_template"
                    if candidate_available
                    else "correct_and_resubmit_template"
                ),
            )

        if submission.ttp_template == self.session.last_ttp_template:
            self.session.ttp_submissions += 1
            candidate_available = self.session.has_validated_ttp_candidate
            issues = (
                {
                    "code": "ttp.unchanged_submission",
                    "stage": "template",
                    "message": (
                        "The template is identical to the previous submission. "
                        "Finish the stored validated candidate when available, "
                        "or modify the template before resubmission."
                    ),
                    "details": {
                        "required_action": (
                            "finish_or_modify_template"
                            if candidate_available
                            else "modify_template"
                        ),
                    },
                },
            )
            self.session.last_issues = issues
            if self.session.ttp_submissions >= self.session.max_ttp_submissions:
                self.session.terminal_reason = "ttp_submission_limit"
            return _result_chunk(
                phase="template",
                accepted=False,
                capture=_unavailable_capture(),
                issues=issues,
                validated_candidate_available=candidate_available,
                ttp_submission=self.session.ttp_submissions,
                remaining_submissions=max(
                    0,
                    self.session.max_ttp_submissions - self.session.ttp_submissions,
                ),
                next_action=(
                    "finish_or_correct_and_resubmit_template"
                    if candidate_available
                    else "correct_and_resubmit_template"
                ),
            )

        self.session.ttp_submissions += 1
        self.session.last_ttp_template = submission.ttp_template

        candidate = TemplateCandidate(
            ttp_template=submission.ttp_template,
            result_schema=deepcopy(self.session.frozen_schema),
            command_outputs=tuple(self.session.command_outputs),
        )
        try:
            outcome = await run_validator(
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

        if (
            self.session.ttp_submissions >= self.session.max_ttp_submissions
            and self.session.terminal_reason is None
        ):
            self.session.terminal_reason = "ttp_submission_limit"

        candidate_available = self.session.has_validated_ttp_candidate

        return _result_chunk(
            phase="template",
            accepted=accepted,
            capture=capture,
            issues=issues,
            validated_candidate_available=candidate_available,
            ttp_submission=self.session.ttp_submissions,
            remaining_submissions=max(
                0,
                self.session.max_ttp_submissions - self.session.ttp_submissions,
            ),
            next_action=(
                "review_capture_then_finish_or_resubmit"
                if accepted
                else (
                    "finish_or_correct_and_resubmit_template"
                    if candidate_available
                    else "correct_and_resubmit_template"
                )
            ),
        )


class FinishGenerationTool(_SubmissionToolBase):
    """Explicitly finish generation with the latest validated candidate."""

    name = FINISH_GENERATION_TOOL_NAME
    description = (
        "确认最近一次通过验证的 TTP 模板及其 capture 已满足要求，并结束生成。"
        "只有在 submit_ttp_template 已保存有效候选后才能调用；本工具不接收参数。"
    )
    input_schema = FinishGenerationInput.model_json_schema()

    async def call(self) -> ToolChunk:
        return await _run_traced_tool_call(
            name=self.name,
            input={},
            operation=self._call,
            progress=self.progress,
            phase="ttp",
        )

    async def _call(self) -> ToolChunk:
        candidate_available = self.session.has_validated_ttp_candidate

        if self.session.succeeded:
            return _result_chunk(
                phase="template",
                accepted=False,
                issues=(
                    {
                        "code": "generation_already_succeeded",
                        "stage": "template",
                        "message": "Generation has already been explicitly finished.",
                    },
                ),
                generation_finished=True,
                validated_candidate_available=True,
            )

        if (
            self.session.terminal_reason is not None
            and self.session.terminal_reason != "ttp_submission_limit"
        ):
            issues = (_already_terminated_issue(),)
            self.session.last_issues = issues
            return _result_chunk(
                phase="template",
                accepted=False,
                issues=issues,
                generation_finished=False,
                validated_candidate_available=candidate_available,
            )

        if (
            self.session.terminal_reason == "ttp_submission_limit"
            or self.session.ttp_submissions >= self.session.max_ttp_submissions
        ):
            self.session.terminal_reason = "ttp_submission_limit"
            issues = (
                {
                    "code": "ttp_submission_limit",
                    "stage": "template",
                    "message": "The template submission limit is exhausted.",
                },
            )
            self.session.last_issues = issues
            return _result_chunk(
                phase="template",
                accepted=False,
                issues=issues,
                generation_finished=False,
                validated_candidate_available=candidate_available,
            )

        if not candidate_available:
            issues = (
                {
                    "code": "generation.finish_without_valid_candidate",
                    "stage": "template",
                    "message": (
                        "A validated TTP template must be stored before "
                        "generation can finish."
                    ),
                },
            )
            self.session.last_issues = issues
            return _result_chunk(
                phase="template",
                accepted=False,
                issues=issues,
                generation_finished=False,
                validated_candidate_available=False,
                next_action="correct_and_resubmit_template",
            )

        self.session.generation_finished = True
        self.session.last_issues = ()
        self.session.terminal_reason = "success"
        return _result_chunk(
            phase="template",
            accepted=True,
            generation_finished=True,
            validated_candidate_available=True,
        )


def build_submission_tools(
    session: GenerationSession,
    phase: GenerationPhase,
    *,
    progress: ProgressEmitter | None = None,
) -> list[ToolBase]:
    """Build the fixed tools available to an isolated generation phase."""

    if phase == "schema":
        return [SubmitResultSchemaTool(session, progress)]
    if phase == "ttp":
        return [
            SubmitTtpTemplateTool(session, progress),
            FinishGenerationTool(session, progress),
        ]
    raise ValueError(f"unsupported generation phase: {phase!r}")


__all__ = [
    "FieldEvidenceInput",
    "GenerationPhase",
    "GenerationSession",
    "FinishGenerationTool",
    "SchemaCandidate",
    "SchemaValidator",
    "SubmitResultSchemaTool",
    "SubmitTtpTemplateTool",
    "TemplateCandidate",
    "TemplateValidator",
    "ValidatorOutcome",
    "ValidatorOutcomeAttributes",
    "ValidatorOutcomeLike",
    "SUBMIT_SCHEMA_TOOL_NAME",
    "SUBMIT_TEMPLATE_TOOL_NAME",
    "FINISH_GENERATION_TOOL_NAME",
    "build_submission_tools",
]
