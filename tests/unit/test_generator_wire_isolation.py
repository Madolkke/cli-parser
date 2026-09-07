from __future__ import annotations

import json
from copy import deepcopy
from types import SimpleNamespace
from typing import Any

import openai
import pytest
from openai.types.chat import ChatCompletion

from cli_parser_agent import (
    GenerationPolicy,
    GenerationRequest,
    TemplateRequest,
    TtpGenerator,
    TtpGeneratorSettings,
    ValidationIssue,
)
from cli_parser_agent.ttp_generation import workflow as workflow_module
from cli_parser_agent.ttp_generation.agent import (
    FINISH_GENERATION_TOOL_NAME,
    SUBMIT_SCHEMA_TOOL_NAME,
    SUBMIT_TEMPLATE_TOOL_NAME,
    TEST_TEMPLATE_TOOL_NAME,
    TTP_SYSTEM_PROMPT,
    build_ttp_task_prompt,
)
from cli_parser_agent.ttp_generation.agent import runner as runner_module
from cli_parser_agent.ttp_generation.validation import TtpParseResult

_SCHEMA_FREE_TEXT_MARKER = "schema-free-text-only-7c134b"
_SCHEMA_THINKING_MARKER = "schema-thinking-only-0ab218"
_SCHEMA_RETRY_MARKER = "schema-retry-only-b47d5f"
_REJECTED_SCHEMA_MARKER = "rejected-schema-only-5a106e"
_REJECTION_ISSUE_MARKER = "schema-issue-only-d28547"
_SCHEMA_USAGE_NUMBERS = (810_031, 810_032, 1_620_063)


def _result_schema() -> dict[str, Any]:
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "type": "object",
        "properties": {"value": {"type": "string"}},
        "required": ["value"],
        "additionalProperties": False,
    }


def _completion(
    *,
    content: str | None = None,
    reasoning_content: str | None = None,
    tool_name: str | None = None,
    tool_arguments: dict[str, Any] | None = None,
    tool_call_id: str = "call-test",
    usage: tuple[int, int, int] = (1, 1, 2),
) -> ChatCompletion:
    message: dict[str, Any] = {
        "role": "assistant",
        "content": content,
    }
    finish_reason = "stop"
    if reasoning_content is not None:
        message["reasoning_content"] = reasoning_content
    if tool_name is not None:
        message["tool_calls"] = [
            {
                "id": tool_call_id,
                "type": "function",
                "function": {
                    "name": tool_name,
                    "arguments": json.dumps(
                        tool_arguments,
                        ensure_ascii=False,
                        separators=(",", ":"),
                    ),
                },
            },
        ]
        finish_reason = "tool_calls"

    prompt_tokens, completion_tokens, total_tokens = usage
    return ChatCompletion.model_validate(
        {
            "id": f"chatcmpl-{tool_call_id}",
            "object": "chat.completion",
            "created": 0,
            "model": "test-model",
            "choices": [
                {
                    "index": 0,
                    "finish_reason": finish_reason,
                    "message": message,
                },
            ],
            "usage": {
                "prompt_tokens": prompt_tokens,
                "completion_tokens": completion_tokens,
                "total_tokens": total_tokens,
            },
        },
    )


def _request_text(request: dict[str, Any]) -> str:
    return json.dumps(request, ensure_ascii=False, default=str)


def _tool_feedback(request: dict[str, Any], call_id: str) -> dict[str, Any]:
    messages = [
        message
        for message in request["messages"]
        if message.get("role") == "tool" and message.get("tool_call_id") == call_id
    ]
    assert len(messages) == 1
    content = messages[0]["content"]
    text = (
        content
        if isinstance(content, str)
        else "".join(block["text"] for block in content)
    )
    assert text.startswith("<validation_feedback>\n")
    serialized, separator, _ = text.removeprefix("<validation_feedback>\n").partition(
        "\n</validation_feedback>",
    )
    assert separator
    return json.loads(serialized)


async def test_pipe_gate_feedback_reaches_next_request_and_allows_repair(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    requests: list[dict[str, Any]] = []

    async def create_completion(**kwargs: Any) -> ChatCompletion:
        requests.append(deepcopy(kwargs))
        if len(requests) == 1:
            return _completion(
                tool_name=SUBMIT_TEMPLATE_TOOL_NAME,
                tool_arguments={"ttp_template": 'value: {{ value | re("one|two") }}'},
                tool_call_id="bad-pipe",
            )
        if len(requests) == 2:
            feedback = _tool_feedback(kwargs, "bad-pipe")
            assert feedback["accepted"] is False
            assert feedback["issues"][0]["code"] == "ttp.incompatible_argument_pipe"
            assert feedback["issues"][0]["details"]["required_action"] == (
                "split_pipe_argument"
            )
            return _completion(
                tool_name=SUBMIT_TEMPLATE_TOOL_NAME,
                tool_arguments={
                    "ttp_template": 'value: {{ value | re("one") | re("two") }}'
                },
                tool_call_id="repaired",
            )
        assert len(requests) == 3
        assert _tool_feedback(kwargs, "repaired")["accepted"] is True
        return _completion(
            tool_name=FINISH_GENERATION_TOOL_NAME,
            tool_arguments={},
            tool_call_id="finished",
        )

    monkeypatch.setattr(
        openai,
        "AsyncClient",
        lambda **_: SimpleNamespace(
            chat=SimpleNamespace(completions=SimpleNamespace(create=create_completion))
        ),
    )
    generator = TtpGenerator(
        settings=TtpGeneratorSettings(api_key="offline", model_name="offline"),
    )
    result = await generator.generate_from_schema(
        TemplateRequest(
            command_outputs=["value: one\n"], result_schema=_result_schema()
        )
    )
    assert result.status == "success"
    assert len(requests) == 3


async def test_first_ttp_wire_request_has_no_schema_phase_history(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Exercise real AgentScope state/formatting across the phase handoff."""

    command_output = "value: one"
    frozen_schema = _result_schema()
    schema_requests: list[dict[str, Any]] = []
    ttp_requests: list[dict[str, Any]] = []

    rejected_submission = {
        "result_schema": {
            **frozen_schema,
            "title": _REJECTED_SCHEMA_MARKER,
        },
    }
    accepted_submission = {
        "result_schema": frozen_schema,
    }

    async def create_completion(**kwargs: Any) -> ChatCompletion:
        request = deepcopy(kwargs)
        tool_names = [item["function"]["name"] for item in request.get("tools", [])]
        if tool_names == [SUBMIT_SCHEMA_TOOL_NAME]:
            schema_requests.append(request)
            schema_call = len(schema_requests)
            if schema_call == 1:
                return _completion(
                    content=_SCHEMA_FREE_TEXT_MARKER,
                    reasoning_content=_SCHEMA_THINKING_MARKER,
                    usage=_SCHEMA_USAGE_NUMBERS,
                )
            if schema_call == 2:
                return _completion(
                    tool_name=SUBMIT_SCHEMA_TOOL_NAME,
                    tool_arguments=rejected_submission,
                    tool_call_id="call-schema-rejected",
                )
            if schema_call == 3:
                return _completion(
                    tool_name=SUBMIT_SCHEMA_TOOL_NAME,
                    tool_arguments=accepted_submission,
                    tool_call_id="call-schema-accepted",
                )
            raise AssertionError("Schema agent made an unexpected extra model call")

        if tool_names == [
            SUBMIT_TEMPLATE_TOOL_NAME,
            TEST_TEMPLATE_TOOL_NAME,
            FINISH_GENERATION_TOOL_NAME,
        ]:
            ttp_requests.append(request)
            if len(ttp_requests) == 1:
                return _completion(
                    tool_name=SUBMIT_TEMPLATE_TOOL_NAME,
                    tool_arguments={"ttp_template": "missing: {{ value }}"},
                    tool_call_id="call-ttp-rejected",
                )
            if len(ttp_requests) == 2:
                return _completion(
                    tool_name=TEST_TEMPLATE_TOOL_NAME,
                    tool_arguments={
                        "text": "unmatched",
                        "ttp_template": "value: {{ value }}",
                    },
                    tool_call_id="call-ttp-test",
                )
            if len(ttp_requests) == 3:
                return _completion(
                    tool_name=SUBMIT_TEMPLATE_TOOL_NAME,
                    tool_arguments={"ttp_template": "value: {{ value | ORPHRASE }}"},
                    tool_call_id="call-ttp-accepted",
                )
            if len(ttp_requests) == 4:
                return _completion(
                    tool_name=FINISH_GENERATION_TOOL_NAME,
                    tool_arguments={},
                    tool_call_id="call-ttp-finish",
                )
            raise AssertionError("TTP agent made an unexpected extra model call")

        raise AssertionError(f"unexpected wire tool set: {tool_names!r}")

    def build_fake_client(**kwargs: Any) -> SimpleNamespace:
        del kwargs
        return SimpleNamespace(
            chat=SimpleNamespace(
                completions=SimpleNamespace(create=create_completion),
            ),
        )

    schema_validation_calls = 0

    def validate_schema(
        result_schema: dict[str, Any],
        *_: Any,
        **__: Any,
    ) -> list[ValidationIssue]:
        nonlocal schema_validation_calls
        schema_validation_calls += 1
        if result_schema.get("title") == _REJECTED_SCHEMA_MARKER:
            return [
                ValidationIssue(
                    code="schema.test_rejection",
                    message="Rejected only for the isolation regression test.",
                    stage="schema",
                    details={"marker": _REJECTION_ISSUE_MARKER},
                ),
            ]
        return []

    def validate_template(template: str, *_: Any, **__: Any) -> SimpleNamespace:
        if template == "missing: {{ value }}":
            return SimpleNamespace(
                valid=False,
                issues=(
                    ValidationIssue(
                        code="schema.record_mismatch",
                        message="Private validator message must not reach the model.",
                        output_index=0,
                        path="/",
                        details={"keyword": "required", "missing_required": ["value"]},
                    ),
                ),
                records=({},),
            )
        return SimpleNamespace(
            valid=True,
            issues=(),
            records=({"value": "one"},),
        )

    monkeypatch.setattr(openai, "AsyncClient", build_fake_client)
    monkeypatch.setattr(
        runner_module,
        "SCHEMA_NO_TOOL_RETRY_PROMPT",
        _SCHEMA_RETRY_MARKER,
    )
    monkeypatch.setattr(workflow_module, "validate_result_schema", validate_schema)
    monkeypatch.setattr(
        workflow_module,
        "validate_ttp_template",
        validate_template,
    )
    monkeypatch.setattr(
        workflow_module,
        "parse_ttp_template",
        lambda *_args, **_kwargs: TtpParseResult(result=[], issues=[]),
    )

    generator = TtpGenerator(
        settings=TtpGeneratorSettings(
            api_key="test-key",
            model_name="test-model",
        ),
        policy=GenerationPolicy(),
    )
    result = await generator.generate(
        GenerationRequest(command_outputs=[command_output]),
    )

    assert result.status == "success"
    assert len(schema_requests) == 3
    assert len(ttp_requests) == 4
    assert schema_validation_calls == 3

    assert _SCHEMA_RETRY_MARKER in _request_text(schema_requests[1])
    final_schema_request = _request_text(schema_requests[2])
    for marker in (
        _REJECTED_SCHEMA_MARKER,
        _REJECTION_ISSUE_MARKER,
    ):
        assert marker in final_schema_request

    first_ttp_request = ttp_requests[0]
    assert first_ttp_request["messages"] == [
        {
            "role": "system",
            "name": "system",
            "content": [{"type": "text", "text": TTP_SYSTEM_PROMPT}],
        },
        {
            "role": "user",
            "name": "user",
            "content": [
                {
                    "type": "text",
                    "text": build_ttp_task_prompt(
                        [command_output],
                        frozen_schema,
                    ),
                },
            ],
        },
    ]
    assert [item["function"]["name"] for item in first_ttp_request["tools"]] == [
        SUBMIT_TEMPLATE_TOOL_NAME,
        TEST_TEMPLATE_TOOL_NAME,
        FINISH_GENERATION_TOOL_NAME,
    ]
    assert first_ttp_request["parallel_tool_calls"] is False
    assert "tool_choice" not in first_ttp_request

    second_ttp_request = ttp_requests[1]
    assert [item["function"]["name"] for item in second_ttp_request["tools"]] == [
        SUBMIT_TEMPLATE_TOOL_NAME,
        TEST_TEMPLATE_TOOL_NAME,
        FINISH_GENERATION_TOOL_NAME,
    ]
    assert second_ttp_request["parallel_tool_calls"] is False
    assert "tool_choice" not in second_ttp_request
    assert "call-ttp-rejected" in _request_text(second_ttp_request)
    rejected_feedback = _tool_feedback(second_ttp_request, "call-ttp-rejected")
    assert rejected_feedback["accepted"] is False
    assert rejected_feedback["scope"] == "full_input_validation"
    assert rejected_feedback["returned_record_count"] == 1
    assert rejected_feedback["retained_candidate_submission_index"] is None
    assert rejected_feedback["issues"][0]["code"] == "schema.record_mismatch"
    assert rejected_feedback["issues"][0]["path"] == "/"
    assert rejected_feedback["issues"][0]["keyword"] == "required"
    assert "Private validator message" not in _request_text(second_ttp_request)

    test_feedback = _tool_feedback(ttp_requests[2], "call-ttp-test")
    assert test_feedback["scope"] == "parse_only"
    assert test_feedback["parse_succeeded"] is True
    assert test_feedback["tests_used"] == 1
    assert test_feedback["issues"] == []
    assert "accepted" not in test_feedback

    final_request = ttp_requests[3]
    accepted_feedback = _tool_feedback(final_request, "call-ttp-accepted")
    assert accepted_feedback["accepted"] is True
    assert accepted_feedback["candidate_updated"] is True
    assert accepted_feedback["retained_candidate_submission_index"] == 2
    assert accepted_feedback["submissions_used"] == 2
    assert accepted_feedback["issues"] == []
    assert _tool_feedback(final_request, "call-ttp-test") == test_feedback
    rejected_history = [
        message
        for message in final_request["messages"]
        if message.get("tool_call_id") == "call-ttp-rejected"
    ]
    assert len(rejected_history) == 1
    assert "该次提交的匹配结果已被后续提交取代" in _request_text(rejected_history[0])
    assert "validation_feedback" not in _request_text(rejected_history[0])

    ttp_wire_text = _request_text(first_ttp_request)
    for marker in (
        _SCHEMA_FREE_TEXT_MARKER,
        _SCHEMA_THINKING_MARKER,
        _SCHEMA_RETRY_MARKER,
        _REJECTED_SCHEMA_MARKER,
        _REJECTION_ISSUE_MARKER,
    ):
        assert marker not in ttp_wire_text
    for usage_number in _SCHEMA_USAGE_NUMBERS:
        assert str(usage_number) not in ttp_wire_text

    second_ttp_wire_text = _request_text({"requests": ttp_requests[1:]})
    for marker in (
        _SCHEMA_FREE_TEXT_MARKER,
        _SCHEMA_THINKING_MARKER,
        _SCHEMA_RETRY_MARKER,
        _REJECTED_SCHEMA_MARKER,
        _REJECTION_ISSUE_MARKER,
    ):
        assert marker not in second_ttp_wire_text
    for usage_number in _SCHEMA_USAGE_NUMBERS:
        assert str(usage_number) not in second_ttp_wire_text
