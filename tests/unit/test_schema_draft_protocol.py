"""Actual offline transport checks for the request-local lightweight draft path."""

import asyncio
import json
import re
from copy import deepcopy

import httpx
import openai
import pytest

from cli_parser_agent import (
    GenerationPolicy,
    GenerationRequest,
    TemplateRequest,
    TtpGenerator,
    TtpGeneratorSettings,
)
from cli_parser_agent.ttp_generation.agent.prompt import PROMPT_VERSION
from cli_parser_agent.ttp_generation.agent.schema_draft_prompt import (
    SCHEMA_DRAFT_SYSTEM_PROMPT,
)
from cli_parser_agent.ttp_generation.agent.schema_draft_tools import (
    SubmitSchemaDraftTool,
)
from cli_parser_agent.ttp_generation.agent.schema_strategy import (
    DRAFT_PROMPT_VERSION,
    current_prompt_version,
    current_schema_strategy,
    schema_strategy_for_testing,
)
from cli_parser_agent.ttp_generation.agent.session import (
    GenerationSession,
    ValidatorOutcome,
)
from cli_parser_agent.ttp_generation.schema_draft import compile_schema_draft
from cli_parser_agent.ttp_generation.schema_draft_sources import prepare_draft_sources
from cli_parser_agent.ttp_generation.validation import validate_records_against_schema


def draft():
    return {
        "version": 1,
        "fields": [
            {
                "name": {"source": [{"line_id": "i0f0l1", "quote": "Name"}]},
                "required": True,
                "node": {"type": "string"},
            }
        ],
    }


def completion(name, arguments, index):
    return {
        "id": f"offline-{index}",
        "object": "chat.completion",
        "created": 0,
        "model": "offline",
        "choices": [
            {
                "index": 0,
                "finish_reason": "tool_calls",
                "message": {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": f"call-{index}",
                            "type": "function",
                            "function": {
                                "name": name,
                                "arguments": json.dumps(arguments),
                            },
                        }
                    ],
                },
            }
        ],
        "usage": {"prompt_tokens": 2, "completion_tokens": 3, "total_tokens": 5},
    }


@pytest.fixture
def transport(monkeypatch):
    """Keep the real OpenAI SDK serialization; only substitute its HTTP transport."""
    original = openai.AsyncClient
    clients = []

    def install(respond):
        def client(**kwargs):
            http_client = httpx.AsyncClient(transport=httpx.MockTransport(respond))
            clients.append(http_client)
            return original(**{**kwargs, "http_client": http_client})

        monkeypatch.setattr(openai, "AsyncClient", client)

    return install


def generator(**policy):
    return TtpGenerator(
        settings=TtpGeneratorSettings(
            api_key="offline",
            model_name="offline",
            base_url="https://offline.invalid/v1",
            model_max_retries=0,
        ),
        policy=GenerationPolicy(**policy),
    )


def test_actual_prompt_example_compiles_and_describes_multiple_inputs():
    text = re.search(
        r"<draft_example_input>\n(.*?)</draft_example_input>",
        SCHEMA_DRAFT_SYSTEM_PROMPT,
        re.S,
    ).group(1)
    arguments = json.loads(
        re.search(
            r"<draft_example_arguments_json>\n(.*?)</draft_example_arguments_json>",
            SCHEMA_DRAFT_SYSTEM_PROMPT,
            re.S,
        ).group(1)
    )
    outcome = compile_schema_draft(
        arguments["draft"], prepare_draft_sources([text], originals=[text])
    )
    assert outcome.accepted, outcome.issues
    schema = outcome.schema
    assert set(schema["properties"]) == {"batch", "assets", "total"}
    assert schema["required"] == ["batch", "assets", "total"]
    entity = schema["properties"]["assets"]["items"]
    assert set(entity["properties"]) == {"asset", "result", "checks", "tail"}
    assert entity["required"] == ["asset"]
    check = entity["properties"]["checks"]["items"]
    assert set(check["properties"]) == {"check", "outcome"}
    assert check["required"] == ["check", "outcome"]
    assert all(
        node["additionalProperties"] is False for node in (schema, entity, check)
    )
    records = [
        {
            "batch": "winter",
            "assets": [
                {
                    "asset": "Cedar",
                    "result": "ready",
                    "checks": [
                        {"check": "voltage", "outcome": "!pending"},
                        {"check": "status", "outcome": ""},
                    ],
                    "tail": "one",
                },
                {"asset": "Birch"},
                {
                    "asset": "Elm",
                    "result": "!hold",
                    "checks": [{"check": "route", "outcome": "pass"}],
                    "tail": "three",
                },
                {"asset": "Aspen", "result": "", "tail": "four"},
            ],
            "total": 4,
        },
        {
            "batch": "summer",
            "assets": [{"asset": "Spruce", "result": "ready"}],
            "total": 1,
        },
    ]
    assert validate_records_against_schema(records, schema) == []
    misplaced = deepcopy(records[:1])
    misplaced[0]["checks"] = misplaced[0]["assets"][0].pop("checks")
    assert validate_records_against_schema(misplaced, schema)
    missing_summary = deepcopy(records[:1])
    del missing_summary[0]["total"]
    assert validate_records_against_schema(missing_summary, schema)
    assert "submit_result_schema" not in SCHEMA_DRAFT_SYSTEM_PROMPT
    assert "result_schema" not in SCHEMA_DRAFT_SYSTEM_PROMPT
    assert "SchemaPlan" not in SCHEMA_DRAFT_SYSTEM_PROMPT


async def test_actual_draft_requests_freeze_once_and_isolate_ttp(transport):
    requests = []
    observed = []

    def respond(request):
        payload = json.loads(request.content)
        requests.append(payload)
        names = [t["function"]["name"] for t in payload["tools"]]
        text = json.dumps(payload["messages"], ensure_ascii=False)
        if names == ["submit_schema_draft"]:
            assert len(requests) == 1
            assert "i0f0l1" in text and "i1f0l1" in text
            assert "submit_result_schema" not in text
            return httpx.Response(200, json=completion(names[0], {"draft": draft()}, 1))
        assert names == [
            "submit_ttp_template",
            "test_ttp_template",
            "finish_generation",
        ]
        assert "submit_schema_draft" not in text
        assert "draft_example" not in text
        assert "line_id" not in text
        assert "i0f0l1" not in text
        assert "winter" not in text
        if len(requests) == 2:
            assert "name" in text and "required" in text
            return httpx.Response(
                200,
                json=completion(
                    "submit_ttp_template",
                    {"ttp_template": "Name: {{ name | WORD }}"},
                    2,
                ),
            )
        return httpx.Response(200, json=completion("finish_generation", {}, 3))

    transport(respond)
    with schema_strategy_for_testing("draft"):
        result = await generator().generate(
            GenerationRequest(command_outputs=["Name: Atlas\n", "Name: Birch\n"]),
            observer=observed.append,
        )
    assert result.status == "success", result.issues
    assert result.artifact.records == [{"name": "Atlas"}, {"name": "Birch"}]
    assert result.metadata.prompt_version == DRAFT_PROMPT_VERSION
    assert result.metadata.schema_agent_rounds == 1
    assert len(requests) == 3
    tool = [
        e
        for e in observed
        if getattr(e, "name", None) == "cli_parser.tool.result"
        and e.value["tool_name"] == "submit_schema_draft"
    ][0]
    assert tool.metadata["sensitive"] is True
    assert tool.value["output"]["compiled_schema"] == result.artifact.result_schema
    assert tool.value["output"]["facts"]["property_count"] == 1


async def test_business_rejection_resets_protocol_sequence(transport):
    requests = []

    def respond(request):
        payload = json.loads(request.content)
        requests.append(payload)
        i = len(requests)
        if i in (1, 2, 3, 5, 6, 7):
            return httpx.Response(
                200, json=completion("submit_schema_draft", {"arguments": "invalid"}, i)
            )
        if i == 4:
            return httpx.Response(
                200,
                json=completion(
                    "submit_schema_draft",
                    {
                        "draft": {
                            "version": 1,
                            "fields": [
                                {
                                    "name": {"fallback": "x", "reason": "unlabeled"},
                                    "required": True,
                                    "node": {"type": "null"},
                                }
                            ],
                        }
                    },
                    i,
                ),
            )
        assert "schema_draft" in json.dumps(payload["messages"])
        return httpx.Response(
            200, json=completion("submit_schema_draft", {"draft": draft()}, i)
        )

    transport(respond)
    with schema_strategy_for_testing("draft"):
        result = await generator(max_agent_rounds=10).propose_schema(
            GenerationRequest(command_outputs=["Name: Atlas\n"])
        )
    assert result.status == "success", result.issues
    assert len(requests) == 8
    assert result.metadata.schema_submissions == 2


async def test_four_protocol_failures_stop_without_draft(transport):
    requests = []

    def respond(request):
        requests.append(json.loads(request.content))
        return httpx.Response(
            200,
            json=completion(
                "submit_schema_draft", {"arguments": "invalid"}, len(requests)
            ),
        )

    transport(respond)
    with schema_strategy_for_testing("draft"):
        result = await generator().propose_schema(
            GenerationRequest(command_outputs=["Name: Atlas\n"])
        )
    assert result.status == "failed" and result.proposal is None
    assert len(requests) == 4
    assert result.metadata.schema_submissions == 0
    assert result.metadata.termination_reason == "model_submission_tool_call_invalid"


def session(validator=None):
    text = "Name: Atlas\n"
    return GenerationSession(
        command_outputs=(text,),
        schema_validator=validator or (lambda _: ValidatorOutcome(valid=True)),
        template_validator=lambda _: None,
        schema_strategy="draft",
        schema_draft_sources=prepare_draft_sources([text], originals=[text]),
    )


async def test_freeze_is_permanent_and_not_alias_to_submission():
    state = session()
    candidate = draft()
    tool = SubmitSchemaDraftTool(state)
    first = await tool.call(draft=candidate)
    frozen = deepcopy(state.frozen_schema)
    candidate["fields"][0]["node"]["attributes"] = {"description": "PRIVATE"}
    await tool.call(draft={"version": 1, "fields": []})
    assert state.frozen_schema == frozen
    assert state.schema_submissions == 1
    assert "compiled_schema" not in str(first)


@pytest.mark.parametrize("failure", ["compiler", "validator", "cancelled"])
async def test_execution_error_is_sanitized_and_cancel_propagates(monkeypatch, failure):
    from cli_parser_agent.ttp_generation.agent import schema_draft_tools

    def broken(*args, **kwargs):
        raise RuntimeError("PRIVATE_ERROR_BODY")

    async def cancel(*args, **kwargs):
        raise asyncio.CancelledError()

    state = session()
    tool = SubmitSchemaDraftTool(state)
    if failure == "compiler":
        monkeypatch.setattr(schema_draft_tools, "compile_schema_draft", broken)
    else:
        state.schema_validator = cancel if failure == "cancelled" else broken
    if failure == "cancelled":
        with pytest.raises(asyncio.CancelledError):
            await tool.call(draft=draft())
    else:
        chunk = await tool.call(draft=draft())
        assert "PRIVATE_ERROR_BODY" not in str(chunk)
        assert "schema.validator_failed" in str(chunk)
    assert state.frozen_schema is None


async def test_compiled_schema_rejection_remains_unfrozen():
    state = session(
        lambda _: ValidatorOutcome(
            valid=False,
            issues=(
                {
                    "code": "synthetic.rejected",
                    "stage": "schema",
                    "message": "rejected",
                },
            ),
        )
    )
    chunk = await SubmitSchemaDraftTool(state).call(draft=draft())
    assert state.frozen_schema is None
    assert state.schema_submissions == 1
    assert "synthetic.rejected" in str(chunk)


async def test_external_schema_ignores_draft_strategy(transport):
    requests = []

    def respond(request):
        payload = json.loads(request.content)
        requests.append(payload)
        assert [x["function"]["name"] for x in payload["tools"]] == [
            "submit_ttp_template",
            "test_ttp_template",
            "finish_generation",
        ]
        assert "submit_schema_draft" not in str(payload["messages"])
        return httpx.Response(
            200,
            json=completion(
                "submit_ttp_template" if len(requests) == 1 else "finish_generation",
                {"ttp_template": "Name: {{ provided | WORD }}"}
                if len(requests) == 1
                else {},
                len(requests),
            ),
        )

    transport(respond)
    supplied = {
        "type": "object",
        "properties": {"provided": {"type": "string"}},
        "additionalProperties": False,
    }
    with schema_strategy_for_testing("draft"):
        result = await generator().generate_from_schema(
            TemplateRequest(command_outputs=["Name: Atlas\n"], result_schema=supplied)
        )
    assert result.status == "success", result.issues
    assert result.metadata.schema_agent_rounds == 0
    assert result.artifact.result_schema == supplied


async def test_strategy_is_request_local_and_default_stays_direct():
    async def check(arm):
        with schema_strategy_for_testing(arm):
            await asyncio.sleep(0)
            assert current_schema_strategy() == arm
            return current_prompt_version()

    assert await asyncio.gather(check("draft"), check("direct")) == [
        DRAFT_PROMPT_VERSION,
        PROMPT_VERSION,
    ]
    assert current_schema_strategy() == "direct"
    assert current_prompt_version() == PROMPT_VERSION


async def test_final_fitting_sources_equal_actual_wire_task(monkeypatch, transport):
    from cli_parser_agent.ttp_generation import workflow
    from cli_parser_agent.ttp_generation.agent import schema_draft_tools

    original = "".join(f"Item Label {i}: value_{i}\n" for i in range(400))
    captured_sessions = []
    estimated_tasks = []
    compiled_sources = []
    requests = []
    build = workflow.build_agent
    compile_draft = schema_draft_tools.compile_schema_draft

    def capture_agent(**kwargs):
        captured_sessions.append(kwargs["session"])
        return build(**kwargs)

    async def estimate(agent, message, phase):
        assert phase == "schema"
        assert captured_sessions[-1].schema_draft_sources == ()
        estimated_tasks.append(message.content[0].text)
        return 100_000 if len(estimated_tasks) == 1 else 1

    def capture_compile(raw, sources, **limits):
        compiled_sources.extend(sources)
        return compile_draft(raw, sources, **limits)

    def respond(request):
        payload = json.loads(request.content)
        requests.append(payload)
        content = payload["messages"][1]["content"]
        task = (
            content if isinstance(content, str) else "".join(x["text"] for x in content)
        )
        table = json.loads(
            re.search(
                r"<source_lines_json>(.*?)</source_lines_json>", task, re.S
            ).group(1)
        )
        assert table[0]["sampled"] is True
        # The final tail fragment changes its own line numbering as fitting shrinks.
        line = next(row for row in reversed(table[0]["lines"]) if row["complete"])
        candidate = draft()
        candidate["fields"][0]["name"] = {
            "source": [
                {"line_id": line["line_id"], "quote": line["text"].split(":", 1)[0]}
            ]
        }
        return httpx.Response(
            200, json=completion("submit_schema_draft", {"draft": candidate}, 1)
        )

    monkeypatch.setattr(workflow, "build_agent", capture_agent)
    monkeypatch.setattr(workflow, "estimate_initial_model_tokens", estimate)
    monkeypatch.setattr(schema_draft_tools, "compile_schema_draft", capture_compile)
    transport(respond)
    with schema_strategy_for_testing("draft"):
        result = await generator(model_input_char_budget=4000).propose_schema(
            GenerationRequest(command_outputs=[original])
        )
    assert result.status == "success", result.issues
    assert len(estimated_tasks) >= 2
    content = requests[0]["messages"][1]["content"]
    actual_task = (
        content if isinstance(content, str) else "".join(x["text"] for x in content)
    )
    assert actual_task == estimated_tasks[-1]
    assert actual_task != estimated_tasks[0]
    shown = json.loads(
        re.search(
            r"<source_lines_json>(.*?)</source_lines_json>", actual_task, re.S
        ).group(1)
    )[0]["lines"]
    assert [
        {"line_id": line.line_id, "complete": line.complete, "text": line.text}
        for line in compiled_sources
    ] == shown
    assert tuple(compiled_sources) == captured_sessions[0].schema_draft_sources
