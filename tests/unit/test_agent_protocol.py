from __future__ import annotations

import asyncio
import json
from contextlib import contextmanager
from types import SimpleNamespace
from typing import Any, cast

import pytest
from agentscope.agent import Agent
from agentscope.event import ToolResultEndEvent, ToolResultTextDeltaEvent
from agentscope.message import TextBlock, ToolCallBlock, ToolResultState
from agentscope.tool import ToolChunk, Toolkit, ToolResponse

from cli_parser_agent.ttp_generation.agent import (
    PROMPT_VERSION,
    SCHEMA_SYSTEM_PROMPT,
    SUBMIT_SCHEMA_TOOL_NAME,
    SUBMIT_TEMPLATE_TOOL_NAME,
    TTP_SYSTEM_PROMPT,
    GenerationPhase,
    GenerationSession,
    LosslessContextMiddleware,
    SchemaCandidate,
    SubmitResultSchemaTool,
    SubmitTtpTemplateTool,
    TemplateCandidate,
    ValidatorOutcome,
    build_schema_task_prompt,
    build_submission_tools,
    build_ttp_task_prompt,
)
from cli_parser_agent.ttp_generation.agent import tools as tools_module


def _schema(field_name: str = "value") -> dict[str, Any]:
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "type": "object",
        "properties": {field_name: {"type": "string"}},
        "required": [field_name],
        "additionalProperties": False,
    }


def _payload(chunk: ToolChunk) -> dict[str, Any]:
    assert len(chunk.content) == 1
    block = chunk.content[0]
    assert isinstance(block, TextBlock)
    return cast(dict[str, Any], json.loads(block.text))


def _unused_schema_validator(candidate: SchemaCandidate) -> ValidatorOutcome:
    raise AssertionError(f"schema validator should not be called: {candidate!r}")


def _unused_template_validator(candidate: TemplateCandidate) -> ValidatorOutcome:
    raise AssertionError(f"template validator should not be called: {candidate!r}")


def _tool_event_agent(
    session: GenerationSession,
    phase: GenerationPhase,
) -> Agent:
    return Agent(
        name="test_agent",
        system_prompt="test",
        model=cast(Any, object()),
        toolkit=Toolkit(tools=build_submission_tools(session, phase)),
    )


async def _tool_result_events(
    session: GenerationSession,
    tool_call: ToolCallBlock,
) -> list[Any]:
    phase: GenerationPhase = (
        "schema" if tool_call.name == SUBMIT_SCHEMA_TOOL_NAME else "ttp"
    )
    agent = _tool_event_agent(session, phase)
    events: list[Any] = []
    async for item in agent._acting(tool_call):
        if isinstance(item, ToolChunk):
            events.extend(
                [
                    event
                    async for event in agent._convert_tool_chunk_to_event(
                        tool_call.id,
                        item.content,
                    )
                ],
            )
        elif isinstance(item, ToolResponse):
            events.append(
                ToolResultEndEvent(
                    reply_id=agent.state.reply_id,
                    tool_call_id=tool_call.id,
                    state=item.state,
                    metadata=item.metadata,
                ),
            )
    return events


def _event_text(events: list[Any]) -> str:
    return "".join(
        event.delta for event in events if isinstance(event, ToolResultTextDeltaEvent)
    )


def _contains_chinese(text: str) -> bool:
    return any("\u4e00" <= character <= "\u9fff" for character in text)


def test_phase_prompts_are_independent_chinese_protocols() -> None:
    assert PROMPT_VERSION == "ttp-generator-v9-phase-isolated-zh-cn"
    assert _contains_chinese(SCHEMA_SYSTEM_PROMPT)
    assert _contains_chinese(TTP_SYSTEM_PROMPT)
    assert SCHEMA_SYSTEM_PROMPT != TTP_SYSTEM_PROMPT

    assert "submit_result_schema" in SCHEMA_SYSTEM_PROMPT
    assert "固定字段数量" in SCHEMA_SYSTEM_PROMPT
    assert "整条数据行" in SCHEMA_SYSTEM_PROMPT
    assert "中文" in SCHEMA_SYSTEM_PROMPT
    assert "1-3" not in SCHEMA_SYSTEM_PROMPT
    assert "TTP" not in SCHEMA_SYSTEM_PROMPT
    assert "submit_ttp_template" not in SCHEMA_SYSTEM_PROMPT
    assert "<group" not in SCHEMA_SYSTEM_PROMPT
    assert "{{" not in SCHEMA_SYSTEM_PROMPT

    assert "submit_ttp_template" in TTP_SYSTEM_PROMPT
    assert "语义字段" in TTP_SYSTEM_PROMPT
    assert "未建模列" in TTP_SYSTEM_PROMPT
    assert "表格" in TTP_SYSTEM_PROMPT
    assert "`{{ ignore }}`" in TTP_SYSTEM_PROMPT
    assert "`{{ ignore(ORPHRASE) }}`" in TTP_SYSTEM_PROMPT
    assert '`{{ ignore("PID:.*SN:") }}`' in TTP_SYSTEM_PROMPT
    assert "ignore |" not in TTP_SYSTEM_PROMPT
    assert "capture 必须与 issues 一起用于修正" in TTP_SYSTEM_PROMPT
    assert "submit_result_schema" not in TTP_SYSTEM_PROMPT
    assert "evidence" not in TTP_SYSTEM_PROMPT
    assert "assumptions" not in TTP_SYSTEM_PROMPT

    schema_tokens = (
        "JSON Schema",
        "evidence_not_found",
        "required_action",
        "replace_excerpt",
        "change_output_index",
        "matching_output_indexes",
        "/interfaces/*/name",
    )
    for token in schema_tokens:
        assert token in SCHEMA_SYSTEM_PROMPT

    ttp_tokens = (
        "TTP",
        "XML",
        "forbidden_tag",
        "invalid_xml",
        "unsafe_variable_attribute",
        "ttp.no_match",
        "ttp.invalid_ignore_syntax",
        "replace_with_ignore_call",
    )
    for token in ttp_tokens:
        assert token in TTP_SYSTEM_PROMPT


def test_submission_tool_contracts_are_chinese_with_stable_names() -> None:
    assert SubmitResultSchemaTool.name == SUBMIT_SCHEMA_TOOL_NAME
    assert SubmitTtpTemplateTool.name == SUBMIT_TEMPLATE_TOOL_NAME
    assert not hasattr(SubmitResultSchemaTool.call, "__wrapped__")
    assert not hasattr(SubmitTtpTemplateTool.call, "__wrapped__")
    assert _contains_chinese(SubmitResultSchemaTool.description)
    assert _contains_chinese(SubmitTtpTemplateTool.description)

    schema_contract = SubmitResultSchemaTool.input_schema
    schema_protocol = SubmitResultSchemaTool.description + json.dumps(
        schema_contract,
        ensure_ascii=False,
    )
    assert "TTP" not in schema_protocol
    assert "submit_ttp_template" not in schema_protocol
    assert set(schema_contract["properties"]) == {
        "result_schema",
        "evidence",
        "assumptions",
    }
    for property_schema in schema_contract["properties"].values():
        assert _contains_chinese(property_schema["description"])

    evidence_contract = schema_contract["$defs"]["FieldEvidenceInput"]
    assert set(evidence_contract["properties"]) == {
        "path",
        "output_index",
        "excerpt",
    }
    for property_schema in evidence_contract["properties"].values():
        assert _contains_chinese(property_schema["description"])

    assumptions_description = schema_contract["properties"]["assumptions"][
        "description"
    ]
    assert "中文 assumptions" in assumptions_description

    template_contract = SubmitTtpTemplateTool.input_schema
    template_protocol = SubmitTtpTemplateTool.description + json.dumps(
        template_contract,
        ensure_ascii=False,
    )
    assert "submit_result_schema" not in template_protocol
    assert "evidence" not in template_protocol
    assert "assumptions" not in template_protocol
    assert set(template_contract["properties"]) == {"ttp_template"}
    assert _contains_chinese(
        template_contract["properties"]["ttp_template"]["description"],
    )


def test_phase_task_prompts_round_trip_only_their_inputs() -> None:
    outputs = [
        '接口 "Gi0/1"\n状态: <up> & ready',
        "第二份输出\r\n值：雪",
    ]
    schema = _schema()

    schema_prompt = build_schema_task_prompt(outputs)
    ttp_prompt = build_ttp_task_prompt(outputs, schema)

    opening_tag = "<command_outputs_json>"
    closing_tag = "</command_outputs_json>"
    for prompt in (schema_prompt, ttp_prompt):
        serialized = prompt.split(opening_tag, maxsplit=1)[1].split(
            closing_tag,
            maxsplit=1,
        )[0]
        assert json.loads(serialized) == outputs
        assert "接口" in serialized
        assert '\\"Gi0/1\\"' in serialized
        assert "\\n" in serialized
        assert "<up>" in serialized
        assert "& ready" in serialized

    schema_tag = "<frozen_result_schema_json>"
    schema_end_tag = "</frozen_result_schema_json>"
    assert schema_tag not in schema_prompt
    serialized_schema = ttp_prompt.split(schema_tag, maxsplit=1)[1].split(
        schema_end_tag,
        maxsplit=1,
    )[0]
    assert json.loads(serialized_schema) == schema
    assert "evidence" not in ttp_prompt
    assert "assumptions" not in ttp_prompt


@pytest.mark.asyncio
async def test_schema_rejection_can_be_corrected_then_frozen_once() -> None:
    seen: list[SchemaCandidate] = []

    def validate_schema(candidate: SchemaCandidate) -> ValidatorOutcome:
        seen.append(candidate)
        accepted = "value" in candidate.result_schema["properties"]
        issues: tuple[dict[str, str], ...] = ()
        if not accepted:
            issues = (
                {
                    "code": "schema.missing_value",
                    "stage": "schema",
                    "message": "value is required",
                },
            )
        return ValidatorOutcome(valid=accepted, issues=issues)

    session = GenerationSession(
        command_outputs=["value: one", "value: two"],
        schema_validator=validate_schema,
        template_validator=_unused_template_validator,
    )
    tool = SubmitResultSchemaTool(session)

    rejected = await tool.call(
        result_schema=_schema("wrong"),
        evidence=[{"path": "/wrong", "output_index": 0, "excerpt": "one"}],
    )
    assert _payload(rejected)["accepted"] is False
    assert session.frozen_schema is None
    assert session.schema_submissions == 1
    accepted_schema = _schema()
    accepted = await tool.call(
        result_schema=accepted_schema,
        evidence=[{"path": "/value", "output_index": 1, "excerpt": "two"}],
        assumptions=["这些值按标签处理。"],
    )
    assert _payload(accepted)["accepted"] is True
    assert _payload(accepted)["next_action"] == "finish_schema"
    assert session.schema_submissions == 2
    assert session.frozen_schema == _schema()
    assert session.field_evidence == (
        {"path": "/value", "output_index": 1, "excerpt": "two"},
    )
    assert session.assumptions == ("这些值按标签处理。",)
    assert seen[-1].command_outputs == ("value: one", "value: two")
    accepted_schema["properties"]["value"]["type"] = "integer"
    replacement = await tool.call(
        result_schema=_schema("replacement"),
        evidence=[
            {"path": "/replacement", "output_index": 0, "excerpt": "one"},
        ],
    )
    replacement_payload = _payload(replacement)
    assert replacement_payload["accepted"] is False
    assert replacement_payload["issues"][0]["code"] == "schema_already_frozen"
    assert session.schema_submissions == 2
    assert len(seen) == 2
    assert session.frozen_schema == _schema()


@pytest.mark.asyncio
async def test_schema_tool_span_records_the_full_submission_and_result(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    starts: list[dict[str, Any]] = []
    finishes: list[dict[str, Any]] = []

    @contextmanager
    def start(name: str, **kwargs: Any) -> Any:
        starts.append({"name": name, **kwargs})
        yield object()

    monkeypatch.setattr(tools_module, "start_laminar_span", start)
    monkeypatch.setattr(
        tools_module,
        "finish_laminar_span",
        lambda **kwargs: finishes.append(kwargs),
    )

    session = GenerationSession(
        command_outputs=["value: one"],
        schema_validator=lambda candidate: ValidatorOutcome(valid=True),
        template_validator=_unused_template_validator,
    )
    schema = _schema()
    evidence = [{"path": "/value", "output_index": 0, "excerpt": "one"}]

    result = await SubmitResultSchemaTool(session).call(
        result_schema=schema,
        evidence=evidence,
        assumptions=["按字符串处理。"],
    )
    payload = _payload(result)

    assert starts == [
        {
            "name": SUBMIT_SCHEMA_TOOL_NAME,
            "input": {
                "result_schema": schema,
                "evidence": evidence,
                "assumptions": ["按字符串处理。"],
            },
            "span_type": "TOOL",
        },
    ]
    assert finishes == [
        {
            "output": payload,
            "outcome": "success",
            "attributes": {
                "phase": "schema",
                "accepted": True,
                "schema_submission": 1,
            },
        },
    ]


@pytest.mark.asyncio
async def test_invalid_schema_input_is_redacted_from_tool_result_events() -> None:
    secret = "schema-evidence-secret-7b459b"
    session = GenerationSession(
        command_outputs=["value: one"],
        schema_validator=_unused_schema_validator,
        template_validator=_unused_template_validator,
    )
    tool_call = ToolCallBlock(
        id="schema-call",
        name=SUBMIT_SCHEMA_TOOL_NAME,
        input=json.dumps(
            {
                "result_schema": _schema(),
                "evidence": [
                    {
                        "path": "/value",
                        "output_index": 0,
                        "excerpt": {"untrusted": secret},
                    },
                ],
            },
        ),
    )

    events = await _tool_result_events(session, tool_call)

    text = _event_text(events)
    payload = cast(dict[str, Any], json.loads(text))
    assert payload["accepted"] is False
    assert payload["issues"][0]["code"] == "schema.submission_invalid"
    assert secret not in text
    assert "input_value" not in text
    assert session.schema_submissions == 0
    assert session.last_issues == tuple(payload["issues"])


@pytest.mark.asyncio
async def test_schema_validator_exception_is_redacted_from_agent_events() -> None:
    secret = "schema-validator-secret-c52679"

    def fail_with_candidate(candidate: SchemaCandidate) -> ValidatorOutcome:
        raise RuntimeError(f"{secret}: {candidate.evidence!r}")

    session = GenerationSession(
        command_outputs=["value: one"],
        schema_validator=fail_with_candidate,
        template_validator=_unused_template_validator,
    )
    tool_call = ToolCallBlock(
        id="schema-call",
        name=SUBMIT_SCHEMA_TOOL_NAME,
        input=json.dumps(
            {
                "result_schema": _schema(),
                "evidence": [
                    {
                        "path": "/value",
                        "output_index": 0,
                        "excerpt": secret,
                    },
                ],
            },
        ),
    )

    events = await _tool_result_events(session, tool_call)

    text = _event_text(events)
    payload = cast(dict[str, Any], json.loads(text))
    assert payload["accepted"] is False
    assert payload["issues"][0]["code"] == "schema.validator_failed"
    assert secret not in text
    assert any(
        isinstance(event, ToolResultEndEvent) and event.state == ToolResultState.SUCCESS
        for event in events
    )
    assert session.schema_submissions == 1
    assert session.frozen_schema is None


@pytest.mark.asyncio
async def test_schema_validator_cancellation_propagates() -> None:
    async def cancel_validation(
        candidate: SchemaCandidate,
    ) -> ValidatorOutcome:
        raise asyncio.CancelledError

    session = GenerationSession(
        command_outputs=["value: one"],
        schema_validator=cancel_validation,
        template_validator=_unused_template_validator,
    )

    with pytest.raises(asyncio.CancelledError):
        await SubmitResultSchemaTool(session).call(
            result_schema=_schema(),
            evidence=[
                {"path": "/value", "output_index": 0, "excerpt": "one"},
            ],
        )

    assert session.schema_submissions == 1
    assert session.frozen_schema is None


@pytest.mark.asyncio
async def test_template_submission_requires_a_frozen_schema() -> None:
    session = GenerationSession(
        command_outputs=["value: one"],
        schema_validator=_unused_schema_validator,
        template_validator=_unused_template_validator,
    )

    result = await SubmitTtpTemplateTool(session).call("{{ value }}")

    payload = _payload(result)
    assert payload["accepted"] is False
    assert payload["issues"][0]["code"] == "schema_not_frozen"
    assert session.ttp_submissions == 0
    assert session.last_ttp_template is None


@pytest.mark.asyncio
async def test_invalid_template_input_is_redacted_from_tool_result_events() -> None:
    secret = "template-input-secret-e76840"
    session = GenerationSession(
        command_outputs=["value: one"],
        schema_validator=_unused_schema_validator,
        template_validator=_unused_template_validator,
    )
    session.frozen_schema = _schema()
    tool_call = ToolCallBlock(
        id="template-call",
        name=SUBMIT_TEMPLATE_TOOL_NAME,
        input=json.dumps({"ttp_template": {"untrusted": secret}}),
    )

    events = await _tool_result_events(session, tool_call)

    text = _event_text(events)
    payload = cast(dict[str, Any], json.loads(text))
    assert payload["accepted"] is False
    assert payload["issues"][0]["code"] == "ttp.submission_invalid"
    assert secret not in text
    assert "input_value" not in text
    assert session.ttp_submissions == 0
    assert session.last_ttp_template is None
    assert session.last_issues == tuple(payload["issues"])


@pytest.mark.asyncio
async def test_template_validator_exception_is_redacted_from_agent_events() -> None:
    secret = "template-validator-secret-8d0c31"

    async def fail_with_candidate(
        candidate: TemplateCandidate,
    ) -> ValidatorOutcome:
        raise ValueError(f"{secret}: {candidate.ttp_template}")

    session = GenerationSession(
        command_outputs=["value: one"],
        schema_validator=_unused_schema_validator,
        template_validator=fail_with_candidate,
    )
    session.frozen_schema = _schema()
    tool_call = ToolCallBlock(
        id="template-call",
        name=SUBMIT_TEMPLATE_TOOL_NAME,
        input=json.dumps({"ttp_template": f"value: {{{{ {secret} }}}}"}),
    )

    events = await _tool_result_events(session, tool_call)

    text = _event_text(events)
    payload = cast(dict[str, Any], json.loads(text))
    assert payload["accepted"] is False
    assert payload["issues"][0]["code"] == "ttp.validator_failed"
    assert secret not in text
    assert any(
        isinstance(event, ToolResultEndEvent) and event.state == ToolResultState.SUCCESS
        for event in events
    )
    assert session.ttp_submissions == 1
    assert session.validated_ttp_template is None
    assert session.first_ttp_valid is False


@pytest.mark.asyncio
async def test_rejected_template_returns_index_mapped_capture_without_storing_it() -> (
    None
):
    captured_records = [
        {},
        {"items": [{"name": "second"}]},
    ]
    issues = (
        {
            "code": "schema.record_mismatch",
            "stage": "schema",
            "message": "record does not match",
            "output_index": 1,
        },
    )

    def reject(candidate: TemplateCandidate) -> ValidatorOutcome:
        return ValidatorOutcome(
            valid=False,
            issues=issues,
            records=tuple(captured_records),
        )

    session = GenerationSession(
        command_outputs=["first", "second"],
        schema_validator=_unused_schema_validator,
        template_validator=reject,
    )
    session.frozen_schema = _schema()

    result = await SubmitTtpTemplateTool(session).call("{{ value }}")

    payload = _payload(result)
    assert payload["capture"] == {
        "available": True,
        "complete": True,
        "serialized_bytes": len(
            json.dumps(
                captured_records,
                ensure_ascii=True,
                separators=(",", ":"),
            ).encode("utf-8"),
        ),
        "records": captured_records,
        "previews": [],
    }
    assert session.records == ()
    assert session.validated_ttp_template is None
    assert session.last_issues == issues
    assert "capture" not in json.dumps(payload["issues"])


@pytest.mark.asyncio
async def test_template_tool_span_records_the_same_bounded_capture(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    starts: list[dict[str, Any]] = []
    finishes: list[dict[str, Any]] = []

    @contextmanager
    def start(name: str, **kwargs: Any) -> Any:
        starts.append({"name": name, **kwargs})
        yield object()

    monkeypatch.setattr(tools_module, "start_laminar_span", start)
    monkeypatch.setattr(
        tools_module,
        "finish_laminar_span",
        lambda **kwargs: finishes.append(kwargs),
    )

    def reject(candidate: TemplateCandidate) -> ValidatorOutcome:
        return ValidatorOutcome(
            valid=False,
            issues=(
                {
                    "code": "ttp.no_match",
                    "stage": "ttp",
                    "message": "no match",
                    "output_index": 0,
                },
            ),
            records=({},),
        )

    session = GenerationSession(
        command_outputs=["unmatched"],
        schema_validator=_unused_schema_validator,
        template_validator=reject,
    )
    session.frozen_schema = _schema()

    result = await SubmitTtpTemplateTool(session).call("Value: {{ value }}")
    payload = _payload(result)

    assert starts == [
        {
            "name": SUBMIT_TEMPLATE_TOOL_NAME,
            "input": {"ttp_template": "Value: {{ value }}"},
            "span_type": "TOOL",
        },
    ]
    assert finishes == [
        {
            "output": payload,
            "outcome": "success",
            "attributes": {
                "phase": "template",
                "accepted": False,
                "ttp_submission": 1,
            },
        },
    ]
    assert finishes[0]["output"]["capture"]["records"] == [{}]


@pytest.mark.asyncio
async def test_template_validator_cancellation_propagates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    error = asyncio.CancelledError("private cancellation text")
    events: list[str] = []
    finishes: list[dict[str, Any]] = []

    @contextmanager
    def start(*_: Any, **__: Any) -> Any:
        events.append("entered")
        try:
            yield object()
        finally:
            events.append("exited")

    monkeypatch.setattr(tools_module, "start_laminar_span", start)

    def finish(**kwargs: Any) -> None:
        events.append("finished")
        finishes.append(kwargs)

    monkeypatch.setattr(tools_module, "finish_laminar_span", finish)

    async def cancel_validation(
        candidate: TemplateCandidate,
    ) -> ValidatorOutcome:
        raise error

    session = GenerationSession(
        command_outputs=["value: one"],
        schema_validator=_unused_schema_validator,
        template_validator=cancel_validation,
    )
    session.frozen_schema = _schema()

    with pytest.raises(asyncio.CancelledError) as caught:
        await SubmitTtpTemplateTool(session).call("value: {{ value }}")

    assert caught.value is error
    assert events == ["entered", "finished", "exited"]
    assert finishes == [
        {
            "output": {
                "status": "cancelled",
                "exception_type": "CancelledError",
            },
            "outcome": "cancelled",
            "attributes": {"exception_type": "CancelledError"},
        },
    ]
    assert "private" not in str(finishes)
    assert session.ttp_submissions == 1
    assert session.validated_ttp_template is None


@pytest.mark.asyncio
async def test_async_template_validator_preserves_record_index_mapping() -> None:
    returned_records = [
        {"value": "one", "nested": {"index": 0}},
        {"value": "two", "nested": {"index": 1}},
    ]
    seen: list[TemplateCandidate] = []

    async def validate_template(candidate: TemplateCandidate) -> dict[str, Any]:
        seen.append(candidate)
        return {"valid": True, "records": returned_records}

    session = GenerationSession(
        command_outputs=["value: one", "value: two"],
        schema_validator=_unused_schema_validator,
        template_validator=validate_template,
    )
    session.frozen_schema = _schema()

    result = await SubmitTtpTemplateTool(session).call("value: {{ value }}")

    assert _payload(result)["accepted"] is True
    assert seen == [
        TemplateCandidate(
            ttp_template="value: {{ value }}",
            result_schema=_schema(),
            command_outputs=("value: one", "value: two"),
        ),
    ]
    assert session.records == (
        {"value": "one", "nested": {"index": 0}},
        {"value": "two", "nested": {"index": 1}},
    )
    assert session.validated_ttp_template == "value: {{ value }}"
    assert session.first_ttp_valid is True
    assert session.terminal_reason == "success"
    returned_records[0]["nested"]["index"] = 99
    assert session.records[0]["nested"]["index"] == 0


@pytest.mark.asyncio
async def test_attribute_validator_outcome_remains_supported() -> None:
    session = GenerationSession(
        command_outputs=["value: one"],
        schema_validator=_unused_schema_validator,
        template_validator=lambda candidate: SimpleNamespace(
            valid=True,
            records=({"value": "one"},),
        ),
    )
    session.frozen_schema = _schema()

    result = await SubmitTtpTemplateTool(session).call("value: {{ value }}")

    assert _payload(result)["accepted"] is True
    assert session.records == ({"value": "one"},)


@pytest.mark.parametrize(
    ("records", "issue_code"),
    [
        ([{"value": "one"}], "record_count_mismatch"),
        ([{"value": "one"}, ["not", "an", "object"]], "record_root_not_object"),
    ],
)
@pytest.mark.asyncio
async def test_valid_validator_outcome_still_requires_one_object_per_input(
    records: list[Any],
    issue_code: str,
) -> None:
    def validate_template(candidate: TemplateCandidate) -> dict[str, Any]:
        return {"valid": True, "records": records}

    session = GenerationSession(
        command_outputs=["value: one", "value: two"],
        schema_validator=_unused_schema_validator,
        template_validator=validate_template,
    )
    session.frozen_schema = _schema()

    result = await SubmitTtpTemplateTool(session).call("value: {{ value }}")

    payload = _payload(result)
    assert payload["accepted"] is False
    assert payload["issues"][-1]["code"] == issue_code
    assert session.validated_ttp_template is None
    assert session.records == ()
    assert session.first_ttp_valid is False


@pytest.mark.asyncio
async def test_template_submission_budget_blocks_validator_after_limit() -> None:
    seen_templates: list[str] = []

    def reject_template(candidate: TemplateCandidate) -> ValidatorOutcome:
        seen_templates.append(candidate.ttp_template)
        return ValidatorOutcome(
            valid=False,
            issues=(
                {
                    "code": "template.parse_failed",
                    "stage": "template",
                    "message": "did not parse",
                },
            ),
        )

    session = GenerationSession(
        command_outputs=["value: one"],
        schema_validator=_unused_schema_validator,
        template_validator=reject_template,
        max_ttp_submissions=2,
    )
    session.frozen_schema = _schema()
    tool = SubmitTtpTemplateTool(session)

    first = await tool.call("first: {{ value }}")
    second = await tool.call("second: {{ value }}")
    blocked = await tool.call("third: {{ value }}")

    assert _payload(first)["remaining_submissions"] == 1
    assert _payload(second)["remaining_submissions"] == 0
    blocked_payload = _payload(blocked)
    assert blocked_payload["accepted"] is False
    assert blocked_payload["issues"][0]["code"] == "ttp_submission_limit"
    assert seen_templates == ["first: {{ value }}", "second: {{ value }}"]
    assert session.ttp_submissions == 2
    assert session.last_ttp_template == "second: {{ value }}"
    assert session.terminal_reason == "ttp_submission_limit"


@pytest.mark.asyncio
async def test_unchanged_template_is_rejected_without_revalidating() -> None:
    seen_templates: list[str] = []

    def reject_template(candidate: TemplateCandidate) -> ValidatorOutcome:
        seen_templates.append(candidate.ttp_template)
        return ValidatorOutcome(valid=False)

    session = GenerationSession(
        command_outputs=["value: one"],
        schema_validator=_unused_schema_validator,
        template_validator=reject_template,
        max_ttp_submissions=2,
    )
    session.frozen_schema = _schema()
    tool = SubmitTtpTemplateTool(session)

    await tool.call("value: {{ value }}")
    repeated = await tool.call("value: {{ value }}")

    payload = _payload(repeated)
    assert payload["accepted"] is False
    assert payload["issues"] == [
        {
            "code": "ttp.unchanged_submission",
            "stage": "template",
            "message": (
                "The template is identical to the previous rejected submission "
                "and must be changed before resubmission."
            ),
            "details": {"required_action": "modify_template"},
        },
    ]
    assert payload["remaining_submissions"] == 0
    assert seen_templates == ["value: {{ value }}"]
    assert session.ttp_submissions == 2
    assert session.last_issues == tuple(payload["issues"])


@pytest.mark.parametrize(
    ("phase", "tool_type"),
    [
        ("schema", SubmitResultSchemaTool),
        ("ttp", SubmitTtpTemplateTool),
    ],
)
def test_phase_toolkit_builder_returns_exactly_one_tool(
    phase: GenerationPhase,
    tool_type: type[Any],
) -> None:
    session = GenerationSession(
        command_outputs=["value: one"],
        schema_validator=_unused_schema_validator,
        template_validator=_unused_template_validator,
    )

    tools = build_submission_tools(session, phase)

    assert len(tools) == 1
    assert isinstance(tools[0], tool_type)


@pytest.mark.asyncio
async def test_lossless_middleware_never_compresses_source_context() -> None:
    called = False

    async def next_handler(**kwargs: Any) -> None:
        nonlocal called
        called = True

    await LosslessContextMiddleware().on_compress_context(
        object(),
        {},
        next_handler,
    )

    assert called is False
