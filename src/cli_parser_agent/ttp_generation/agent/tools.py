"""AgentScope submission tools for TTP generation."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable, Mapping, Sequence
from copy import deepcopy
from dataclasses import asdict, dataclass, is_dataclass
from enum import Enum
from typing import Any

from agentscope.message import TextBlock, ToolResultState
from agentscope.permission import (
    PermissionBehavior,
    PermissionContext,
    PermissionDecision,
)
from agentscope.tool import ParamsBase, ToolBase, ToolChunk
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from ...observability import finish_laminar_span, start_laminar_span
from ..progress import ProgressEmitter
from ..validation import (
    MAX_TTP_TEMPLATE_BYTES,
    MAX_TTP_TEST_INPUT_BYTES,
    ParseCapture,
    TtpParseResult,
    build_parse_capture,
    parse_ttp_template,
)
from .session import (
    GenerationPhase,
    GenerationSession,
    SchemaCandidate,
    SchemaValidator,
    TemplateCandidate,
    TemplateValidator,
    TtpTestCandidate,
    ValidatorOutcome,
    ValidatorOutcomeAttributes,
    ValidatorOutcomeLike,
    run_ttp_test_validator,
    run_validator,
)

SUBMIT_SCHEMA_TOOL_NAME = "submit_result_schema"
SUBMIT_TEMPLATE_TOOL_NAME = "submit_ttp_template"
TEST_TEMPLATE_TOOL_NAME = "test_ttp_template"
FINISH_GENERATION_TOOL_NAME = "finish_generation"


class SchemaSubmissionInput(ParamsBase):
    """submit_result_schema 接受的完整参数。"""

    model_config = ConfigDict(extra="forbid")

    result_schema: dict[str, Any] = Field(
        description="描述单个 record 的完整 Draft 2020-12 JSON Schema。",
    )


class TemplateSubmissionInput(ParamsBase):
    """submit_ttp_template 接受的完整参数。"""

    model_config = ConfigDict(extra="forbid")

    ttp_template: str = Field(
        min_length=1,
        max_length=65_536,
        description="需要针对全部命令输出验证的完整共享 TTP 模板。",
    )


class TtpTemplateTestInput(ParamsBase):
    """test_ttp_template 接受的独立文本和模板。"""

    model_config = ConfigDict(extra="forbid")

    text: str = Field(
        min_length=1,
        max_length=1_048_576,
        description="用于测试 TTP 模板的单份非空白文本。",
    )
    ttp_template: str = Field(
        min_length=1,
        max_length=65_536,
        description="需要测试的完整 TTP 模板。",
    )

    @field_validator("text")
    @classmethod
    def _validate_text(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("text must not be blank")
        try:
            size = len(value.encode("utf-8"))
        except UnicodeEncodeError as error:
            raise ValueError("text must be valid UTF-8") from error
        if size > MAX_TTP_TEST_INPUT_BYTES:
            raise ValueError("text exceeds the UTF-8 byte limit")
        return value

    @field_validator("ttp_template")
    @classmethod
    def _validate_template_bytes(cls, value: str) -> str:
        try:
            size = len(value.encode("utf-8"))
        except UnicodeEncodeError as error:
            raise ValueError("ttp_template must be valid UTF-8") from error
        if size > MAX_TTP_TEMPLATE_BYTES:
            raise ValueError("ttp_template exceeds the UTF-8 byte limit")
        return value


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


def _result_payload(
    *,
    phase: str,
    accepted: bool,
    issues: Sequence[Any] = (),
    **details: Any,
) -> dict[str, Any]:
    return {
        "phase": phase,
        "accepted": accepted,
        "issues": _jsonable(tuple(issues)),
        **details,
    }


def _result_chunk(
    *,
    phase: str,
    accepted: bool,
    issues: Sequence[Any] = (),
    **details: Any,
) -> ToolChunk:
    payload = _result_payload(
        phase=phase,
        accepted=accepted,
        issues=issues,
        **details,
    )
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


@dataclass(frozen=True, slots=True)
class _TracedToolResult:
    """Separate model-visible feedback from the diagnostic TOOL payload."""

    chunk: ToolChunk
    diagnostic_payload: dict[str, Any]


_TTP_SYNTAX_OR_SAFETY_CODES = {
    "ttp.duplicate_line_variable",
    "ttp.empty_template",
    "ttp.forbidden_group_attribute",
    "ttp.forbidden_tag",
    "ttp.forbidden_template_attribute",
    "ttp.forbidden_xml_declaration",
    "ttp.group_attribute_too_long",
    "ttp.group_depth_exceeded",
    "ttp.invalid_field_name",
    "ttp.invalid_group_method",
    "ttp.invalid_group_name",
    "ttp.invalid_ignore_syntax",
    "ttp.invalid_line_control",
    "ttp.invalid_root_tag",
    "ttp.invalid_utf8",
    "ttp.invalid_variable_syntax",
    "ttp.invalid_xml",
    "ttp.no_variables",
    "ttp.submission_invalid",
    "ttp.template_too_large",
    "ttp.unsafe_group_attribute",
    "ttp.unsafe_variable_attribute",
}
_TTP_PARSE_FAILURE_CODES = {
    "generation.timeout",
    "ttp.timeout",
    "ttp.validator_failed",
    "ttp.worker_bootstrap_failed",
    "ttp.worker_error",
    "ttp.worker_host_unsupported",
    "ttp.worker_start_failed",
}


def _issue_code(issue: Any) -> str | None:
    value = _jsonable(issue)
    if not isinstance(value, Mapping):
        return None
    code = value.get("code")
    return code if isinstance(code, str) else None


def _brief_ttp_error(issues: Sequence[Any]) -> str:
    codes = {_issue_code(issue) for issue in issues}
    if any(code in _TTP_PARSE_FAILURE_CODES for code in codes):
        return "错误：模板解析未能完成。"
    if any(code in _TTP_SYNTAX_OR_SAFETY_CODES for code in codes):
        return "错误：模板未通过语法或安全检查。"
    return "错误：模板未产生可用的匹配结果。"


def _format_ttp_records_for_model(records: Sequence[Any]) -> str:
    """Format each input-mapped record as an independently labelled block."""

    if not records:
        return "[]"

    lines = [
        "以下是按输入顺序分别返回的解析结果。每个块只对应一个输入，不要跨块合并。",
    ]
    for input_index, record in enumerate(records):
        serialized = json.dumps(
            record,
            ensure_ascii=False,
            separators=(",", ":"),
        )
        lines.extend(
            [
                "",
                (
                    f'<parsed_record input_index="{input_index}" '
                    f'display_number="{input_index + 1}">'
                ),
                serialized,
                "</parsed_record>",
            ],
        )
    return "\n".join(lines)


def _ttp_result_chunk(
    *,
    accepted: bool,
    issues: Sequence[Any] = (),
    matched_records: Sequence[Any] = (),
    **details: Any,
) -> _TracedToolResult:
    diagnostic_payload = _result_payload(
        phase="template",
        accepted=accepted,
        issues=issues,
        **details,
    )
    records = _jsonable(tuple(matched_records))
    model_text = _format_ttp_records_for_model(records)
    if not records:
        model_text = f"{model_text}\n{_brief_ttp_error(issues)}"
    return _TracedToolResult(
        chunk=ToolChunk(
            content=[TextBlock(text=model_text)],
            state=ToolResultState.SUCCESS,
            metadata={"phase": "template", "accepted": accepted},
        ),
        diagnostic_payload=diagnostic_payload,
    )


def _ttp_test_result_chunk(
    *,
    result: Any = None,
    has_result: bool = False,
    issues: Sequence[Any] = (),
    **details: Any,
) -> _TracedToolResult:
    """Format a parse-only result like submit_ttp_template for the model."""

    diagnostic_payload = _result_payload(
        phase="template",
        accepted=has_result and not issues,
        issues=issues,
        **details,
    )
    if has_result:
        model_text = _format_ttp_records_for_model((result,))
    else:
        model_text = f"[]\n{_brief_ttp_error(issues)}"
    return _TracedToolResult(
        chunk=ToolChunk(
            content=[TextBlock(text=model_text)],
            state=ToolResultState.SUCCESS,
            metadata={"phase": "template", "accepted": has_result and not issues},
        ),
        diagnostic_payload=diagnostic_payload,
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
    operation: Callable[[], Awaitable[ToolChunk | _TracedToolResult]],
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
            operation_result = await operation()
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
            if isinstance(operation_result, _TracedToolResult):
                result = operation_result.chunk
                payload = operation_result.diagnostic_payload
            else:
                result = operation_result
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


class TestTtpTemplateTool(_SubmissionToolBase):
    """Safely experiment with one independent text/template pair."""

    __test__ = False
    name = TEST_TEMPLATE_TOOL_NAME
    description = (
        "使用一份独立文本测试 TTP 模板的解析行为。该工具只返回实验结果，"
        "不会校验冻结 Schema，也不会保存或替换生成候选。"
    )
    input_schema = TtpTemplateTestInput.model_json_schema()

    async def call(
        self,
        text: str | None = None,
        ttp_template: str | None = None,
        **unexpected_arguments: Any,
    ) -> ToolChunk:
        self.session.ttp_test_calls += 1
        traced_input: dict[str, Any] = {
            "text": text,
            "ttp_template": ttp_template,
        }
        if unexpected_arguments:
            traced_input["invalid_tool_arguments"] = True
        return await _run_traced_tool_call(
            name=self.name,
            input=traced_input,
            operation=lambda: self._call(
                text=text,
                ttp_template=ttp_template,
                unexpected_arguments=unexpected_arguments,
            ),
            progress=self.progress,
            phase="ttp",
        )

    async def _call(
        self,
        *,
        text: str | None,
        ttp_template: str | None,
        unexpected_arguments: Mapping[str, Any],
    ) -> _TracedToolResult:
        try:
            submission = TtpTemplateTestInput.model_validate(
                {
                    "text": text,
                    "ttp_template": ttp_template,
                    **unexpected_arguments,
                },
            )
        except ValidationError:
            issues = (
                {
                    "code": "ttp.test_input_invalid",
                    "stage": "template",
                    "message": "TTP test input does not satisfy the tool contract.",
                },
            )
            return _ttp_test_result_chunk(
                issues=issues,
                ttp_test_calls=self.session.ttp_test_calls,
            )

        candidate = TtpTestCandidate(
            text=submission.text,
            ttp_template=submission.ttp_template,
        )
        try:
            if self.session.ttp_test_validator is None:
                outcome = await asyncio.to_thread(
                    parse_ttp_template,
                    candidate.ttp_template,
                    candidate.text,
                )
            else:
                outcome = await run_ttp_test_validator(
                    self.session.ttp_test_validator,
                    candidate,
                )
            if not isinstance(outcome, TtpParseResult):
                raise TypeError("TTP test validator returned an unsupported result")
        except asyncio.CancelledError:
            raise
        except Exception:
            return _ttp_test_result_chunk(
                issues=(
                    {
                        "code": "ttp.test_validator_failed",
                        "stage": "template",
                        "message": "TTP test validation could not be completed.",
                    },
                ),
                ttp_test_calls=self.session.ttp_test_calls,
            )

        if outcome.issues:
            return _ttp_test_result_chunk(
                issues=outcome.issues,
                ttp_test_calls=self.session.ttp_test_calls,
            )
        return _ttp_test_result_chunk(
            result=deepcopy(outcome.result),
            has_result=True,
            ttp_test_calls=self.session.ttp_test_calls,
        )


class SubmitResultSchemaTool(_SubmissionToolBase):
    """Validate and permanently freeze the first accepted result schema."""

    name = SUBMIT_SCHEMA_TOOL_NAME
    description = (
        "提交完整的结果 JSON Schema。Schema 一旦通过便"
        "永久冻结；被拒绝后可以修正并重新提交。"
    )
    input_schema = SchemaSubmissionInput.model_json_schema()

    async def call(
        self,
        result_schema: dict[str, Any] | None = None,
        **unexpected_arguments: Any,
    ) -> ToolChunk:
        traced_input: dict[str, Any] = {
            "result_schema": result_schema,
        }
        if unexpected_arguments:
            traced_input["invalid_tool_arguments"] = True
        return await _run_traced_tool_call(
            name=self.name,
            input=traced_input,
            operation=lambda: self._call(
                result_schema=result_schema,
                unexpected_arguments=unexpected_arguments,
            ),
            progress=self.progress,
            phase="schema",
        )

    async def _call(
        self,
        result_schema: dict[str, Any] | None,
        unexpected_arguments: Mapping[str, Any],
    ) -> ToolChunk:
        try:
            submission = SchemaSubmissionInput.model_validate(
                {
                    "result_schema": result_schema,
                    **unexpected_arguments,
                },
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

        candidate = SchemaCandidate(
            result_schema=deepcopy(submission.result_schema),
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
        "JSON Schema 对它进行验证，并直接返回按输入索引排列的 TTP 匹配结果。"
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

    async def _call(self, ttp_template: str) -> _TracedToolResult:
        if not self.session.schema_is_frozen:
            return _ttp_result_chunk(
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
            return _ttp_result_chunk(
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
            return _ttp_result_chunk(
                accepted=False,
                capture=_unavailable_capture(),
                issues=issues,
                validated_candidate_available=(
                    self.session.has_validated_ttp_candidate
                ),
            )
        if self.session.ttp_submissions >= self.session.max_ttp_submissions:
            self.session.terminal_reason = "ttp_submission_limit"
            return _ttp_result_chunk(
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
            return _ttp_result_chunk(
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
            return _ttp_result_chunk(
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

        return _ttp_result_chunk(
            accepted=accepted,
            capture=capture,
            issues=issues,
            matched_records=outcome.records,
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
            TestTtpTemplateTool(session, progress),
            FinishGenerationTool(session, progress),
        ]
    raise ValueError(f"unsupported generation phase: {phase!r}")


__all__ = [
    "GenerationPhase",
    "GenerationSession",
    "FinishGenerationTool",
    "SchemaCandidate",
    "SchemaValidator",
    "SubmitResultSchemaTool",
    "SubmitTtpTemplateTool",
    "TestTtpTemplateTool",
    "TEST_TEMPLATE_TOOL_NAME",
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
