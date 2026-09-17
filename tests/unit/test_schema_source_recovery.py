"""Source positions may enrich one existing Schema retry, never model policy."""

from __future__ import annotations

import asyncio
import json
import time
from copy import deepcopy

import httpx
import openai
import pytest
from lmnr import Laminar

from cli_parser_agent import (
    GenerationPolicy,
    GenerationRequest,
    TtpGenerator,
    TtpGeneratorSettings,
)
from cli_parser_agent.ttp_generation.agent.builder import (
    build_agent,
    build_schema_task_message,
)
from cli_parser_agent.ttp_generation.agent.model_attempts import _ProviderReplyFacts
from cli_parser_agent.ttp_generation.agent.prompt import (
    SCHEMA_NO_TOOL_RETRY_PROMPT,
    build_schema_task_prompt,
    build_ttp_task_prompt,
)
from cli_parser_agent.ttp_generation.agent.runner import (
    _retry_message,
    _schema_source_retry_message,
)
from cli_parser_agent.ttp_generation.agent.session import GenerationSession
from cli_parser_agent.ttp_generation.progress import ProgressEmitter
from cli_parser_agent.ttp_generation.sampling import (
    SampledCommandOutput,
    sample_command_outputs,
)

SCHEMA = {
    "type": "object",
    "properties": {"value": {"type": "string"}},
    "required": ["value"],
    "additionalProperties": False,
}


def _completion(*, finish="length", reasoning=True, text=None, tool=None, args=None):
    message = {"role": "assistant", "content": text}
    if reasoning:
        message["reasoning_content"] = "untrusted-private-reasoning"
    if tool:
        message["tool_calls"] = [
            {
                "id": "call-" + tool,
                "type": "function",
                "function": {"name": tool, "arguments": json.dumps(args)},
            }
        ]
    return {
        "id": "offline-source",
        "object": "chat.completion",
        "created": 0,
        "model": "offline",
        "choices": [{"index": 0, "finish_reason": finish, "message": message}],
        "usage": {"prompt_tokens": 10, "completion_tokens": 20, "total_tokens": 30},
    }


@pytest.fixture(autouse=True)
def _no_trace(monkeypatch):
    monkeypatch.setattr(Laminar, "is_initialized", lambda: False)


def _settings():
    return TtpGeneratorSettings(
        api_key="offline", model_name="offline", base_url="https://api.deepseek.com"
    )


@pytest.mark.parametrize("failed_rounds", [0, 1, 3])
@pytest.mark.parametrize("partial_tool", [False, True])
async def test_source_only_in_first_length_retry_and_never_ttp(
    monkeypatch, failed_rounds, partial_tool
):
    source = "Value: one\n"
    replies = [_completion() for _ in range(failed_rounds)] + [
        _completion(
            finish="tool_calls",
            tool="submit_result_schema",
            args={"result_schema": SCHEMA},
        ),
        _completion(
            finish="tool_calls",
            tool="submit_ttp_template",
            args={"ttp_template": "Value: {{ value }}"},
        ),
        _completion(finish="tool_calls", tool="finish_generation", args={}),
    ]
    if failed_rounds and partial_tool:
        replies[0] = _completion(
            tool="submit_result_schema", args={"result_schema": SCHEMA}
        )
        replies[0]["choices"][0]["message"]["tool_calls"][0]["function"][
            "arguments"
        ] = json.dumps({"result_schema": SCHEMA})[:-2]
    requests, events = [], []

    def respond(request):
        requests.append(json.loads(request.content))
        assert len(requests) <= len(replies)
        return httpx.Response(200, json=replies[len(requests) - 1])

    real_client = openai.AsyncClient
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        monkeypatch.setattr(
            openai,
            "AsyncClient",
            lambda **kwargs: real_client(**kwargs, http_client=client),
        )
        result = await TtpGenerator(settings=_settings()).generate(
            GenerationRequest(command_outputs=[source]), observer=events.append
        )
    assert result.status == "success"
    assert result.artifact.result_schema == SCHEMA
    assert result.artifact.records == [{"value": "one"}]
    assert len(requests) == failed_rounds + 3
    assert requests[0]["messages"][-1]["content"] == [
        {"type": "text", "text": build_schema_task_prompt([source])}
    ]
    for request in requests:
        assert request["max_tokens"] == 8192
        assert "reasoning_effort" not in request
        assert "thinking" not in request
        assert "untrusted-private-reasoning" not in json.dumps(request)
    source_events = [
        event
        for event in events
        if getattr(event, "name", None) == "cli_parser.schema.source_view"
    ]
    assert len(source_events) == int(failed_rounds > 0)
    if failed_rounds:
        assert source_events[0].value["status"] == "injected"
        assert source_events[0].value["round_index"] == 1
        assert "Value" not in json.dumps(source_events[0].value)
        fourth_messages = requests[failed_rounds]["messages"]
        blocks = [block for msg in fourth_messages for block in msg["content"]]
        assert sum("token 不等于业务字段" in block["text"] for block in blocks) == 1
        assert requests[1]["messages"][-1]["content"][0]["text"] == (
            SCHEMA_NO_TOOL_RETRY_PROMPT
        )
    for request in requests[failed_rounds + 1 :]:
        assert "token 不等于业务字段" not in json.dumps(request, ensure_ascii=False)
    assert requests[failed_rounds + 1]["messages"][-1]["content"] == [
        {"type": "text", "text": build_ttp_task_prompt([source], SCHEMA)}
    ]


def _prepared_agent(*, phase="schema", samples=None):
    session = GenerationSession(
        command_outputs=("Value: one",),
        schema_validator=lambda _: None,
        template_validator=lambda _: None,
        min_round_seconds=1,
    )
    agent = build_agent(
        settings=_settings(), policy=GenerationPolicy(), session=session, phase=phase
    )
    agent.model.configure_schema_source_samples(
        tuple(sample_command_outputs(["Value: one"])) if samples is None else samples
    )
    session.record_agent_round(phase)
    session.model_attempts_observed = 1
    agent.model._provider_reply_facts = _ProviderReplyFacts(
        round_index=1,
        attempt_index=1,
        finished_reason="length",
        reasoning_present=True,
        completed=True,
    )
    message = build_schema_task_message(["Value: one"])
    agent.state.context.append(deepcopy(message))
    return agent, session, message


@pytest.mark.parametrize(
    "change", ["stop", "text", "no_reasoning", "incomplete", "stale", "ttp"]
)
async def test_other_replies_do_not_trigger_source(change):
    agent, session, message = _prepared_agent(
        phase="ttp" if change == "ttp" else "schema"
    )
    facts = agent.model._provider_reply_facts
    if change == "stop":
        facts.finished_reason = "stop"
    elif change == "text":
        facts.text_present = True
    elif change == "no_reasoning":
        facts.reasoning_present = False
    elif change == "incomplete":
        facts.completed = False
    elif change == "stale":
        facts.attempt_index = 0
    events = []
    retry = _retry_message("schema")
    assert (
        await _schema_source_retry_message(
            agent, message, retry, session, ProgressEmitter("test", events.append)
        )
        is retry
    )
    assert not events


@pytest.mark.parametrize(
    ("skip", "status"),
    [
        ("truncated", "skipped_no_source"),
        ("summary", "skipped_history"),
        ("missing", "skipped_history"),
        ("context", "skipped_context"),
        ("after_count", "skipped_deadline"),
        ("count_error", "skipped_count"),
        ("prepare_error", "skipped_count"),
    ],
)
async def test_missing_original_or_context_or_time_skips_without_mutation(
    monkeypatch, skip, status
):
    samples = (SampledCommandOutput(0, "sampled fragment", True, 999, 20),)
    agent, session, message = _prepared_agent(
        samples=samples if skip == "truncated" else None
    )
    if skip == "summary":
        agent.state.summary = "summary"
    elif skip == "missing":
        agent.state.context.clear()
    captured = []

    async def count(messages, tools):
        captured.append((messages, tools))
        if skip == "after_count":
            session.deadline_monotonic = time.monotonic() - 1
        if skip == "count_error":
            raise RuntimeError("private source body")
        return agent.model.context_size // 2 if skip == "context" else 1

    monkeypatch.setattr(agent.model, "count_tokens", count)
    if skip == "prepare_error":

        async def failed_prepare():
            raise RuntimeError("private source body")

        monkeypatch.setattr(agent, "_prepare_model_input", failed_prepare)
    before = deepcopy(agent.state.context)
    events = []
    retry = _retry_message("schema")
    progress = ProgressEmitter("test", events.append)
    result = await _schema_source_retry_message(
        agent, message, retry, session, progress
    )
    assert result is retry
    assert agent.state.context == before
    assert len(events) == 1
    assert events[0].value["status"] == status
    assert "private source body" not in json.dumps(events[0].value)
    assert (
        await _schema_source_retry_message(agent, message, retry, session, progress)
        is retry
    )
    assert len(events) == 1
    if captured:
        assert captured[0][0][0].role == "system"
        assert captured[0][1][0]["function"]["name"] == "submit_result_schema"
        assert len(captured[0][0][-1].get_content_blocks()) == 2


async def test_cancel_during_context_count_does_not_append_source(monkeypatch):
    agent, session, message = _prepared_agent()
    before = deepcopy(agent.state.context)

    async def count(*_):
        raise asyncio.CancelledError()

    monkeypatch.setattr(agent.model, "count_tokens", count)
    with pytest.raises(asyncio.CancelledError):
        await _schema_source_retry_message(
            agent, message, _retry_message("schema"), session, None
        )
    assert agent.state.context == before
    assert agent.model.take_schema_source_view() is None


async def test_expired_budget_prevents_source_build_and_event(monkeypatch):
    agent, session, message = _prepared_agent()
    session.deadline_monotonic = time.monotonic() - 1

    def forbidden_build():
        raise AssertionError("Source rendering must not begin after deadline")

    monkeypatch.setattr(agent.model, "take_schema_source_view", forbidden_build)
    events = []
    retry = _retry_message("schema")
    assert (
        await _schema_source_retry_message(
            agent, message, retry, session, ProgressEmitter("test", events.append)
        )
        is retry
    )
    assert not events


@pytest.mark.parametrize("limit", ["half", "compression", "output_reserve"])
async def test_actual_count_must_be_strictly_below_every_context_bound(
    monkeypatch, limit
):
    agent, session, message = _prepared_agent()
    agent.model.context_size = 20000
    if limit == "compression":
        agent.context_config.trigger_ratio = 0.25
    elif limit == "output_reserve":
        agent.model.parameters.max_tokens = 16000
    threshold = min(
        10000,
        int(20000 * agent.context_config.trigger_ratio),
        20000 - agent.model.parameters.max_tokens,
    )

    async def count(*_):
        return threshold

    monkeypatch.setattr(agent.model, "count_tokens", count)
    events = []
    retry = _retry_message("schema")
    assert (
        await _schema_source_retry_message(
            agent, message, retry, session, ProgressEmitter("test", events.append)
        )
        is retry
    )
    assert events[0].value["status"] == "skipped_context"
    assert events[0].value["estimated_tokens"] == threshold


async def test_no_tool_exhaustion_still_stops_after_four_calls(monkeypatch):
    requests, events = [], []

    def respond(request):
        requests.append(json.loads(request.content))
        return httpx.Response(200, json=_completion())

    real_client = openai.AsyncClient
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        monkeypatch.setattr(
            openai,
            "AsyncClient",
            lambda **kwargs: real_client(**kwargs, http_client=client),
        )
        result = await TtpGenerator(settings=_settings()).propose_schema(
            GenerationRequest(command_outputs=["Value: one"]), observer=events.append
        )
    assert len(requests) == 4
    assert result.status == "failed"
    assert result.proposal is None
    assert (
        sum(getattr(e, "name", None) == "cli_parser.schema.source_view" for e in events)
        == 1
    )
