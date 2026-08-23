"""Public facade for one schema-then-TTP generation request."""

from __future__ import annotations

import asyncio
import copy
from collections.abc import Mapping
from typing import Any
from uuid import uuid4

from ..config import (
    GenerationPolicy,
    TtpGeneratorSettings,
    model_extra_body_sha256,
)
from ..observability import (
    finish_laminar_span,
    initialize_laminar_from_env,
    start_laminar_span,
)
from .agent import PROMPT_VERSION
from .contracts import (
    GenerationRequest,
    GenerationResult,
    SchemaProposalResult,
    TemplateRequest,
)
from .progress import ProgressEmitter, ProgressObserver
from .workflow import GenerationMode, _GenerationWorkflow


class TtpGenerator:
    """Generate and independently accept one TTP artifact bundle."""

    def __init__(
        self,
        *,
        settings: TtpGeneratorSettings,
        policy: GenerationPolicy | None = None,
        _laminar_environ: Mapping[str, str] | None = None,
    ) -> None:
        self.settings = TtpGeneratorSettings.model_validate(
            copy.deepcopy(settings.model_dump()),
        )
        self.policy = (
            GenerationPolicy()
            if policy is None
            else GenerationPolicy.model_validate(policy)
        )
        initialize_laminar_from_env(_laminar_environ)

    @classmethod
    def from_env(
        cls,
        *,
        environ: Mapping[str, str] | None = None,
        policy: GenerationPolicy | None = None,
    ) -> TtpGenerator:
        """Construct model settings and optional budgets from the environment."""

        resolved_policy = (
            GenerationPolicy.from_env(environ) if policy is None else policy
        )
        settings = TtpGeneratorSettings.from_env(environ)
        return cls(
            settings=settings,
            policy=resolved_policy,
            _laminar_environ=environ,
        )

    async def generate(
        self,
        request: GenerationRequest,
        *,
        observer: ProgressObserver | None = None,
    ) -> GenerationResult:
        """Trace one request and preserve the framework-neutral public API."""

        return await self._traced_generate(
            GenerationRequest.model_validate(request),
            observer=observer,
        )

    async def propose_schema(
        self,
        request: GenerationRequest,
        *,
        observer: ProgressObserver | None = None,
    ) -> SchemaProposalResult:
        """Freeze a result schema without generating a template.

        Runs the Schema phase alone and returns the frozen proposal with its
        evidence and assumptions, so a caller can review or edit it and feed it
        back through :meth:`generate_from_schema`.
        """

        return await self._traced_generate(
            GenerationRequest.model_validate(request),
            observer=observer,
            mode="schema_only",
        )

    async def generate_from_schema(
        self,
        request: TemplateRequest,
        *,
        observer: ProgressObserver | None = None,
    ) -> GenerationResult:
        """Run the TTP phase alone against a caller-supplied result schema.

        The schema is frozen as given instead of being inferred, so the Schema
        phase is skipped entirely.  Acceptance and TTP validation are identical
        to :meth:`generate`, which keeps the two paths comparable in evaluation.
        """

        request = TemplateRequest.model_validate(request)
        return await self._traced_generate(
            GenerationRequest(command_outputs=request.command_outputs),
            observer=observer,
            mode="template_only",
            injected_schema=request.result_schema,
        )

    async def _traced_generate(
        self,
        request: GenerationRequest,
        *,
        observer: ProgressObserver | None,
        mode: GenerationMode = "full",
        injected_schema: Mapping[str, object] | None = None,
    ) -> Any:
        """Trace one request across every generation mode."""

        request_id = str(uuid4())
        progress = ProgressEmitter(request_id=request_id, observer=observer)
        base_attributes = {
            "request_id": request_id,
            "model_name": self.settings.model_name,
            "model_extra_body_configured": self.settings.extra_body is not None,
            "model_extra_body_sha256": model_extra_body_sha256(
                self.settings.extra_body,
            ),
            "prompt_version": PROMPT_VERSION,
            "command_output_count": len(request.command_outputs),
            "input_char_count": sum(
                len(item) for item in request.command_outputs
            ),
            "policy_total_timeout_seconds": self.policy.total_timeout_seconds,
            "policy_max_agent_rounds": self.policy.max_agent_rounds,
            "policy_max_ttp_submissions": self.policy.max_ttp_submissions,
            "policy_ttp_validation_timeout_seconds": (
                self.policy.ttp_validation_timeout_seconds
            ),
            "policy_model_input_char_budget": self.policy.model_input_char_budget,
            "policy_max_schema_evidence": self.policy.max_schema_evidence,
            "generation_mode": mode,
        }
        if progress.enabled:
            progress.custom(
                "cli_parser.generation.started",
                {
                    "request": request.model_dump(mode="json"),
                    "model_name": self.settings.model_name,
                    "prompt_version": PROMPT_VERSION,
                },
                phase="generation",
                sensitive=True,
            )

        with start_laminar_span(
            "ttp.generate",
            input=request.model_dump(mode="json"),
            tags=("ttp-generation",),
            attributes=base_attributes,
        ) as span_scope:
            try:
                if observer is None:
                    result = await self._generate(
                        request,
                        request_id=request_id,
                        mode=mode,
                        injected_schema=injected_schema,
                    )
                else:
                    result = await self._generate(
                        request,
                        request_id=request_id,
                        progress=progress,
                        mode=mode,
                        injected_schema=injected_schema,
                    )
            except asyncio.CancelledError as error:
                if progress.enabled:
                    progress.custom(
                        "cli_parser.generation.cancelled",
                        {"status": "cancelled"},
                        phase="generation",
                        sensitive=False,
                    )
                trace_metadata = (
                    {
                        **base_attributes,
                        "termination_reason": "cancelled",
                        "status": "cancelled",
                    }
                    if span_scope.creates_trace
                    else None
                )
                finish_laminar_span(
                    output={
                        "status": "cancelled",
                        "exception_type": type(error).__name__,
                    },
                    outcome="cancelled",
                    attributes={
                        "termination_reason": "cancelled",
                        "status": "cancelled",
                        "exception_type": type(error).__name__,
                    },
                    trace_metadata=trace_metadata,
                )
                raise
            except BaseException as error:
                if progress.enabled:
                    progress.custom(
                        "cli_parser.generation.exception",
                        {
                            "status": "failed",
                            "exception_type": type(error).__name__,
                        },
                        phase="generation",
                        sensitive=False,
                    )
                trace_metadata = (
                    {
                        **base_attributes,
                        "termination_reason": "exception",
                        "status": "failed",
                    }
                    if span_scope.creates_trace
                    else None
                )
                finish_laminar_span(
                    output={
                        "status": "failed",
                        "exception_type": type(error).__name__,
                    },
                    outcome="exception",
                    attributes={
                        "termination_reason": "exception",
                        "status": "failed",
                        "exception_type": type(error).__name__,
                    },
                    trace_metadata=trace_metadata,
                )
                raise

            if progress.enabled:
                progress.custom(
                    "cli_parser.generation.completed",
                    {"result": result.model_dump(mode="json")},
                    phase="generation",
                    sensitive=True,
                )
            result_metadata = result.metadata
            # A schema-only result has no artifact or last_attempt, so the
            # funnel attributes are read tolerantly rather than duplicating the
            # whole tracing skeleton per result type.
            artifact = getattr(result, "artifact", None)
            last_attempt = getattr(result, "last_attempt", None)
            proposal = getattr(result, "proposal", None)
            final_attributes = {
                "request_id": result_metadata.request_id,
                "model_name": result_metadata.model_name,
                "model_extra_body_configured": (
                    self.settings.extra_body is not None
                ),
                "model_extra_body_sha256": model_extra_body_sha256(
                    self.settings.extra_body,
                ),
                "prompt_version": result_metadata.prompt_version,
                "command_output_count": result_metadata.command_output_count,
                "schema_sampled_char_count": (
                    result_metadata.schema_sampled_char_count
                ),
                "ttp_sampled_char_count": result_metadata.ttp_sampled_char_count,
                "agent_rounds": result_metadata.agent_rounds,
                "schema_agent_rounds": result_metadata.schema_agent_rounds,
                "ttp_agent_rounds": result_metadata.ttp_agent_rounds,
                "schema_submissions": result_metadata.schema_submissions,
                "ttp_submissions": result_metadata.ttp_submissions,
                "termination_reason": result_metadata.termination_reason or "",
                "fault_domain": result_metadata.fault_domain or "",
                "schema_frozen": bool(
                    artifact is not None
                    or proposal is not None
                    or result_metadata.ttp_agent_rounds > 0
                ),
                "entered_ttp": bool(
                    result_metadata.ttp_agent_rounds > 0
                    or result_metadata.ttp_sampled_char_count > 0
                ),
                "valid_ttp_candidate": bool(
                    artifact is not None
                    or (
                        last_attempt is not None
                        and last_attempt.ttp_template is not None
                    )
                ),
                "finish_called": result_metadata.termination_reason == "success",
                "status": result.status,
            }
            finish_laminar_span(
                output=result.model_dump(mode="json"),
                outcome=result.status,
                attributes=final_attributes,
                trace_metadata=(final_attributes if span_scope.creates_trace else None),
            )
            return result

    async def _generate(
        self,
        request: GenerationRequest,
        *,
        request_id: str,
        progress: ProgressEmitter | None = None,
        mode: GenerationMode = "full",
        injected_schema: Mapping[str, object] | None = None,
    ) -> Any:
        """Delegate one validated request to its private workflow."""

        workflow = _GenerationWorkflow(
            settings=self.settings,
            policy=self.policy,
            request=request,
            request_id=request_id,
            mode=mode,
            injected_schema=(
                None if injected_schema is None else dict(injected_schema)
            ),
            progress=(
                progress
                if progress is not None
                else ProgressEmitter(request_id=request_id)
            ),
        )
        if mode == "schema_only":
            return await workflow.run_schema_only()
        return await workflow.run()


__all__ = ["TtpGenerator"]
