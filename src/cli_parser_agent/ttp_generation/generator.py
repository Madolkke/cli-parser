"""Public orchestration for one schema-then-TTP generation request."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable, Mapping, Sequence
from copy import deepcopy
from typing import Any, TypeVar

from pydantic import ValidationError

from ..config import GenerationPolicy, TtpGeneratorSettings
from .agent import (
    PROMPT_VERSION,
    GenerationSession,
    SchemaCandidate,
    TemplateCandidate,
    ValidatorOutcome,
    build_agent,
    build_task_message,
    build_task_prompt,
    estimate_initial_model_tokens,
    run_generation_agent,
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
from .sampling import SampledCommandOutput, sample_command_outputs
from .validation import validate_schema_proposal, validate_ttp_template

_T = TypeVar("_T")
# Leave room for the model completion and tokenizer variance beyond
# AgentScope's byte-based estimate.
_INITIAL_CONTEXT_RATIO = 0.5


async def _fit_sampled_outputs(
    command_outputs: Sequence[str],
    *,
    total_char_budget: int,
    max_initial_tokens: int,
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
        serialized_chars = len(build_task_prompt(texts))
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
    """Run an operation under a watchdog that AgentScope cannot swallow.

    AgentScope converts cancellation inside a model call into an interrupted
    response. A separate task lets this layer distinguish its own wall-clock
    deadline from cancellation requested by the API caller.
    """

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


class TtpGenerator:
    """Generate and independently accept one TTP artifact bundle."""

    def __init__(
        self,
        *,
        settings: TtpGeneratorSettings,
        policy: GenerationPolicy | None = None,
    ) -> None:
        self.settings = TtpGeneratorSettings.model_validate(settings)
        self.policy = (
            GenerationPolicy()
            if policy is None
            else GenerationPolicy.model_validate(policy)
        )

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
        return cls(
            settings=TtpGeneratorSettings.from_env(environ),
            policy=resolved_policy,
        )

    async def generate(self, request: GenerationRequest) -> GenerationResult:
        """Run the request-local AgentScope loop and final full-input acceptance."""

        request = GenerationRequest.model_validate(request)
        started = time.monotonic()
        deadline = started + self.policy.total_timeout_seconds
        sampled: list[SampledCommandOutput] = []

        session: GenerationSession

        def validate_schema_candidate(candidate: SchemaCandidate) -> ValidatorOutcome:
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

        async def schema_validator(candidate: SchemaCandidate) -> ValidatorOutcome:
            if time.monotonic() >= deadline:
                issue = _issue(
                    "generation.timeout",
                    "The total generation time budget was exhausted.",
                    stage="budget",
                )
                return ValidatorOutcome(valid=False, issues=(issue,))
            return await asyncio.to_thread(validate_schema_candidate, candidate)

        async def template_validator(candidate: TemplateCandidate) -> ValidatorOutcome:
            remaining = max(0.0, deadline - time.monotonic())
            if remaining <= 0:
                issue = _issue(
                    "generation.timeout",
                    "The total generation time budget was exhausted.",
                    stage="budget",
                )
                return ValidatorOutcome(valid=False, issues=(issue,))
            timeout = min(
                self.policy.ttp_validation_timeout_seconds,
                remaining,
            )
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
                session.terminal_reason = "ttp_worker_unavailable"
            return ValidatorOutcome(
                valid=outcome.valid,
                issues=tuple(outcome.issues),
                records=tuple(outcome.records),
            )

        session = GenerationSession(
            command_outputs=tuple(request.command_outputs),
            schema_validator=schema_validator,
            template_validator=template_validator,
            max_ttp_submissions=self.policy.max_ttp_submissions,
            max_agent_rounds=self.policy.max_agent_rounds,
            max_schema_no_tool_retries=(self.policy.max_schema_no_tool_retries),
            max_ttp_no_tool_retries=self.policy.max_ttp_no_tool_retries,
            deadline_monotonic=deadline,
        )

        def metadata(termination_reason: str) -> GenerationMetadata:
            return GenerationMetadata(
                model_name=self.settings.model_name,
                prompt_version=PROMPT_VERSION,
                command_output_count=len(request.command_outputs),
                input_char_count=sum(len(item) for item in request.command_outputs),
                sampled_char_count=sum(len(item.text) for item in sampled),
                elapsed_seconds=max(0.0, time.monotonic() - started),
                agent_rounds=session.agent_rounds,
                tool_call_starts=session.tool_call_starts,
                tool_result_errors=session.tool_result_errors,
                schema_submissions=session.schema_submissions,
                ttp_submissions=session.ttp_submissions,
                schema_no_tool_responses=session.schema_no_tool_responses,
                ttp_no_tool_responses=session.ttp_no_tool_responses,
                schema_no_tool_retries=session.schema_no_tool_retries,
                ttp_no_tool_retries=session.ttp_no_tool_retries,
                first_ttp_passed=session.first_ttp_valid,
                termination_reason=termination_reason,
            )

        def failure(
            termination_reason: str,
            issues: Sequence[ValidationIssue],
        ) -> GenerationResult:
            return GenerationResult(
                status="failed",
                issues=list(issues),
                metadata=metadata(termination_reason),
                last_attempt=_last_attempt(session),
            )

        try:
            agent = build_agent(
                settings=self.settings,
                policy=self.policy,
                session=session,
            )
            max_initial_tokens = min(
                int(self.settings.context_size * _INITIAL_CONTEXT_RATIO),
                self.settings.context_size - self.settings.max_tokens,
            )
            if max_initial_tokens <= 0:
                issue = _issue(
                    "model.context_budget_too_small",
                    "Model context settings leave no room for the initial request.",
                    stage="model",
                )
                return failure("model_context_budget", [issue])

            async def estimate_tokens(texts: Sequence[str]) -> int:
                return await estimate_initial_model_tokens(
                    agent,
                    build_task_message(list(texts)),
                )

            fit_completed, fit_result = await _run_before_deadline(
                lambda: _fit_sampled_outputs(
                    tuple(request.command_outputs),
                    total_char_budget=self.policy.model_input_char_budget,
                    max_initial_tokens=max_initial_tokens,
                    estimate_tokens=estimate_tokens,
                ),
                deadline_monotonic=deadline,
            )
            if not fit_completed or fit_result is None or time.monotonic() >= deadline:
                issue = _issue(
                    "generation.timeout",
                    "The total generation time budget expired during input sampling.",
                    stage="budget",
                )
                return failure("generation_timeout", [issue])
            sampled, input_fits = fit_result
            if not input_fits:
                issue = _issue(
                    "model.context_budget_exceeded",
                    "The serialized request cannot fit the configured model context.",
                    stage="model",
                )
                return failure("model_context_budget", [issue])

            message = build_task_message([item.text for item in sampled])
            completed, run_outcome = await _run_before_deadline(
                lambda: run_generation_agent(agent, message, session),
                deadline_monotonic=deadline,
            )
        except asyncio.CancelledError:
            raise
        except Exception as error:
            if _is_model_timeout(error):
                issue = _issue(
                    "model.timeout",
                    "The configured model request exceeded its timeout.",
                    stage="model",
                )
                reason = "model_timeout"
            elif _is_model_error(error):
                issue = _issue(
                    "model.request_failed",
                    "The configured model request failed.",
                    stage="model",
                    details={"exception_type": type(error).__name__},
                )
                reason = "model_error"
            else:
                issue = _issue(
                    "generation.internal_error",
                    "Generation stopped because an internal component failed.",
                    stage="generation",
                    details={"exception_type": type(error).__name__},
                )
                reason = "internal_error"
            previous = _normalise_issues(
                session.last_issues,
                fallback_stage="generation",
            )
            return failure(reason, [*previous, issue])

        if not completed or time.monotonic() >= deadline:
            issue = _issue(
                "generation.timeout",
                "The total generation time budget was exhausted.",
                stage="budget",
            )
            previous = _normalise_issues(
                session.last_issues,
                fallback_stage="generation",
            )
            return failure("generation_timeout", [*previous, issue])

        if not session.succeeded:
            previous = _normalise_issues(
                session.last_issues,
                fallback_stage="generation",
            )
            if session.terminal_reason == "ttp_worker_unavailable":
                issue = _issue(
                    "generation.ttp_worker_unavailable",
                    "The isolated TTP validation worker is unavailable.",
                    stage="generation",
                )
                reason = "ttp_worker_unavailable"
            elif session.ttp_submissions >= self.policy.max_ttp_submissions:
                issue = _issue(
                    "generation.ttp_submission_limit",
                    "The TTP template submission budget was exhausted.",
                    stage="budget",
                )
                reason = "ttp_submission_limit"
            elif session.terminal_reason == "model_no_tool_retry_limit":
                issue = _issue(
                    "model.submission_tool_not_called",
                    "The model did not call the current submission tool before "
                    "the no-tool retry limit was exhausted.",
                    stage="model",
                )
                reason = "model_no_tool_retry_limit"
            elif (
                run_outcome is not None and run_outcome.exceeded_max_iters
            ) or session.agent_rounds >= self.policy.max_agent_rounds:
                current_phase_never_entered = (
                    not session.schema_is_frozen and session.schema_submissions == 0
                ) or (session.schema_is_frozen and session.ttp_submissions == 0)
                if (
                    current_phase_never_entered
                    and session.submission_tool_call_invalids > 0
                ):
                    issue = _issue(
                        "model.submission_tool_call_invalid",
                        "The model repeatedly produced invalid arguments for "
                        "the current submission tool.",
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
                    "The AgentScope loop ended before a validated submission "
                    "was produced.",
                    stage="generation",
                )
                reason = "agent_stopped"
            return failure(reason, [*previous, issue])

        if session.frozen_schema is None or session.validated_ttp_template is None:
            issue = _issue(
                "generation.invalid_session_state",
                "Generation reached an inconsistent terminal state.",
                stage="generation",
            )
            return failure("internal_error", [issue])

        def validate_frozen_schema() -> list[ValidationIssue]:
            try:
                frozen_evidence = [
                    FieldEvidence.model_validate(item)
                    for item in session.field_evidence
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
                session.frozen_schema,
                frozen_evidence,
                tuple(request.command_outputs),
                max_schema_bytes=self.policy.max_schema_bytes,
                max_schema_depth=self.policy.max_schema_depth,
                max_schema_properties=self.policy.max_schema_properties,
            )

        try:
            schema_completed, schema_issues = await _run_before_deadline(
                lambda: asyncio.to_thread(validate_frozen_schema),
                deadline_monotonic=deadline,
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
            return failure("final_validation_failed", [issue])
        if (
            not schema_completed
            or schema_issues is None
            or time.monotonic() >= deadline
        ):
            issue = _issue(
                "generation.timeout",
                "The total generation time budget expired during final "
                "schema acceptance.",
                stage="budget",
            )
            return failure("generation_timeout", [issue])
        if schema_issues:
            return failure("final_validation_failed", schema_issues)

        final_timeout = min(
            self.policy.ttp_validation_timeout_seconds,
            max(0.0, deadline - time.monotonic()),
        )
        try:
            completed, acceptance = await _run_before_deadline(
                lambda: asyncio.to_thread(
                    validate_ttp_template,
                    session.validated_ttp_template,
                    tuple(request.command_outputs),
                    session.frozen_schema,
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
                deadline_monotonic=deadline,
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
            return failure("final_validation_failed", [issue])
        if not completed or acceptance is None or time.monotonic() >= deadline:
            issue = _issue(
                "generation.timeout",
                "The total generation time budget expired during final acceptance.",
                stage="budget",
            )
            return failure("generation_timeout", [issue])

        final_issues = _normalise_issues(
            acceptance.issues,
            fallback_stage="acceptance",
        )
        if not acceptance.valid:
            return failure("final_validation_failed", final_issues)

        def build_artifact() -> ArtifactBundle:
            return ArtifactBundle(
                ttp_template=session.validated_ttp_template,
                result_schema=deepcopy(session.frozen_schema),
                records=deepcopy(acceptance.records),
                assumptions=list(session.assumptions),
            )

        try:
            artifact_completed, artifact = await _run_before_deadline(
                lambda: asyncio.to_thread(build_artifact),
                deadline_monotonic=deadline,
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
            return failure("final_validation_failed", [issue])
        if not artifact_completed or artifact is None or time.monotonic() >= deadline:
            issue = _issue(
                "generation.timeout",
                "The total generation time budget expired while packaging "
                "the artifact.",
                stage="budget",
            )
            return failure("generation_timeout", [issue])
        return GenerationResult(
            status="success",
            artifact=artifact,
            metadata=metadata("success"),
        )


__all__ = ["TtpGenerator"]
