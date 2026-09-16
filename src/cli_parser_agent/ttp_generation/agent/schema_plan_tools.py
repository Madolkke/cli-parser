"""Internal Schema-plan tools; never change the external injection protocol."""

import asyncio
from copy import deepcopy
from typing import Any

from agentscope.tool import ParamsBase, ToolChunk
from pydantic import ConfigDict, Field, ValidationError

from ..schema_plan import compile_schema_plan
from .protocol import (
    mark_tool_arguments_rejected,
    mark_tool_arguments_valid,
    mark_tool_entered,
    mark_tool_execution_failed,
)
from .session import SchemaCandidate, run_validator
from .tools import (
    FinishGenerationInput,
    _result_chunk,
    _run_traced_tool_call,
    _SubmissionToolBase,
)


class _PlanInput(ParamsBase):
    model_config = ConfigDict(extra="forbid")
    # Deliberately use a shallow wire schema, as with result_schema: recursive
    # plans are checked by the bounded compiler, not the provider tool dialect.
    plan: dict[str, Any] = Field(description="完整的 version=1 SchemaPlan 建模方案。")


class SubmitSchemaPlanTool(_SubmissionToolBase):
    name = "submit_schema_plan"
    description = (
        "提交完整建模方案 plan：version=1 和 nodes 列表。"
        "节点引用实际 source fragments；"
        "不提交 JSON Schema、任意 type/required/description。"
        "合法计划由程序编译，错误后修正完整计划。"
    )
    input_schema = _PlanInput.model_json_schema()

    async def call(
        self, plan: dict[str, Any] | None = None, **unexpected: Any
    ) -> ToolChunk:
        mark_tool_entered(self.name)
        # Invalidate before validation, including malformed replacement attempts.
        self.session.pending_schema_plan = None
        self.session.pending_schema_plan_round = None
        return await _run_traced_tool_call(
            name=self.name,
            input={
                "plan": plan,
                **({"invalid_tool_arguments": True} if unexpected else {}),
            },
            operation=lambda: self._call(plan, unexpected),
            progress=self.progress,
            phase="schema",
        )

    async def _call(self, plan, unexpected):
        try:
            submitted = _PlanInput.model_validate({"plan": plan, **unexpected})
        except ValidationError:
            mark_tool_arguments_rejected(self.name)
            return _result_chunk(
                phase="schema",
                accepted=False,
                frozen=False,
                issues=(
                    {
                        "code": "schema_plan.invalid_arguments",
                        "path": "/",
                        "message": "Submit one complete plan object.",
                    },
                ),
            )
        mark_tool_arguments_valid(self.name)
        if self.session.schema_is_frozen or self.session.terminal_reason:
            return _result_chunk(
                phase="schema",
                accepted=False,
                frozen=self.session.schema_is_frozen,
                issues=(
                    {
                        "code": "schema_plan.closed",
                        "path": "/",
                        "message": "Schema generation is closed.",
                    },
                ),
            )
        self.session.schema_submissions += 1
        try:
            outcome = compile_schema_plan(
                submitted.plan, self.session.schema_plan_sources
            )
        except Exception:
            return self._execution_failure()
        self.session.last_issues = outcome.issues
        if not outcome.accepted:
            return _result_chunk(
                phase="schema",
                accepted=False,
                frozen=False,
                issues=outcome.issues,
                schema_submission=self.session.schema_submissions,
                facts=outcome.facts,
            )
        # The workflow's validator also applies request policy limits.
        try:
            validation = await run_validator(
                self.session.schema_validator,
                SchemaCandidate(outcome.schema, tuple(self.session.command_outputs)),
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            return self._execution_failure()
        self.session.last_issues = validation.issues
        if not validation.valid:
            return _result_chunk(
                phase="schema",
                accepted=False,
                frozen=False,
                issues=validation.issues,
                schema_submission=self.session.schema_submissions,
            )
        self.session.last_result_schema = deepcopy(outcome.schema)
        confirm = self.session.schema_strategy == "plan_confirm"
        if confirm:
            self.session.pending_schema_plan = deepcopy(outcome.schema)
            self.session.pending_schema_plan_round = self.session.schema_agent_rounds
        else:
            self.session.frozen_schema = deepcopy(outcome.schema)
        return _result_chunk(
            phase="schema",
            accepted=True,
            frozen=not confirm,
            issues=(),
            schema_submission=self.session.schema_submissions,
            compiled_schema=outcome.schema,
            facts=outcome.facts,
            semantic_validation="not_performed",
        )

    def _execution_failure(self):
        mark_tool_execution_failed(self.name)
        issues = (
            {
                "code": "schema_plan.validator_failed",
                "path": "/",
                "message": "Schema-plan validation could not be completed.",
            },
        )
        self.session.last_issues = issues
        return _result_chunk(
            phase="schema",
            accepted=False,
            frozen=False,
            issues=issues,
            schema_submission=self.session.schema_submissions,
        )


class ConfirmSchemaPlanTool(_SubmissionToolBase):
    name = "confirm_schema_plan"
    description = (
        "无参数。对照原文复核最新完整编译Schema的实体、覆盖、值保真及required后确认冻结；"
        "如需修改先重新提交完整plan。确认不等同于确定性语义验收。"
    )
    input_schema = FinishGenerationInput.model_json_schema()

    async def call(self) -> ToolChunk:
        mark_tool_entered(self.name)
        mark_tool_arguments_valid(self.name)
        return await _run_traced_tool_call(
            name=self.name,
            input={},
            operation=self._call,
            progress=self.progress,
            phase="schema",
        )

    async def _call(self):
        if (
            self.session.pending_schema_plan is None
            or self.session.schema_is_frozen
            or self.session.terminal_reason
            or self.session.schema_strategy != "plan_confirm"
        ):
            return _result_chunk(
                phase="schema",
                accepted=False,
                frozen=self.session.schema_is_frozen,
                issues=(
                    {
                        "code": "schema_plan.no_pending_plan",
                        "path": "/",
                        "message": "A latest valid pending plan is required.",
                    },
                ),
            )
        if (
            self.session.pending_schema_plan_round is None
            or self.session.schema_agent_rounds
            <= self.session.pending_schema_plan_round
        ):
            return _result_chunk(
                phase="schema",
                accepted=False,
                frozen=False,
                issues=(
                    {
                        "code": "schema_plan.confirmation_requires_review",
                        "path": "/",
                        "message": (
                            "Read the compiled schema before confirming "
                            "in a later round."
                        ),
                    },
                ),
            )
        self.session.frozen_schema = deepcopy(self.session.pending_schema_plan)
        self.session.pending_schema_plan = None
        self.session.pending_schema_plan_round = None
        return _result_chunk(
            phase="schema",
            accepted=True,
            frozen=True,
            issues=(),
            compiled_schema=self.session.frozen_schema,
            semantic_validation="not_performed",
        )
