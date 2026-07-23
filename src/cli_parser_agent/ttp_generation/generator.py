"""Public facade for one schema-then-TTP generation request."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from uuid import uuid4

from ..config import GenerationPolicy, TtpGeneratorSettings
from ..observability import (
    finish_laminar_span,
    initialize_laminar_from_env,
    start_laminar_span,
)
from .agent import PROMPT_VERSION
from .contracts import GenerationRequest, GenerationResult
from .progress import ProgressEmitter, ProgressObserver
from .workflow import _GenerationWorkflow


class TtpGenerator:
    """Generate and independently accept one TTP artifact bundle."""

    def __init__(
        self,
        *,
        settings: TtpGeneratorSettings,
        policy: GenerationPolicy | None = None,
        _laminar_environ: Mapping[str, str] | None = None,
    ) -> None:
        self.settings = TtpGeneratorSettings.model_validate(settings)
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

        request = GenerationRequest.model_validate(request)
        request_id = str(uuid4())
        progress = ProgressEmitter(request_id=request_id, observer=observer)
        base_attributes = {
            "request_id": request_id,
            "model_name": self.settings.model_name,
            "prompt_version": PROMPT_VERSION,
            "command_output_count": len(request.command_outputs),
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
                    )
                else:
                    result = await self._generate(
                        request,
                        request_id=request_id,
                        progress=progress,
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
            final_attributes = {
                "request_id": result_metadata.request_id,
                "model_name": result_metadata.model_name,
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
    ) -> GenerationResult:
        """Delegate one validated request to its private workflow."""

        return await _GenerationWorkflow(
            settings=self.settings,
            policy=self.policy,
            request=request,
            request_id=request_id,
            progress=(
                progress
                if progress is not None
                else ProgressEmitter(request_id=request_id)
            ),
        ).run()


__all__ = ["TtpGenerator"]
