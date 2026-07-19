"""Opt-in end-to-end tests against a real OpenAI-compatible model."""

from __future__ import annotations

import asyncio
import os
import time

import pytest
from pydantic import ValidationError

from cli_parser_agent import (
    GenerationPolicy,
    GenerationRequest,
    TtpGenerator,
    TtpGeneratorSettings,
)
from cli_parser_agent.ttp_generation.agent import (
    GenerationSession,
    SchemaCandidate,
    TemplateCandidate,
    ValidatorOutcome,
    build_agent,
    build_task_message,
    run_generation_agent,
)
from cli_parser_agent.ttp_generation.contracts import FieldEvidence, ValidationIssue
from cli_parser_agent.ttp_generation.generator import _run_before_deadline
from cli_parser_agent.ttp_generation.validation import (
    validate_result_schema,
    validate_schema_proposal,
    validate_ttp_template,
)

COMMAND_OUTPUTS = [
    """\
Interface              Status   Protocol
GigabitEthernet0/0     up       up
GigabitEthernet0/1     down     down
Loopback0              up       up""",
    """\
Interface              Status   Protocol
GigabitEthernet0/0     down     down
GigabitEthernet0/1     up       up
Loopback0              up       up""",
]


def _settings_from_live_environment() -> TtpGeneratorSettings:
    required = ("OPENAI_API_KEY", "OPENAI_MODEL")
    missing = [name for name in required if not os.getenv(name)]
    if missing:
        pytest.skip(
            "live model credentials are incomplete: " + ", ".join(missing),
        )
    return TtpGeneratorSettings.from_env()


@pytest.mark.live
async def test_real_model_generates_a_fully_validated_artifact() -> None:
    """Exercise the public API and independently revalidate its artifact."""

    settings = _settings_from_live_environment()
    policy = GenerationPolicy()
    generator = TtpGenerator(settings=settings, policy=policy)

    result = await generator.generate(
        GenerationRequest(command_outputs=COMMAND_OUTPUTS),
    )

    assert result.status == "success", [
        (issue.code, issue.stage, issue.message) for issue in result.issues
    ]
    assert result.artifact is not None
    assert result.last_attempt is None
    assert len(result.artifact.records) == len(COMMAND_OUTPUTS)
    assert all(
        isinstance(record, dict) and record for record in result.artifact.records
    )
    assert result.metadata.command_output_count == len(COMMAND_OUTPUTS)
    assert result.metadata.schema_submissions >= 1
    assert result.metadata.ttp_submissions >= 1

    assert validate_result_schema(result.artifact.result_schema) == []
    acceptance = validate_ttp_template(
        result.artifact.ttp_template,
        COMMAND_OUTPUTS,
        result.artifact.result_schema,
        timeout_seconds=policy.ttp_validation_timeout_seconds,
        max_result_bytes=policy.max_parse_result_bytes,
    )
    assert acceptance.valid, [
        (issue.code, issue.stage, issue.message) for issue in acceptance.issues
    ]
    assert acceptance.records == result.artifact.records


@pytest.mark.live
async def test_real_model_is_stopped_by_the_agent_round_budget() -> None:
    """A real tool-capable model cannot reach TTP in one allowed round."""

    settings = _settings_from_live_environment()
    policy = GenerationPolicy(
        total_timeout_seconds=120,
        max_agent_rounds=1,
        max_ttp_submissions=1,
        ttp_validation_timeout_seconds=10,
    )
    result = await TtpGenerator(settings=settings, policy=policy).generate(
        GenerationRequest(command_outputs=COMMAND_OUTPUTS[:1]),
    )

    assert result.status == "failed"
    assert result.artifact is None
    assert result.metadata.agent_rounds <= 1
    assert result.metadata.ttp_submissions == 0
    assert any(issue.code == "generation.agent_round_limit" for issue in result.issues)


@pytest.mark.live
async def test_real_model_reacts_to_schema_and_template_rejections() -> None:
    """Force one valid rejection per phase while retaining real model reasoning."""

    settings = _settings_from_live_environment()
    policy = GenerationPolicy()
    deadline = time.monotonic() + policy.total_timeout_seconds
    schema_valid_rejected = False
    first_valid_template: str | None = None

    def validate_schema(candidate: SchemaCandidate) -> ValidatorOutcome:
        nonlocal schema_valid_rejected
        try:
            evidence = [
                FieldEvidence.model_validate(item) for item in candidate.evidence
            ]
        except ValidationError:
            issue = ValidationIssue(
                code="schema.invalid_evidence",
                stage="schema",
                message="Field evidence does not satisfy the contract.",
            )
            return ValidatorOutcome(valid=False, issues=(issue,))
        issues = validate_schema_proposal(
            candidate.result_schema,
            evidence,
            candidate.command_outputs,
        )
        if not issues and not schema_valid_rejected:
            schema_valid_rejected = True
            issue = ValidationIssue(
                code="live.schema_revision_required",
                stage="schema",
                message=(
                    "The schema is valid. Review it once and resubmit it before "
                    "continuing to the template phase."
                ),
            )
            return ValidatorOutcome(valid=False, issues=(issue,))
        return ValidatorOutcome(valid=not issues, issues=tuple(issues))

    async def validate_template(candidate: TemplateCandidate) -> ValidatorOutcome:
        nonlocal first_valid_template
        outcome = await asyncio.to_thread(
            validate_ttp_template,
            candidate.ttp_template,
            candidate.command_outputs,
            candidate.result_schema,
            timeout_seconds=policy.ttp_validation_timeout_seconds,
            max_result_bytes=policy.max_parse_result_bytes,
        )
        if outcome.valid and first_valid_template is None:
            first_valid_template = candidate.ttp_template
            issue = ValidationIssue(
                code="live.template_revision_required",
                stage="ttp",
                message=(
                    "The template is valid, but this protocol test requires a "
                    "textual revision. Add harmless surrounding whitespace and "
                    "resubmit the complete template."
                ),
            )
            return ValidatorOutcome(valid=False, issues=(issue,))
        if outcome.valid and candidate.ttp_template == first_valid_template:
            issue = ValidationIssue(
                code="live.template_unchanged",
                stage="ttp",
                message="The resubmitted template must differ textually.",
            )
            return ValidatorOutcome(valid=False, issues=(issue,))
        return ValidatorOutcome(
            valid=outcome.valid,
            issues=tuple(outcome.issues),
            records=tuple(outcome.records),
        )

    session = GenerationSession(
        command_outputs=tuple(COMMAND_OUTPUTS),
        schema_validator=validate_schema,
        template_validator=validate_template,
        max_ttp_submissions=policy.max_ttp_submissions,
        deadline_monotonic=deadline,
    )
    agent = build_agent(settings=settings, policy=policy, session=session)
    completed, _ = await _run_before_deadline(
        lambda: run_generation_agent(
            agent,
            build_task_message(COMMAND_OUTPUTS),
            session,
        ),
        deadline_monotonic=deadline,
    )

    assert completed
    assert schema_valid_rejected
    assert session.schema_submissions >= 2
    assert session.succeeded, session.last_issues
    assert session.ttp_submissions >= 2
    assert first_valid_template is not None
    assert session.validated_ttp_template != first_valid_template
