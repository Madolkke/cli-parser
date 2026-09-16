"""Compile a request-local lightweight draft and freeze its first valid Schema."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from copy import deepcopy
from typing import Any

from agentscope.tool import ParamsBase, ToolChunk
from pydantic import ConfigDict, Field, ValidationError

from ..schema_draft import compile_schema_draft
from .protocol import (
    mark_tool_arguments_rejected,
    mark_tool_arguments_valid,
    mark_tool_entered,
    mark_tool_execution_failed,
)
from .session import SchemaCandidate, run_validator
from .tools import (
    _result_chunk,
    _result_payload,
    _run_traced_tool_call,
    _safe_boundary_issue,
    _SubmissionToolBase,
    _TracedToolResult,
)

SUBMIT_SCHEMA_DRAFT_TOOL_NAME = "submit_schema_draft"


class SchemaDraftSubmissionInput(ParamsBase):
    model_config = ConfigDict(extra="forbid")

    draft: dict[str, Any] = Field(
        description="描述整份单次命令输出的完整 version=1 轻量草稿，含 fields。"
        "字段用来源标签或必要语义兜底命名，类型、required 和说明由模型判断。"
    )


class SubmitSchemaDraftTool(_SubmissionToolBase):
    name = SUBMIT_SCHEMA_DRAFT_TOOL_NAME
    description = (
        "提交完整轻量 Schema 草稿。程序核对标签来源并编译，成功后立即永久冻结。"
        "一次完整输出为一个根对象，重复业务实体属于根内或所属实体内的数组。"
        "被拒绝时按 issues 修正完整草稿后重交；不提交任意 result_schema。"
    )
    input_schema = SchemaDraftSubmissionInput.model_json_schema()

    async def call(
        self, draft: dict[str, Any] | None = None, **unexpected_arguments: Any
    ) -> ToolChunk:
        mark_tool_entered(self.name)
        traced_input = {"draft": draft}
        if unexpected_arguments:
            traced_input["invalid_tool_arguments"] = True
        return await _run_traced_tool_call(
            name=self.name,
            input=traced_input,
            operation=lambda: self._call(draft, unexpected_arguments),
            progress=self.progress,
            phase="schema",
        )

    async def _call(
        self, draft: dict[str, Any] | None, unexpected_arguments: Mapping[str, Any]
    ) -> ToolChunk | _TracedToolResult:
        try:
            submission = SchemaDraftSubmissionInput.model_validate(
                {"draft": draft, **unexpected_arguments}
            )
        except ValidationError:
            mark_tool_arguments_rejected(self.name)
            return self._rejected(
                (_safe_boundary_issue(phase="schema", failure="input"),)
            )
        mark_tool_arguments_valid(self.name)
        if self.session.schema_is_frozen:
            return _result_chunk(
                phase="schema",
                accepted=False,
                frozen=True,
                issues=(
                    {
                        "code": "schema_already_frozen",
                        "stage": "schema",
                        "message": "The accepted schema cannot be replaced.",
                    },
                ),
            )
        self.session.schema_submissions += 1
        try:
            compiled = compile_schema_draft(
                submission.draft,
                self.session.schema_draft_sources,
                max_schema_bytes=self.session.max_schema_bytes,
                max_schema_depth=self.session.max_schema_depth,
                max_schema_properties=self.session.max_schema_properties,
            )
            if not compiled.accepted:
                return self._rejected(compiled.issues, facts=compiled.facts)
            candidate = SchemaCandidate(
                result_schema=deepcopy(compiled.schema),
                command_outputs=tuple(self.session.command_outputs),
            )
            self.session.last_result_schema = deepcopy(candidate.result_schema)
            outcome = await run_validator(self.session.schema_validator, candidate)
        except asyncio.CancelledError:
            raise
        except Exception:
            mark_tool_execution_failed(self.name)
            return self._rejected(
                (_safe_boundary_issue(phase="schema", failure="validator"),)
            )
        self.session.last_issues = outcome.issues
        if not outcome.valid:
            return self._rejected(outcome.issues, facts=compiled.facts)
        self.session.frozen_schema = deepcopy(candidate.result_schema)
        details = {
            "frozen": True,
            "schema_submission": self.session.schema_submissions,
            "next_action": "finish_schema",
            "facts": dict(compiled.facts),
        }
        # The complete compiled contract goes only to explicitly sensitive
        # diagnostics; there is no second model confirmation call.
        return _TracedToolResult(
            chunk=_result_chunk(phase="schema", accepted=True, **details),
            diagnostic_payload=_result_payload(
                phase="schema",
                accepted=True,
                **details,
                compiled_schema=deepcopy(self.session.frozen_schema),
            ),
        )

    def _rejected(self, issues, *, facts=None) -> ToolChunk:
        self.session.last_issues = tuple(issues)
        return _result_chunk(
            phase="schema",
            accepted=False,
            frozen=False,
            issues=issues,
            schema_submission=self.session.schema_submissions,
            next_action="correct_and_resubmit_schema",
            **({"facts": dict(facts)} if facts is not None else {}),
        )
