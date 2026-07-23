"""Private request workflow for schema-then-TTP generation."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable, Mapping, Sequence
from copy import deepcopy
from dataclasses import dataclass
from typing import Any, TypeVar

from pydantic import ValidationError

from ..config import GenerationPolicy, TtpGeneratorSettings
from ..observability import (
    current_laminar_trace_id,
    finish_laminar_span,
    start_laminar_span,
)
from .agent import (
    PROMPT_VERSION,
    AgentRunOutcome,
    GenerationPhase,
    GenerationSession,
    SchemaCandidate,
    TemplateCandidate,
    ValidatorOutcome,
    build_agent,
    build_schema_task_message,
    build_schema_task_prompt,
    build_ttp_task_message,
    build_ttp_task_prompt,
    estimate_initial_model_tokens,
    run_generation_phase,
)
from .contracts import (
    ArtifactBundle,
    FieldEvidence,
    GenerationMetadata,
    GenerationRequest,
    GenerationResult,
    LastAttempt,
    ValidationIssue,
)
from .sampling import (
    TRUNCATION_MARKER,
    SampledCommandOutput,
    sample_command_outputs,
)
from .validation import validate_schema_proposal, validate_ttp_template

_T = TypeVar("_T")
# Leave room for the model completion and tokenizer variance beyond
# AgentScope's byte-based estimate.
_INITIAL_CONTEXT_RATIO = 0.5


@dataclass(frozen=True, slots=True)
class _PhaseExecution:
    """One phase's result, including failures before its first model call."""

    deadline_completed: bool
    outcome: AgentRunOutcome | None = None
    terminal_result: GenerationResult | None = None

    @property
    def phase_completed(self) -> bool:
        return bool(
            self.deadline_completed
            and self.outcome is not None
            and self.outcome.phase_completed
            and self.terminal_result is None
        )


async def _fit_sampled_outputs(
    command_outputs: Sequence[str],
    *,
    total_char_budget: int,
    max_initial_tokens: int,
    serialize_prompt: Callable[[Sequence[str]], str],
    estimate_tokens: Callable[[Sequence[str]], Awaitable[int]],
) -> tuple[list[SampledCommandOutput], bool]:
    """Fit raw samples to serialized-char and initial model-token budgets."""

    raw_budget = total_char_budget
    while True:
        sampled = sample_command_outputs(
            command_outputs,
            total_char_budget=raw_budget,
        )
        texts = [item.text for item in sampled]
        if any(
            item.truncated and item.allocated_char_budget <= len(TRUNCATION_MARKER)
            for item in sampled
        ):
            # A truncated marker without any source character is not a usable
            # view of that input. The fitter can only shrink from here.
            return sampled, False
        serialized_chars = len(serialize_prompt(texts))
        estimated_tokens = await estimate_tokens(texts)
        if (
            serialized_chars <= total_char_budget
            and estimated_tokens <= max_initial_tokens
        ):
            return sampled, True
        if raw_budget <= 1:
            return sampled, False

        char_ratio = total_char_budget / max(1, serialized_chars)
        token_ratio = max_initial_tokens / max(1, estimated_tokens)
        scale = min(char_ratio, token_ratio, 1.0)
        next_budget = int(raw_budget * scale * 0.9)
        raw_budget = max(1, min(raw_budget - 1, next_budget))
        await asyncio.sleep(0)


async def _run_traced_generation_phase(
    *,
    operation: Callable[[], Awaitable[_PhaseExecution]],
    session: GenerationSession,
    phase: GenerationPhase,
    span_input: Mapping[str, Any],
    request_id: str,
) -> _PhaseExecution:
    """Run all construction, fitting, and model work inside a phase span."""

    rounds_before = session.agent_rounds
    span_name = f"{phase}.phase"
    base_attributes = {
        "request_id": request_id,
        "phase": phase,
    }
    with start_laminar_span(
        span_name,
        input=dict(span_input),
        tags=(f"{phase}-phase",),
        attributes=base_attributes,
    ):
        try:
            execution = await operation()
        except asyncio.CancelledError as error:
            finish_laminar_span(
                output={
                    "status": "cancelled",
                    "phase": phase,
                    "exception_type": type(error).__name__,
                },
                outcome="cancelled",
                attributes={
                    **base_attributes,
                    "status": "cancelled",
                    "agent_rounds": session.agent_rounds - rounds_before,
                },
            )
            raise
        except BaseException as error:
            finish_laminar_span(
                output={
                    "status": "failed",
                    "phase": phase,
                    "exception_type": type(error).__name__,
                },
                outcome="exception",
                attributes={
                    **base_attributes,
                    "status": "failed",
                    "agent_rounds": session.agent_rounds - rounds_before,
                    "exception_type": type(error).__name__,
                },
            )
            raise

        phase_completed = execution.phase_completed
        status = "success" if phase_completed else "failed"
        termination_reason = (
            execution.terminal_result.metadata.termination_reason
            if execution.terminal_result is not None
            else session.terminal_reason or ""
        )
        if not termination_reason and not execution.deadline_completed:
            termination_reason = "generation_timeout"
        elif not termination_reason and execution.outcome is not None:
            if execution.outcome.model_no_tool_retry_limit:
                termination_reason = "model_no_tool_retry_limit"
            elif execution.outcome.exceeded_max_iters:
                termination_reason = "agent_round_limit"
            else:
                termination_reason = "agent_stopped"
        finish_laminar_span(
            output={
                "status": status,
                "phase": phase,
                "phase_completed": phase_completed,
                "deadline_completed": execution.deadline_completed,
                "agent_rounds": session.agent_rounds - rounds_before,
                "termination_reason": termination_reason,
            },
            outcome=status,
            attributes={
                **base_attributes,
                "status": status,
                "phase_completed": phase_completed,
                "agent_rounds": session.agent_rounds - rounds_before,
                "schema_submissions": session.schema_submissions,
                "ttp_submissions": session.ttp_submissions,
                "termination_reason": termination_reason,
            },
        )
        return execution


def _issue(
    code: str,
    message: str,
    *,
    stage: str,
    details: dict[str, Any] | None = None,
) -> ValidationIssue:
    return ValidationIssue(
        code=code,
        message=message,
        stage=stage,
        details=details or {},
    )


def _normalise_issues(
    values: Sequence[Any],
    *,
    fallback_stage: str,
) -> list[ValidationIssue]:
    """Retain only bounded contract fields from request-local validators."""

    issues: list[ValidationIssue] = []
    for value in values:
        if isinstance(value, ValidationIssue):
            issues.append(value)
            continue
        if hasattr(value, "model_dump"):
            value = value.model_dump(mode="python")
        if not isinstance(value, Mapping):
            issues.append(
                _issue(
                    "generation.invalid_validator_issue",
                    "A validator returned an issue in an unsupported format.",
                    stage=fallback_stage,
                ),
            )
            continue

        candidate = {
            key: value[key]
            for key in (
                "code",
                "message",
                "severity",
                "stage",
                "path",
                "output_index",
                "details",
            )
            if key in value
        }
        candidate.setdefault("stage", fallback_stage)
        try:
            issues.append(ValidationIssue.model_validate(candidate))
        except ValidationError:
            issues.append(
                _issue(
                    "generation.invalid_validator_issue",
                    "A validator returned an issue that violates the result contract.",
                    stage=fallback_stage,
                ),
            )
    return issues


def _last_attempt(session: GenerationSession) -> LastAttempt | None:
    schema = session.frozen_schema or session.last_result_schema
    if session.last_ttp_template is None and schema is None:
        return None
    return LastAttempt(
        ttp_template=session.last_ttp_template,
        result_schema=deepcopy(schema),
    )


def _exception_nodes(error: BaseException) -> list[BaseException]:
    nodes: list[BaseException] = []
    pending = [error]
    seen: set[int] = set()
    while pending:
        current = pending.pop()
        if id(current) in seen:
            continue
        seen.add(id(current))
        nodes.append(current)
        nested = getattr(current, "exceptions", ())
        if isinstance(nested, tuple):
            pending.extend(item for item in nested if isinstance(item, BaseException))
        if current.__cause__ is not None:
            pending.append(current.__cause__)
        elif current.__context__ is not None:
            pending.append(current.__context__)
    return nodes


def _is_model_timeout(error: BaseException) -> bool:
    return any(
        "timeout" in type(node).__name__.lower() for node in _exception_nodes(error)
    )


def _is_model_error(error: BaseException) -> bool:
    for node in _exception_nodes(error):
        module = type(node).__module__.lower()
        name = type(node).__name__.lower()
        if module.startswith(("openai", "httpx", "httpcore")):
            return True
        if any(token in name for token in ("apierror", "connectionerror")):
            return True
    return False


async def _cancel_and_drain(task: asyncio.Task[Any]) -> None:
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    except Exception:
        # The caller is already handling timeout/cancellation; draining only
        # prevents an unobserved task exception.
        pass


async def _run_before_deadline(
    operation: Callable[[], Awaitable[_T]],
    *,
    deadline_monotonic: float,
) -> tuple[bool, _T | None]:
    """Run an operation under a watchdog that AgentScope cannot swallow."""

    remaining = deadline_monotonic - time.monotonic()
    if remaining <= 0:
        return False, None

    task = asyncio.create_task(operation())
    try:
        done, _ = await asyncio.wait({task}, timeout=remaining)
    except asyncio.CancelledError:
        await _cancel_and_drain(task)
        raise

    if task not in done:
        await _cancel_and_drain(task)
        return False, None
    return True, task.result()


class _GenerationWorkflow:
    """Own one request's state, phase execution, and final acceptance."""

    def __init__(
        self,
        *,
        settings: TtpGeneratorSettings,
        policy: GenerationPolicy,
        request: GenerationRequest,
        request_id: str,
    ) -> None:
        self.settings = settings
        self.policy = policy
        self.request = request
        self.request_id = request_id
        self.started = time.monotonic()
        self.deadline = self.started + policy.total_timeout_seconds
        self.schema_sampled: list[SampledCommandOutput] = []
        self.ttp_sampled: list[SampledCommandOutput] = []
        self.session = GenerationSession(
            command_outputs=tuple(request.command_outputs),
            schema_validator=self._schema_validator,
            template_validator=self._template_validator,
            max_ttp_submissions=policy.max_ttp_submissions,
            max_agent_rounds=policy.max_agent_rounds,
            max_schema_no_tool_retries=policy.max_schema_no_tool_retries,
            max_ttp_no_tool_retries=policy.max_ttp_no_tool_retries,
            deadline_monotonic=self.deadline,
        )

    def _validate_schema_candidate(
        self,
        candidate: SchemaCandidate,
    ) -> ValidatorOutcome:
        evidence: list[FieldEvidence] = []
        evidence_issues: list[ValidationIssue] = []
        for item in candidate.evidence:
            try:
                parsed = FieldEvidence.model_validate(item)
            except ValidationError:
                evidence_issues.append(
                    _issue(
                        "schema.invalid_evidence",
                        "Field evidence does not satisfy the submission contract.",
                        stage="schema",
                    ),
                )
                continue
            if len(parsed.excerpt) > self.policy.max_evidence_excerpt_chars:
                evidence_issues.append(
                    _issue(
                        "schema.evidence_excerpt_too_long",
                        "Field evidence exceeds the configured character limit.",
                        stage="schema",
                    ),
                )
                continue
            evidence.append(parsed)
        if evidence_issues:
            return ValidatorOutcome(valid=False, issues=tuple(evidence_issues))

        issues = validate_schema_proposal(
            candidate.result_schema,
            evidence,
            candidate.command_outputs,
            max_schema_bytes=self.policy.max_schema_bytes,
            max_schema_depth=self.policy.max_schema_depth,
            max_schema_properties=self.policy.max_schema_properties,
        )
        return ValidatorOutcome(valid=not issues, issues=tuple(issues))

    async def _schema_validator(
        self,
        candidate: SchemaCandidate,
    ) -> ValidatorOutcome:
        if time.monotonic() >= self.deadline:
            issue = _issue(
                "generation.timeout",
                "The total generation time budget was exhausted.",
                stage="budget",
            )
            return ValidatorOutcome(valid=False, issues=(issue,))
        return await asyncio.to_thread(self._validate_schema_candidate, candidate)

    async def _template_validator(
        self,
        candidate: TemplateCandidate,
    ) -> ValidatorOutcome:
        remaining = max(0.0, self.deadline - time.monotonic())
        if remaining <= 0:
            issue = _issue(
                "generation.timeout",
                "The total generation time budget was exhausted.",
                stage="budget",
            )
            return ValidatorOutcome(valid=False, issues=(issue,))
        timeout = min(self.policy.ttp_validation_timeout_seconds, remaining)
        outcome = await asyncio.to_thread(
            validate_ttp_template,
            candidate.ttp_template,
            candidate.command_outputs,
            candidate.result_schema,
            timeout_seconds=timeout,
            max_result_bytes=self.policy.max_parse_result_bytes,
            max_ttp_template_bytes=self.policy.max_ttp_template_bytes,
            max_ttp_group_depth=self.policy.max_ttp_group_depth,
            max_ttp_regex_chars=self.policy.max_ttp_regex_chars,
            max_ttp_argument_chars=self.policy.max_ttp_argument_chars,
            max_schema_bytes=self.policy.max_schema_bytes,
            max_schema_depth=self.policy.max_schema_depth,
            max_schema_properties=self.policy.max_schema_properties,
        )
        fatal_worker_codes = {
            "ttp.worker_bootstrap_failed",
            "ttp.worker_host_unsupported",
            "ttp.worker_start_failed",
        }
        if any(issue.code in fatal_worker_codes for issue in outcome.issues):
            self.session.terminal_reason = "ttp_worker_unavailable"
        return ValidatorOutcome(
            valid=outcome.valid,
            issues=tuple(outcome.issues),
            records=tuple(outcome.records),
        )

    def _metadata(self, termination_reason: str) -> GenerationMetadata:
        return GenerationMetadata(
            request_id=self.request_id,
            model_name=self.settings.model_name,
            prompt_version=PROMPT_VERSION,
            command_output_count=len(self.request.command_outputs),
            input_char_count=sum(len(item) for item in self.request.command_outputs),
            schema_sampled_char_count=sum(
                len(item.text) for item in self.schema_sampled
            ),
            ttp_sampled_char_count=sum(len(item.text) for item in self.ttp_sampled),
            elapsed_seconds=max(0.0, time.monotonic() - self.started),
            agent_rounds=self.session.agent_rounds,
            schema_agent_rounds=self.session.schema_agent_rounds,
            ttp_agent_rounds=self.session.ttp_agent_rounds,
            tool_call_starts=self.session.tool_call_starts,
            tool_result_errors=self.session.tool_result_errors,
            schema_submissions=self.session.schema_submissions,
            ttp_submissions=self.session.ttp_submissions,
            schema_no_tool_responses=self.session.schema_no_tool_responses,
            ttp_no_tool_responses=self.session.ttp_no_tool_responses,
            schema_no_tool_retries=self.session.schema_no_tool_retries,
            ttp_no_tool_retries=self.session.ttp_no_tool_retries,
            first_ttp_passed=self.session.first_ttp_valid,
            termination_reason=termination_reason,
            laminar_trace_id=current_laminar_trace_id(),
        )

    def _failure(
        self,
        termination_reason: str,
        issues: Sequence[ValidationIssue],
    ) -> GenerationResult:
        return GenerationResult(
            status="failed",
            issues=list(issues),
            metadata=self._metadata(termination_reason),
            last_attempt=_last_attempt(self.session),
        )

    def _phase_exception_failure(
        self,
        error: Exception,
        phase: GenerationPhase,
    ) -> GenerationResult:
        if _is_model_timeout(error):
            issue = _issue(
                "model.timeout",
                "The configured model request exceeded its timeout.",
                stage="model",
                details={"phase": phase},
            )
            reason = "model_timeout"
        elif _is_model_error(error):
            issue = _issue(
                "model.request_failed",
                "The configured model request failed.",
                stage="model",
                details={
                    "exception_type": type(error).__name__,
                    "phase": phase,
                },
            )
            reason = "model_error"
        else:
            issue = _issue(
                "generation.internal_error",
                "Generation stopped because an internal component failed.",
                stage="generation",
                details={
                    "exception_type": type(error).__name__,
                    "phase": phase,
                },
            )
            reason = "internal_error"
        previous = _normalise_issues(
            self.session.last_issues,
            fallback_stage=phase,
        )
        return self._failure(reason, [*previous, issue])

    def _phase_stopped_failure(
        self,
        phase: GenerationPhase,
        run_outcome: AgentRunOutcome | None,
        *,
        invalid_calls_before: int,
    ) -> GenerationResult:
        previous = _normalise_issues(
            self.session.last_issues,
            fallback_stage=phase,
        )
        if phase == "ttp" and self.session.terminal_reason == "ttp_worker_unavailable":
            issue = _issue(
                "generation.ttp_worker_unavailable",
                "The isolated TTP validation worker is unavailable.",
                stage="generation",
            )
            reason = "ttp_worker_unavailable"
        elif (
            phase == "ttp"
            and self.session.ttp_submissions >= self.policy.max_ttp_submissions
        ):
            issue = _issue(
                "generation.ttp_submission_limit",
                "The TTP template submission budget was exhausted.",
                stage="budget",
            )
            reason = "ttp_submission_limit"
        elif self.session.terminal_reason == "model_no_tool_retry_limit":
            issue = _issue(
                "model.submission_tool_not_called",
                "The model did not call an allowed tool for the current phase "
                "before the no-tool retry limit was exhausted.",
                stage="model",
            )
            reason = "model_no_tool_retry_limit"
        elif (
            run_outcome is not None and run_outcome.exceeded_max_iters
        ) or self.session.agent_rounds >= self.policy.max_agent_rounds:
            phase_submissions = (
                self.session.schema_submissions
                if phase == "schema"
                else self.session.ttp_submissions
            )
            invalid_tool_call_observed = (
                self.session.submission_tool_call_invalids > invalid_calls_before
            )
            ended_after_invalid_tool_call = bool(
                run_outcome is not None
                and run_outcome.ended_after_invalid_tool_call
            )
            if invalid_tool_call_observed and (
                phase_submissions == 0 or ended_after_invalid_tool_call
            ):
                issue = _issue(
                    "model.submission_tool_call_invalid",
                    "The model produced invalid arguments for an allowed tool "
                    "in the current phase.",
                    stage="model",
                )
                reason = "model_submission_tool_call_invalid"
            else:
                issue = _issue(
                    "generation.agent_round_limit",
                    "The AgentScope reasoning round budget was exhausted.",
                    stage="budget",
                )
                reason = "agent_round_limit"
        else:
            issue = _issue(
                "generation.agent_stopped",
                "The AgentScope loop ended before the current generation phase "
                "was explicitly completed.",
                stage="generation",
            )
            reason = "agent_stopped"
        return self._failure(reason, [*previous, issue])

    async def _fit_and_run_phase(
        self,
        *,
        phase: GenerationPhase,
        serialize_prompt: Callable[[Sequence[str]], str],
        build_message: Callable[[Sequence[str]], Any],
        span_input: Mapping[str, Any],
    ) -> _PhaseExecution:
        async def operation() -> _PhaseExecution:
            max_initial_tokens = min(
                int(self.settings.context_size * _INITIAL_CONTEXT_RATIO),
                self.settings.context_size - self.settings.max_tokens,
            )
            if max_initial_tokens <= 0:
                issue = _issue(
                    "model.context_budget_too_small",
                    "Model context settings leave no room for the initial request.",
                    stage="model",
                    details={"phase": phase},
                )
                return _PhaseExecution(
                    deadline_completed=True,
                    terminal_result=self._failure("model_context_budget", [issue]),
                )

            agent = build_agent(
                settings=self.settings,
                policy=self.policy,
                session=self.session,
                phase=phase,
            )

            async def estimate_tokens(texts: Sequence[str]) -> int:
                return await estimate_initial_model_tokens(
                    agent,
                    build_message(texts),
                    phase,
                )

            fit_completed, fit_result = await _run_before_deadline(
                lambda: _fit_sampled_outputs(
                    tuple(self.request.command_outputs),
                    total_char_budget=self.policy.model_input_char_budget,
                    max_initial_tokens=max_initial_tokens,
                    serialize_prompt=serialize_prompt,
                    estimate_tokens=estimate_tokens,
                ),
                deadline_monotonic=self.deadline,
            )
            if (
                not fit_completed
                or fit_result is None
                or time.monotonic() >= self.deadline
            ):
                issue = _issue(
                    "generation.timeout",
                    "The total generation time budget expired during input sampling.",
                    stage="budget",
                    details={"phase": phase},
                )
                return _PhaseExecution(
                    deadline_completed=False,
                    terminal_result=self._failure("generation_timeout", [issue]),
                )

            candidate_sample, input_fits = fit_result
            if not input_fits:
                issue = _issue(
                    "model.context_budget_exceeded",
                    "The serialized request cannot fit the configured model context.",
                    stage="model",
                    details={"phase": phase},
                )
                return _PhaseExecution(
                    deadline_completed=True,
                    terminal_result=self._failure("model_context_budget", [issue]),
                )

            if phase == "schema":
                self.schema_sampled = candidate_sample
            else:
                self.ttp_sampled = candidate_sample
            texts = [item.text for item in candidate_sample]
            message = build_message(texts)
            completed, outcome = await _run_before_deadline(
                lambda: run_generation_phase(
                    agent,
                    message,
                    self.session,
                    phase,
                ),
                deadline_monotonic=self.deadline,
            )
            return _PhaseExecution(
                deadline_completed=(completed and time.monotonic() < self.deadline),
                outcome=outcome,
            )

        try:
            return await _run_traced_generation_phase(
                operation=operation,
                session=self.session,
                phase=phase,
                span_input=span_input,
                request_id=self.request_id,
            )
        except asyncio.CancelledError:
            raise
        except Exception as error:
            return _PhaseExecution(
                deadline_completed=True,
                terminal_result=self._phase_exception_failure(error, phase),
            )

    async def _run_schema_phase(self) -> _PhaseExecution:
        return await self._fit_and_run_phase(
            phase="schema",
            serialize_prompt=build_schema_task_prompt,
            build_message=build_schema_task_message,
            span_input={"command_output_count": len(self.request.command_outputs)},
        )

    async def _run_ttp_phase(
        self,
        frozen_result_schema: Mapping[str, Any],
    ) -> _PhaseExecution:
        def serialize_prompt(texts: Sequence[str]) -> str:
            return build_ttp_task_prompt(texts, frozen_result_schema)

        def build_message(texts: Sequence[str]) -> Any:
            return build_ttp_task_message(texts, frozen_result_schema)

        return await self._fit_and_run_phase(
            phase="ttp",
            serialize_prompt=serialize_prompt,
            build_message=build_message,
            span_input={
                "frozen_result_schema": dict(frozen_result_schema),
                "command_output_count": len(self.request.command_outputs),
            },
        )

    def _resolve_phase_execution(
        self,
        phase: GenerationPhase,
        execution: _PhaseExecution,
        *,
        invalid_calls_before: int,
    ) -> GenerationResult | None:
        if execution.terminal_result is not None:
            return execution.terminal_result
        if not execution.deadline_completed or time.monotonic() >= self.deadline:
            issue = _issue(
                "generation.timeout",
                "The total generation time budget was exhausted.",
                stage="budget",
                details={"phase": phase},
            )
            previous = _normalise_issues(
                self.session.last_issues,
                fallback_stage=phase,
            )
            return self._failure("generation_timeout", [*previous, issue])

        phase_succeeded = (
            self.session.schema_is_frozen
            if phase == "schema"
            else self.session.succeeded
        )
        if phase_succeeded:
            return None
        return self._phase_stopped_failure(
            phase,
            execution.outcome,
            invalid_calls_before=invalid_calls_before,
        )

    def _validate_frozen_schema(self) -> list[ValidationIssue]:
        try:
            frozen_evidence = [
                FieldEvidence.model_validate(item)
                for item in self.session.field_evidence
            ]
        except ValidationError:
            return [
                _issue(
                    "schema.invalid_frozen_evidence",
                    "Frozen field evidence failed final validation.",
                    stage="acceptance",
                ),
            ]
        return validate_schema_proposal(
            self.session.frozen_schema,
            frozen_evidence,
            tuple(self.request.command_outputs),
            max_schema_bytes=self.policy.max_schema_bytes,
            max_schema_depth=self.policy.max_schema_depth,
            max_schema_properties=self.policy.max_schema_properties,
        )

    async def _accept_artifact(self) -> GenerationResult:
        try:
            schema_completed, schema_issues = await _run_before_deadline(
                lambda: asyncio.to_thread(self._validate_frozen_schema),
                deadline_monotonic=self.deadline,
            )
        except asyncio.CancelledError:
            raise
        except Exception as error:
            issue = _issue(
                "generation.final_validation_error",
                "Final schema acceptance failed in the deterministic validator.",
                stage="acceptance",
                details={"exception_type": type(error).__name__},
            )
            return self._failure("final_validation_failed", [issue])
        if (
            not schema_completed
            or schema_issues is None
            or time.monotonic() >= self.deadline
        ):
            issue = _issue(
                "generation.timeout",
                "The total generation time budget expired during final "
                "schema acceptance.",
                stage="budget",
            )
            return self._failure("generation_timeout", [issue])
        if schema_issues:
            return self._failure("final_validation_failed", schema_issues)

        final_timeout = min(
            self.policy.ttp_validation_timeout_seconds,
            max(0.0, self.deadline - time.monotonic()),
        )
        try:
            completed, acceptance = await _run_before_deadline(
                lambda: asyncio.to_thread(
                    validate_ttp_template,
                    self.session.validated_ttp_template,
                    tuple(self.request.command_outputs),
                    self.session.frozen_schema,
                    timeout_seconds=final_timeout,
                    max_result_bytes=self.policy.max_parse_result_bytes,
                    max_ttp_template_bytes=self.policy.max_ttp_template_bytes,
                    max_ttp_group_depth=self.policy.max_ttp_group_depth,
                    max_ttp_regex_chars=self.policy.max_ttp_regex_chars,
                    max_ttp_argument_chars=self.policy.max_ttp_argument_chars,
                    max_schema_bytes=self.policy.max_schema_bytes,
                    max_schema_depth=self.policy.max_schema_depth,
                    max_schema_properties=self.policy.max_schema_properties,
                ),
                deadline_monotonic=self.deadline,
            )
        except asyncio.CancelledError:
            raise
        except Exception as error:
            issue = _issue(
                "generation.final_validation_error",
                "Final acceptance stopped because its isolated validator failed.",
                stage="acceptance",
                details={"exception_type": type(error).__name__},
            )
            return self._failure("final_validation_failed", [issue])
        if not completed or acceptance is None or time.monotonic() >= self.deadline:
            issue = _issue(
                "generation.timeout",
                "The total generation time budget expired during final acceptance.",
                stage="budget",
            )
            return self._failure("generation_timeout", [issue])

        final_issues = _normalise_issues(
            acceptance.issues,
            fallback_stage="acceptance",
        )
        if not acceptance.valid:
            return self._failure("final_validation_failed", final_issues)

        def build_artifact() -> ArtifactBundle:
            return ArtifactBundle(
                ttp_template=self.session.validated_ttp_template,
                result_schema=deepcopy(self.session.frozen_schema),
                records=deepcopy(acceptance.records),
                assumptions=list(self.session.assumptions),
            )

        try:
            artifact_completed, artifact = await _run_before_deadline(
                lambda: asyncio.to_thread(build_artifact),
                deadline_monotonic=self.deadline,
            )
        except asyncio.CancelledError:
            raise
        except Exception as error:
            issue = _issue(
                "generation.artifact_error",
                "Validated records could not be packaged into the result contract.",
                stage="acceptance",
                details={"exception_type": type(error).__name__},
            )
            return self._failure("final_validation_failed", [issue])
        if (
            not artifact_completed
            or artifact is None
            or time.monotonic() >= self.deadline
        ):
            issue = _issue(
                "generation.timeout",
                "The total generation time budget expired while packaging "
                "the artifact.",
                stage="budget",
            )
            return self._failure("generation_timeout", [issue])
        return GenerationResult(
            status="success",
            artifact=artifact,
            metadata=self._metadata("success"),
        )

    async def run(self) -> GenerationResult:
        """Run Schema, TTP, and final full-input acceptance in order."""

        schema_invalid_calls = self.session.submission_tool_call_invalids
        schema_execution = await self._run_schema_phase()
        phase_failure = self._resolve_phase_execution(
            "schema",
            schema_execution,
            invalid_calls_before=schema_invalid_calls,
        )
        if phase_failure is not None:
            return phase_failure

        if self.session.agent_rounds >= self.policy.max_agent_rounds:
            issue = _issue(
                "generation.agent_round_limit",
                "The AgentScope reasoning round budget was exhausted after "
                "the Schema phase.",
                stage="budget",
                details={"phase": "schema", "blocked_phase": "ttp"},
            )
            return self._failure("agent_round_limit", [issue])
        if self.session.frozen_schema is None:
            issue = _issue(
                "generation.invalid_session_state",
                "The Schema phase completed without a frozen result schema.",
                stage="generation",
            )
            return self._failure("internal_error", [issue])

        frozen_result_schema = deepcopy(self.session.frozen_schema)
        ttp_invalid_calls = self.session.submission_tool_call_invalids
        ttp_execution = await self._run_ttp_phase(frozen_result_schema)
        phase_failure = self._resolve_phase_execution(
            "ttp",
            ttp_execution,
            invalid_calls_before=ttp_invalid_calls,
        )
        if phase_failure is not None:
            return phase_failure

        if (
            self.session.frozen_schema is None
            or self.session.validated_ttp_template is None
        ):
            issue = _issue(
                "generation.invalid_session_state",
                "Generation reached an inconsistent terminal state.",
                stage="generation",
            )
            return self._failure("internal_error", [issue])
        return await self._accept_artifact()


__all__: list[str] = []
