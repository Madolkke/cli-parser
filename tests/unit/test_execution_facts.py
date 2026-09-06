from __future__ import annotations

import asyncio
import json

import pytest
from test_generator import _closed_schema, _settings, _template_only_workflow

from cli_parser_agent import GenerationRequest, TemplateRequest, TtpGenerator
from cli_parser_agent.ttp_generation.agent.tools import build_submission_tools
from cli_parser_agent.ttp_generation.workflow import _ExecutionFacts


async def test_facts_follow_valid_candidate_retention_and_final_acceptance() -> None:
    workflow = _template_only_workflow(_closed_schema())
    workflow._freeze_injected_schema(workflow.injected_schema)
    tools = {
        tool.name: tool for tool in build_submission_tools(workflow.session, "ttp")
    }
    await tools["submit_ttp_template"].call(ttp_template="value: {{ value }}")
    await tools["submit_ttp_template"].call(ttp_template="<macro>unsafe</macro>")
    # ToolCallStart is observed by the runner, before the tool implementation.
    workflow.session.finish_called = True
    await tools["finish_generation"].call()
    result = await workflow._accept_artifact()
    workflow.execution_facts.capture(workflow.session)
    facts = workflow.execution_facts.as_dict()
    assert result.status == "success"
    assert facts["valid_ttp_candidate"]
    assert facts["finish_called"] and facts["finish_succeeded"]
    assert facts["final_acceptance_started"] and facts["final_acceptance_passed"]


async def test_finish_without_candidate_is_observed_but_rejected() -> None:
    workflow = _template_only_workflow(_closed_schema())
    workflow._freeze_injected_schema(workflow.injected_schema)
    tools = {
        tool.name: tool for tool in build_submission_tools(workflow.session, "ttp")
    }
    workflow.session.finish_called = True
    await tools["finish_generation"].call()
    workflow.execution_facts.capture(workflow.session)
    assert workflow.execution_facts.finish_called
    assert not workflow.execution_facts.finish_succeeded
    assert not workflow.execution_facts.valid_ttp_candidate
    assert not workflow.execution_facts.final_acceptance_started


async def test_finish_success_is_distinct_from_failed_final_acceptance() -> None:
    workflow = _template_only_workflow(_closed_schema())
    workflow._freeze_injected_schema(workflow.injected_schema)
    tools = {
        tool.name: tool for tool in build_submission_tools(workflow.session, "ttp")
    }
    await tools["submit_ttp_template"].call(ttp_template="value: {{ value }}")
    workflow.session.finish_called = True
    await tools["finish_generation"].call()
    # Corruption after the tool gate must be caught by independent acceptance.
    workflow.session.frozen_schema["additionalProperties"] = True
    result = await workflow._accept_artifact()
    workflow.execution_facts.capture(workflow.session)
    assert result.status == "failed"
    assert workflow.execution_facts.finish_succeeded
    assert workflow.execution_facts.final_acceptance_started
    assert not workflow.execution_facts.final_acceptance_passed
    assert not set(workflow.execution_facts.as_dict()) & set(
        result.metadata.model_dump()
    )


async def test_invalid_injection_reports_facts_without_starting_ttp() -> None:
    events = []
    result = await TtpGenerator(settings=_settings()).generate_from_schema(
        TemplateRequest(
            command_outputs=["value: one"], result_schema={"type": "object"}
        ),
        observer=events.append,
    )
    facts = next(
        event
        for event in events
        if getattr(event, "name", "") == "cli_parser.generation.execution_facts"
    )
    assert result.status == "failed"
    assert facts.value == _ExecutionFacts().as_dict()
    assert facts.metadata["sensitive"] is False


@pytest.mark.parametrize("error_type", [RuntimeError, asyncio.CancelledError])
async def test_exception_and_cancel_capture_request_local_facts(
    monkeypatch: pytest.MonkeyPatch,
    error_type: type[BaseException],
) -> None:
    from cli_parser_agent.ttp_generation.workflow import _GenerationWorkflow

    async def interrupted(self: _GenerationWorkflow):
        self.session.frozen_schema = _closed_schema()
        raise error_type("private exception body")

    monkeypatch.setattr(_GenerationWorkflow, "run", interrupted)
    events = []
    generator = TtpGenerator(settings=_settings())
    with pytest.raises(error_type):
        await generator.generate(
            GenerationRequest(command_outputs=["value: one"]), observer=events.append
        )
    facts = events[-1]
    assert facts.name == "cli_parser.generation.execution_facts"
    assert facts.value["schema_frozen"]
    assert not facts.value["finish_called"]
    assert "private exception body" not in json.dumps(facts.value)


async def test_schema_only_success_has_no_ttp_finish(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from cli_parser_agent.ttp_generation.workflow import _GenerationWorkflow

    async def freeze(self: _GenerationWorkflow):
        return self._freeze_injected_schema(_closed_schema())

    monkeypatch.setattr(_GenerationWorkflow, "_establish_frozen_schema", freeze)
    events = []
    result = await TtpGenerator(settings=_settings()).propose_schema(
        GenerationRequest(command_outputs=["value: one"]),
        observer=events.append,
    )
    assert result.status == "success"
    facts = events[-2].value
    assert facts["schema_frozen"]
    assert not facts["entered_ttp"]
    assert not facts["finish_called"]
    assert not facts["finish_succeeded"]
