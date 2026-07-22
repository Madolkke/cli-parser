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
    TtpGenerator,
    TtpGeneratorSettings,
    ValidationIssue,
)
from cli_parser_agent.ttp_generation import workflow as workflow_module
from cli_parser_agent.ttp_generation.agent import (
    SUBMIT_SCHEMA_TOOL_NAME,
    SUBMIT_TEMPLATE_TOOL_NAME,
    TTP_SYSTEM_PROMPT,
    build_ttp_task_prompt,
)
from cli_parser_agent.ttp_generation.agent import runner as runner_module

_SCHEMA_FREE_TEXT_MARKER = "schema-free-text-only-7c134b"
_SCHEMA_THINKING_MARKER = "schema-thinking-only-0ab218"
_SCHEMA_RETRY_MARKER = "schema-retry-only-b47d5f"
_REJECTED_SCHEMA_MARKER = "rejected-schema-only-5a106e"
_REJECTED_EVIDENCE_MARKER = "rejected-evidence-only-264df7"
_REJECTED_ASSUMPTION_MARKER = "rejected-assumption-only-e0b731"
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
        "evidence": [
            {
                "path": "/value",
                "output_index": 0,
                "excerpt": _REJECTED_EVIDENCE_MARKER,
            },
        ],
        "assumptions": [_REJECTED_ASSUMPTION_MARKER],
    }
    accepted_submission = {
        "result_schema": frozen_schema,
        "evidence": [
            {
                "path": "/value",
                "output_index": 0,
                "excerpt": "one",
            },
        ],
        "assumptions": [],
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

        if tool_names == [SUBMIT_TEMPLATE_TOOL_NAME]:
            ttp_requests.append(request)
            if len(ttp_requests) != 1:
                raise AssertionError("TTP agent made an unexpected extra model call")
            return _completion(
                tool_name=SUBMIT_TEMPLATE_TOOL_NAME,
                tool_arguments={"ttp_template": "value: {{ value | ORPHRASE }}"},
                tool_call_id="call-ttp-accepted",
            )

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

    def validate_template(*_: Any, **__: Any) -> SimpleNamespace:
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
    monkeypatch.setattr(
        workflow_module,
        "validate_schema_proposal",
        validate_schema,
    )
    monkeypatch.setattr(
        workflow_module,
        "validate_ttp_template",
        validate_template,
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
    assert len(ttp_requests) == 1
    assert schema_validation_calls == 3

    assert _SCHEMA_RETRY_MARKER in _request_text(schema_requests[1])
    final_schema_request = _request_text(schema_requests[2])
    for marker in (
        _REJECTED_SCHEMA_MARKER,
        _REJECTED_EVIDENCE_MARKER,
        _REJECTED_ASSUMPTION_MARKER,
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
        SUBMIT_TEMPLATE_TOOL_NAME
    ]
    assert first_ttp_request["parallel_tool_calls"] is False
    assert "tool_choice" not in first_ttp_request

    ttp_wire_text = _request_text(first_ttp_request)
    for marker in (
        _SCHEMA_FREE_TEXT_MARKER,
        _SCHEMA_THINKING_MARKER,
        _SCHEMA_RETRY_MARKER,
        _REJECTED_SCHEMA_MARKER,
        _REJECTED_EVIDENCE_MARKER,
        _REJECTED_ASSUMPTION_MARKER,
        _REJECTION_ISSUE_MARKER,
    ):
        assert marker not in ttp_wire_text
    for usage_number in _SCHEMA_USAGE_NUMBERS:
        assert str(usage_number) not in ttp_wire_text
