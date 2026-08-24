from __future__ import annotations

import asyncio
import json
import re
from contextlib import contextmanager
from types import SimpleNamespace
from typing import Any, cast

import pytest
from agentscope.agent import Agent
from agentscope.event import CustomEvent, ToolResultEndEvent, ToolResultTextDeltaEvent
from agentscope.message import TextBlock, ToolCallBlock, ToolResultState
from agentscope.tool import ToolChunk, Toolkit, ToolResponse

from cli_parser_agent.ttp_generation.agent import (
    FINISH_GENERATION_TOOL_NAME,
    PROMPT_VERSION,
    SCHEMA_SYSTEM_PROMPT,
    SUBMIT_SCHEMA_TOOL_NAME,
    SUBMIT_TEMPLATE_TOOL_NAME,
    TTP_SYSTEM_PROMPT,
    FinishGenerationTool,
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
from cli_parser_agent.ttp_generation.progress import ProgressEmitter


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


def _tool_text(chunk: ToolChunk) -> str:
    assert len(chunk.content) == 1
    block = chunk.content[0]
    assert isinstance(block, TextBlock)
    return block.text


def _matched_records(chunk: ToolChunk) -> list[Any]:
    matches = list(
        re.finditer(
            r'<parsed_record input_index="(?P<input_index>\d+)" '
            r'display_number="(?P<display_number>\d+)">\n'
            r'(?P<record>.*?)\n</parsed_record>',
            _tool_text(chunk),
            flags=re.DOTALL,
        ),
    )
    assert matches, _tool_text(chunk)
    assert [int(item["input_index"]) for item in matches] == list(
        range(len(matches)),
    )
    assert [int(item["display_number"]) for item in matches] == list(
        range(1, len(matches) + 1),
    )
    return [json.loads(item["record"]) for item in matches]


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
    assert PROMPT_VERSION == "ttp-generator-v22-schema-without-evidence-zh-cn"
    assert _contains_chinese(SCHEMA_SYSTEM_PROMPT)
    assert _contains_chinese(TTP_SYSTEM_PROMPT)
    assert SCHEMA_SYSTEM_PROMPT != TTP_SYSTEM_PROMPT

    assert "submit_result_schema" in SCHEMA_SYSTEM_PROMPT
    assert "固定字段数量" in SCHEMA_SYSTEM_PROMPT
    assert "整条数据行" in SCHEMA_SYSTEM_PROMPT
    assert "只在部分实例出现的字段应保持可选" in SCHEMA_SYSTEM_PROMPT
    assert "省略该键" in SCHEMA_SYSTEM_PROMPT
    assert "允许忠实使用空 string" in SCHEMA_SYSTEM_PROMPT
    assert "中文" in SCHEMA_SYSTEM_PROMPT
    assert "1-3" not in SCHEMA_SYSTEM_PROMPT
    assert "TTP" not in SCHEMA_SYSTEM_PROMPT
    assert "submit_ttp_template" not in SCHEMA_SYSTEM_PROMPT
    assert "<group" not in SCHEMA_SYSTEM_PROMPT
    assert "{{" not in SCHEMA_SYSTEM_PROMPT

    assert "submit_ttp_template" in TTP_SYSTEM_PROMPT
    assert "finish_generation" in TTP_SYSTEM_PROMPT
    assert "空对象或关键数组为空" in TTP_SYSTEM_PROMPT
    assert "跨样例" in TTP_SYSTEM_PROMPT
    assert "每次模型回复最多调用一个工具" in TTP_SYSTEM_PROMPT
    assert "ToolResult 已进入" in TTP_SYSTEM_PROMPT
    assert "语义字段" in TTP_SYSTEM_PROMPT
    assert "未建模列" in TTP_SYSTEM_PROMPT
    assert "表格" in TTP_SYSTEM_PROMPT
    assert "`{{ ignore }}`" in TTP_SYSTEM_PROMPT
    assert "`{{ ignore(ORPHRASE) }}`" in TTP_SYSTEM_PROMPT
    assert '`{{ ignore("PID:.*SN:") }}`' in TTP_SYSTEM_PROMPT
    assert '{{ interface | WORD | exclude("Interface") }}' in TTP_SYSTEM_PROMPT
    assert '{{ ok | WORD | equal("YES") }}' in TTP_SYSTEM_PROMPT
    assert "不要增加全是 `ignore` 的表头控制行" in TTP_SYSTEM_PROMPT
    assert "WORD 是 `\\S+`" in TTP_SYSTEM_PROMPT
    assert "只要某个合法值可能只有一个 token 就禁止使用 PHRASE" in TTP_SYSTEM_PROMPT
    assert "ORPHRASE 才能匹配一个 token 或多个 token" in TTP_SYSTEM_PROMPT
    assert "首先检查是否把单 token" in TTP_SYSTEM_PROMPT
    assert "完成这项检查前不要改 XML wrapper" in TTP_SYSTEM_PROMPT
    assert "ignore |" not in TTP_SYSTEM_PROMPT
    assert "直接给出当前模板" in TTP_SYSTEM_PROMPT
    assert "独立的 `<parsed_record>` 块" in TTP_SYSTEM_PROMPT
    assert "不要把不同块拼成一个业务数组" in TTP_SYSTEM_PROMPT
    assert "input_index" in TTP_SYSTEM_PROMPT
    assert "accepted、issues" in TTP_SYSTEM_PROMPT
    assert "存在结果块不代表候选已通过内部验收" in TTP_SYSTEM_PROMPT
    assert "预期数据行数完全相等" in TTP_SYSTEM_PROMPT
    assert "不能把末列 Type 当作中间 Status" in TTP_SYSTEM_PROMPT
    assert "不要使用 condition" in TTP_SYSTEM_PROMPT
    assert "不要用 `.*`、`\\S.*`" in TTP_SYSTEM_PROMPT
    assert "省略未匹配的可选键" in TTP_SYSTEM_PROMPT
    assert "忠实捕获为空 string" in TTP_SYSTEM_PROMPT
    assert "ORPHRASE 都至少匹配一个非空白字符" in TTP_SYSTEM_PROMPT
    assert "绝不能用来捕获空 string" in TTP_SYSTEM_PROMPT
    assert 'pid | re("(?:[^ \\t,](?:[^,]*[^ \\t,])?)?")' in TTP_SYSTEM_PROMPT
    assert "不会替你消费可变空白" in TTP_SYSTEM_PROMPT
    assert "说明行控制拆开了同一实体" in TTP_SYSTEM_PROMPT
    assert "最外层 group 必须省略 name" in TTP_SYSTEM_PROMPT
    assert "未命名的最外层 group 对应根 object 本身" in TTP_SYSTEM_PROMPT
    assert '{{ ignore("\\s*") }}' in TTP_SYSTEM_PROMPT
    assert "吸收可变前导空白" in TTP_SYSTEM_PROMPT
    # Every reply must call exactly one tool; plain text is discarded and only
    # burns budget (0.67 mean no-tool TTP responses observed per trial).
    assert "必须恰好调用这两个工具之一" in TTP_SYSTEM_PROMPT
    assert "会被整条丢弃" in TTP_SYSTEM_PROMPT
    # Superseded results are collapsed in context, so the model must not read
    # the placeholder as a parse failure or resubmit the same template.
    assert "只有最近一次提交的独立解析结果块会完整保留" in TTP_SYSTEM_PROMPT
    assert "不表示那次" in TTP_SYSTEM_PROMPT
    # required is the weakest measured schema dimension; force enumeration.
    assert "required 的判定必须逐实例枚举" in SCHEMA_SYSTEM_PROMPT
    assert "只有每行都有的列才是" in SCHEMA_SYSTEM_PROMPT
    assert "submit_result_schema" not in TTP_SYSTEM_PROMPT
    assert "evidence" not in SCHEMA_SYSTEM_PROMPT
    assert "evidence" not in TTP_SYSTEM_PROMPT
    assert "assumptions" not in TTP_SYSTEM_PROMPT

    schema_tokens = ("JSON Schema", "required 的判定必须逐实例枚举")
    for token in schema_tokens:
        assert token in SCHEMA_SYSTEM_PROMPT

    ttp_tokens = (
        "TTP",
        "XML",
        "forbidden_tag",
        "invalid_xml",
        "unsafe_variable_attribute",
        "ttp.invalid_ignore_syntax",
        "replace_with_ignore_call",
    )
    for token in ttp_tokens:
        assert token in TTP_SYSTEM_PROMPT
    assert "ttp.no_match" not in TTP_SYSTEM_PROMPT
    assert "ttp.materialized_missing_value" not in TTP_SYSTEM_PROMPT


def test_submission_tool_contracts_are_chinese_with_stable_names() -> None:
    assert SubmitResultSchemaTool.name == SUBMIT_SCHEMA_TOOL_NAME
    assert SubmitTtpTemplateTool.name == SUBMIT_TEMPLATE_TOOL_NAME
    assert FinishGenerationTool.name == FINISH_GENERATION_TOOL_NAME
    assert not hasattr(SubmitResultSchemaTool.call, "__wrapped__")
    assert not hasattr(SubmitTtpTemplateTool.call, "__wrapped__")
    assert _contains_chinese(SubmitResultSchemaTool.description)
    assert _contains_chinese(SubmitTtpTemplateTool.description)
    assert _contains_chinese(FinishGenerationTool.description)

    schema_contract = SubmitResultSchemaTool.input_schema
    schema_protocol = SubmitResultSchemaTool.description + json.dumps(
        schema_contract,
        ensure_ascii=False,
    )
    assert "TTP" not in schema_protocol
    assert "submit_ttp_template" not in schema_protocol
    assert set(schema_contract["properties"]) == {
        "result_schema",
        "assumptions",
    }
    for property_schema in schema_contract["properties"].values():
        assert _contains_chinese(property_schema["description"])

    assert "$defs" not in schema_contract

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

    finish_contract = FinishGenerationTool.input_schema
    assert finish_contract["properties"] == {}
    assert finish_contract["additionalProperties"] is False
    assert _contains_chinese(FinishGenerationTool.description)


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
    )
    assert _payload(rejected)["accepted"] is False
    assert session.frozen_schema is None
    assert session.schema_submissions == 1
    accepted_schema = _schema()
    accepted = await tool.call(
        result_schema=accepted_schema,
        assumptions=["这些值按标签处理。"],
    )
    assert _payload(accepted)["accepted"] is True
    assert _payload(accepted)["next_action"] == "finish_schema"
    assert session.schema_submissions == 2
    assert session.frozen_schema == _schema()
    assert session.assumptions == ("这些值按标签处理。",)
    assert seen[-1].command_outputs == ("value: one", "value: two")
    accepted_schema["properties"]["value"]["type"] = "integer"
    replacement = await tool.call(
        result_schema=_schema("replacement"),
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
    result = await SubmitResultSchemaTool(session).call(
        result_schema=schema,
        assumptions=["按字符串处理。"],
    )
    payload = _payload(result)

    assert starts == [
        {
            "name": SUBMIT_SCHEMA_TOOL_NAME,
            "input": {
                "result_schema": schema,
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
    secret = "schema-unknown-field-secret-7b459b"
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
                    "assumptions": [{"untrusted": secret}],
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
        raise RuntimeError(f"{secret}: {candidate.result_schema!r}")

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
                "assumptions": [secret],
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

    assert _tool_text(result) == "[]\n错误：模板未产生可用的匹配结果。"
    assert session.ttp_submissions == 0
    assert session.last_ttp_template is None


@pytest.mark.asyncio
async def test_model_receives_separately_labelled_records_for_each_input() -> None:
    records = [
        {
            "hostname": 'r1 "edge"',
            "interfaces": [{"name": "Gi0", "status": "up"}],
        },
        {
            "hostname": "路由器\n二号",
            "interfaces": [{"name": "Gi1", "status": "down"}],
        },
    ]
    session = GenerationSession(
        command_outputs=["first", "second"],
        schema_validator=_unused_schema_validator,
        template_validator=lambda candidate: ValidatorOutcome(
            valid=True,
            records=tuple(records),
        ),
    )
    session.frozen_schema = _schema()

    result = await SubmitTtpTemplateTool(session).call("{{ value }}")
    text = _tool_text(result)

    assert text.startswith("以下是按输入顺序分别返回的解析结果。")
    assert not text.startswith("[")
    assert text.count("<parsed_record ") == 2
    assert text.count("</parsed_record>") == 2
    assert '<parsed_record input_index="0" display_number="1">' in text
    assert '<parsed_record input_index="1" display_number="2">' in text
    assert _matched_records(result) == records
    assert session.records == tuple(records)


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
    assert text == "[]\n错误：模板未通过语法或安全检查。"
    assert secret not in text
    assert "input_value" not in text
    assert session.ttp_submissions == 0
    assert session.last_ttp_template is None
    assert cast(dict[str, Any], session.last_issues[0])["code"] == (
        "ttp.submission_invalid"
    )


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
    assert text == "[]\n错误：模板解析未能完成。"
    assert secret not in text
    assert any(
        isinstance(event, ToolResultEndEvent) and event.state == ToolResultState.SUCCESS
        for event in events
    )
    assert session.ttp_submissions == 1
    assert session.validated_ttp_template is None
    assert session.first_ttp_valid is False


@pytest.mark.asyncio
async def test_other_failure_returns_empty_records_and_fixed_chinese_error() -> None:
    session = GenerationSession(
        command_outputs=["value: one"],
        schema_validator=_unused_schema_validator,
        template_validator=lambda candidate: ValidatorOutcome(
            valid=False,
            issues=(
                {
                    "code": "schema.record_mismatch",
                    "stage": "ttp",
                    "message": "private validator detail",
                },
            ),
        ),
    )
    session.frozen_schema = _schema()

    result = await SubmitTtpTemplateTool(session).call("value: {{ value }}")

    assert _tool_text(result) == "[]\n错误：模板未产生可用的匹配结果。"
    assert "private validator detail" not in _tool_text(result)


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

    assert _matched_records(result) == captured_records
    assert session.records == ()
    assert session.validated_ttp_template is None
    assert session.last_issues == issues
    assert "capture" not in json.dumps(session.last_issues)


@pytest.mark.asyncio
async def test_model_receives_complete_records_larger_than_capture_limit() -> None:
    large_value = "x" * (40 * 1024)
    records = [{"value": large_value}]
    session = GenerationSession(
        command_outputs=["value: one"],
        schema_validator=_unused_schema_validator,
        template_validator=lambda candidate: ValidatorOutcome(
            valid=True,
            records=tuple(records),
        ),
    )
    session.frozen_schema = _schema()

    result = await SubmitTtpTemplateTool(session).call("value: {{ value }}")

    assert len(_tool_text(result).encode("utf-8")) > 32 * 1024
    assert _matched_records(result) == records


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
                    "code": "schema.record_mismatch",
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
    assert _matched_records(result) == [{}]

    assert starts == [
        {
            "name": SUBMIT_TEMPLATE_TOOL_NAME,
            "input": {"ttp_template": "Value: {{ value }}"},
            "span_type": "TOOL",
        },
    ]
    assert len(finishes) == 1
    assert finishes[0]["outcome"] == "success"
    assert finishes[0]["attributes"] == {
        "phase": "template",
        "accepted": False,
        "ttp_submission": 1,
    }
    assert finishes[0]["output"]["accepted"] is False
    assert finishes[0]["output"]["issues"][0]["code"] == "schema.record_mismatch"
    assert finishes[0]["output"]["capture"]["records"] == [{}]


@pytest.mark.asyncio
async def test_template_progress_retains_diagnostic_payload() -> None:
    observed: list[CustomEvent] = []
    progress = ProgressEmitter(
        request_id="request-1",
        observer=lambda event: observed.append(cast(CustomEvent, event)),
    )
    session = GenerationSession(
        command_outputs=["unmatched"],
        schema_validator=_unused_schema_validator,
        template_validator=lambda candidate: ValidatorOutcome(
            valid=False,
            issues=(
                {
                    "code": "schema.record_mismatch",
                    "stage": "ttp",
                    "message": "no match",
                },
            ),
            records=({},),
        ),
    )
    session.frozen_schema = _schema()

    result = await SubmitTtpTemplateTool(session, progress).call(
        "Value: {{ value }}",
    )

    assert _matched_records(result) == [{}]
    completed = [
        event
        for event in observed
        if event.name == "cli_parser.tool.result"
    ]
    assert len(completed) == 1
    diagnostic = completed[0].value["output"]
    assert diagnostic["accepted"] is False
    assert diagnostic["issues"][0]["code"] == "schema.record_mismatch"
    assert diagnostic["capture"]["records"] == [{}]


@pytest.mark.asyncio
async def test_finish_generation_tool_records_a_tool_span(
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
        schema_validator=_unused_schema_validator,
        template_validator=_unused_template_validator,
    )
    session.frozen_schema = _schema()
    session.validated_ttp_template = "value: {{ value }}"
    session.records = ({"value": "one"},)

    result = await FinishGenerationTool(session).call()
    payload = _payload(result)

    assert starts == [
        {
            "name": FINISH_GENERATION_TOOL_NAME,
            "input": {},
            "span_type": "TOOL",
        },
    ]
    assert finishes == [
        {
            "output": payload,
            "outcome": "success",
            "attributes": {
                "phase": "template",
                "accepted": True,
            },
        },
    ]


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
async def test_valid_template_remains_a_candidate_until_explicit_finish() -> None:
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

    assert _matched_records(result) == returned_records
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
    assert session.has_validated_ttp_candidate
    assert session.generation_finished is False
    assert not session.succeeded
    assert session.first_ttp_valid is True
    assert session.terminal_reason is None
    returned_records[0]["nested"]["index"] = 99
    assert session.records[0]["nested"]["index"] == 0

    finished = await FinishGenerationTool(session).call()

    finish_payload = _payload(finished)
    assert finish_payload == {
        "phase": "template",
        "accepted": True,
        "issues": [],
        "generation_finished": True,
        "validated_candidate_available": True,
    }
    assert session.generation_finished
    assert session.succeeded
    assert session.terminal_reason == "success"
    assert session.ttp_submissions == 1


@pytest.mark.asyncio
async def test_empty_object_candidate_can_be_saved_and_finished() -> None:
    session = GenerationSession(
        command_outputs=["unmatched"],
        schema_validator=_unused_schema_validator,
        template_validator=lambda candidate: ValidatorOutcome(
            valid=True,
            records=({},),
        ),
    )
    session.frozen_schema = {
        "type": "object",
        "properties": {"value": {"type": "string"}},
        "required": [],
        "additionalProperties": False,
    }

    submitted = await SubmitTtpTemplateTool(session).call(
        "Value: {{ value }}",
    )
    finished = await FinishGenerationTool(session).call()

    assert _matched_records(submitted) == [{}]
    assert session.records == ({},)
    assert session.validated_ttp_template == "Value: {{ value }}"
    assert _payload(finished)["accepted"] is True


@pytest.mark.asyncio
async def test_finish_generation_rejects_without_a_validated_candidate() -> None:
    session = GenerationSession(
        command_outputs=["value: one"],
        schema_validator=_unused_schema_validator,
        template_validator=_unused_template_validator,
    )
    session.frozen_schema = _schema()

    result = await FinishGenerationTool(session).call()

    payload = _payload(result)
    assert payload["accepted"] is False
    assert payload["issues"][0]["code"] == (
        "generation.finish_without_valid_candidate"
    )
    assert payload["generation_finished"] is False
    assert payload["validated_candidate_available"] is False
    assert payload["next_action"] == "correct_and_resubmit_template"
    assert not session.has_validated_ttp_candidate
    assert not session.generation_finished
    assert not session.succeeded
    assert session.ttp_submissions == 0


@pytest.mark.asyncio
async def test_finish_generation_locks_the_selected_candidate() -> None:
    submissions = 0

    async def validate_template(candidate: TemplateCandidate) -> ValidatorOutcome:
        nonlocal submissions
        submissions += 1
        return ValidatorOutcome(
            valid=True,
            records=({"value": f"candidate-{submissions}"},),
        )

    session = GenerationSession(
        command_outputs=["value: one"],
        schema_validator=_unused_schema_validator,
        template_validator=validate_template,
    )
    session.frozen_schema = _schema()
    submit_tool = SubmitTtpTemplateTool(session)
    finish_tool = FinishGenerationTool(session)

    await submit_tool.call("first: {{ value }}")
    await submit_tool.call("second: {{ value }}")

    assert session.validated_ttp_template == "second: {{ value }}"
    assert session.records == ({"value": "candidate-2"},)
    assert not session.succeeded

    assert _payload(await finish_tool.call())["accepted"] is True

    repeated_finish = _payload(await finish_tool.call())
    rejected_submit = _tool_text(await submit_tool.call("third: {{ value }}"))
    assert repeated_finish["accepted"] is False
    assert repeated_finish["issues"][0]["code"] == "generation_already_succeeded"
    assert rejected_submit == "[]\n错误：模板未产生可用的匹配结果。"
    assert session.validated_ttp_template == "second: {{ value }}"
    assert session.records == ({"value": "candidate-2"},)
    assert session.ttp_submissions == 2


@pytest.mark.asyncio
async def test_rejected_revision_preserves_the_previous_valid_candidate() -> None:
    templates: list[str] = []

    def validate_template(candidate: TemplateCandidate) -> ValidatorOutcome:
        templates.append(candidate.ttp_template)
        if len(templates) == 1:
            return ValidatorOutcome(valid=True, records=({"value": "one"},))
        return ValidatorOutcome(
            valid=False,
            issues=(
                {
                    "code": "ttp.test_rejected",
                    "stage": "template",
                    "message": "The revision is invalid.",
                },
            ),
            records=({},),
        )

    session = GenerationSession(
        command_outputs=["value: one"],
        schema_validator=_unused_schema_validator,
        template_validator=validate_template,
    )
    session.frozen_schema = _schema()
    tool = SubmitTtpTemplateTool(session)

    await tool.call("value: {{ value }}")
    rejected = await tool.call("changed: {{ value }}")

    assert _matched_records(rejected) == [{}]
    assert session.validated_ttp_template == "value: {{ value }}"
    assert session.records == ({"value": "one"},)
    assert session.has_validated_ttp_candidate
    assert not session.succeeded


@pytest.mark.asyncio
async def test_finish_generation_cannot_bypass_the_submission_limit() -> None:
    session = GenerationSession(
        command_outputs=["value: one"],
        schema_validator=_unused_schema_validator,
        template_validator=lambda candidate: ValidatorOutcome(
            valid=True,
            records=({"value": "one"},),
        ),
        max_ttp_submissions=1,
    )
    session.frozen_schema = _schema()

    submitted = await SubmitTtpTemplateTool(session).call("value: {{ value }}")
    finished = await FinishGenerationTool(session).call()

    assert _matched_records(submitted) == [{"value": "one"}]
    finish_payload = _payload(finished)
    assert finish_payload["accepted"] is False
    assert finish_payload["issues"][0]["code"] == "ttp_submission_limit"
    assert finish_payload["validated_candidate_available"] is True
    assert finish_payload["generation_finished"] is False
    assert session.has_validated_ttp_candidate
    assert not session.generation_finished
    assert not session.succeeded
    assert session.terminal_reason == "ttp_submission_limit"


@pytest.mark.asyncio
async def test_finish_generation_cannot_overwrite_a_terminal_failure() -> None:
    session = GenerationSession(
        command_outputs=["value: one"],
        schema_validator=_unused_schema_validator,
        template_validator=_unused_template_validator,
    )
    session.frozen_schema = _schema()
    session.validated_ttp_template = "value: {{ value }}"
    session.records = ({"value": "one"},)
    session.terminal_reason = "ttp_worker_unavailable"

    payload = _payload(await FinishGenerationTool(session).call())

    assert payload["accepted"] is False
    assert payload["issues"][0]["code"] == "generation_already_terminated"
    assert payload["validated_candidate_available"] is True
    assert session.terminal_reason == "ttp_worker_unavailable"
    assert not session.generation_finished
    assert not session.succeeded


@pytest.mark.asyncio
async def test_submission_limit_does_not_overwrite_a_worker_failure() -> None:
    session: GenerationSession

    def validate_template(candidate: TemplateCandidate) -> ValidatorOutcome:
        del candidate
        session.terminal_reason = "ttp_worker_unavailable"
        return ValidatorOutcome(
            valid=False,
            issues=(
                {
                    "code": "ttp.worker_start_failed",
                    "stage": "template",
                    "message": "The worker could not start.",
                },
            ),
        )

    session = GenerationSession(
        command_outputs=["value: one"],
        schema_validator=_unused_schema_validator,
        template_validator=validate_template,
        max_ttp_submissions=1,
    )
    session.frozen_schema = _schema()

    result = await SubmitTtpTemplateTool(session).call("value: {{ value }}")

    assert _tool_text(result) == "[]\n错误：模板解析未能完成。"
    assert session.ttp_submissions == 1
    assert session.terminal_reason == "ttp_worker_unavailable"


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

    assert _matched_records(result) == [{"value": "one"}]
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

    assert _matched_records(result) == records
    assert cast(dict[str, Any], session.last_issues[-1])["code"] == issue_code
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

    assert _tool_text(first) == "[]\n错误：模板未产生可用的匹配结果。"
    assert _tool_text(second) == "[]\n错误：模板未产生可用的匹配结果。"
    assert _tool_text(blocked) == "[]\n错误：模板未产生可用的匹配结果。"
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

    assert _tool_text(repeated) == "[]\n错误：模板未产生可用的匹配结果。"
    assert session.last_issues == (
        {
            "code": "ttp.unchanged_submission",
            "stage": "template",
            "message": (
                "The template is identical to the previous submission. "
                "Finish the stored validated candidate when available, "
                "or modify the template before resubmission."
            ),
            "details": {"required_action": "modify_template"},
        },
    )
    assert seen_templates == ["value: {{ value }}"]
    assert session.ttp_submissions == 2


@pytest.mark.parametrize(
    ("phase", "tool_types"),
    [
        ("schema", [SubmitResultSchemaTool]),
        ("ttp", [SubmitTtpTemplateTool, FinishGenerationTool]),
    ],
)
def test_phase_toolkit_builder_returns_fixed_phase_tools(
    phase: GenerationPhase,
    tool_types: list[type[Any]],
) -> None:
    session = GenerationSession(
        command_outputs=["value: one"],
        schema_validator=_unused_schema_validator,
        template_validator=_unused_template_validator,
    )

    tools = build_submission_tools(session, phase)

    assert [type(tool) for tool in tools] == tool_types


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
