from __future__ import annotations

from typing import Any, cast

import pytest

from cli_parser_agent.ttp_generation.agent.session import (
    GenerationPhase,
    GenerationSession,
    SchemaCandidate,
    TemplateCandidate,
    ValidatorOutcome,
)


def _unused_schema_validator(candidate: SchemaCandidate) -> ValidatorOutcome:
    raise AssertionError(f"schema validator should not be called: {candidate!r}")


def _unused_template_validator(candidate: TemplateCandidate) -> ValidatorOutcome:
    raise AssertionError(f"template validator should not be called: {candidate!r}")


def _session() -> GenerationSession:
    return GenerationSession(
        command_outputs=["value: one"],
        schema_validator=_unused_schema_validator,
        template_validator=_unused_template_validator,
        max_schema_no_tool_retries=1,
        max_ttp_no_tool_retries=2,
    )


def test_session_records_rounds_by_isolated_phase() -> None:
    session = _session()

    session.record_agent_round("schema")
    session.record_agent_round("schema")
    session.record_agent_round("ttp")

    assert session.schema_agent_rounds == 2
    assert session.ttp_agent_rounds == 1
    assert session.agent_rounds == 3
    assert session.agent_rounds == (
        session.schema_agent_rounds + session.ttp_agent_rounds
    )


@pytest.mark.parametrize("phase", ["schema", "ttp"])
def test_no_tool_accounting_uses_generation_phase(phase: GenerationPhase) -> None:
    session = _session()

    assert session.record_no_tool_response(phase)
    session.record_no_tool_retry(phase)
    session.reset_no_tool_sequence(phase)
    assert session.record_no_tool_response(phase)

    if phase == "schema":
        assert session.schema_no_tool_responses == 2
        assert session.schema_no_tool_retries == 1
        assert session.ttp_no_tool_responses == 0
    else:
        assert session.ttp_no_tool_responses == 2
        assert session.ttp_no_tool_retries == 1
        assert session.schema_no_tool_responses == 0


@pytest.mark.parametrize(
    "operation",
    [
        lambda session, phase: session.record_agent_round(phase),
        lambda session, phase: session.record_no_tool_response(phase),
        lambda session, phase: session.record_no_tool_retry(phase),
        lambda session, phase: session.reset_no_tool_sequence(phase),
    ],
)
def test_session_rejects_unknown_phase(operation: Any) -> None:
    session = _session()
    invalid_phase = cast(GenerationPhase, "unknown")

    with pytest.raises(ValueError, match="unsupported generation phase"):
        operation(session, invalid_phase)
