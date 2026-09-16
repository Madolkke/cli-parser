"""Actual offline SDK/AgentScope requests for the private plan experiment."""

import asyncio
import json
from copy import deepcopy

import openai
import pytest
from openai.types.chat import ChatCompletion

from cli_parser_agent import (
    GenerationPolicy,
    GenerationRequest,
    TemplateRequest,
    TtpGenerator,
    TtpGeneratorSettings,
)
from cli_parser_agent.ttp_generation.agent.schema_strategy import (
    schema_strategy_for_testing,
)


def plan():
    return {
        "version": 1,
        "nodes": [
            {
                "id": "n1",
                "parent_id": "root",
                "kind": "value",
                "role": "name",
                "label_refs": [
                    {"input_index": 0, "fragment_index": 0, "start": 0, "end": 4}
                ],
                "evidence_complete": True,
                "occurrences": [
                    {
                        "parent_instance_id": "input_0",
                        "segments": [
                            {
                                "input_index": 0,
                                "fragment_index": 0,
                                "start": 6,
                                "end": 11,
                            }
                        ],
                    }
                ],
            }
        ],
    }


def completion(name, arguments, index):
    return ChatCompletion.model_validate(
        {
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
    )


@pytest.mark.parametrize("strategy", ["plan", "plan_confirm"])
async def test_actual_plan_request_freezes_and_isolates_ttp(monkeypatch, strategy):
    requests = []

    async def create(self, **kwargs):
        requests.append(deepcopy(kwargs))
        tool_names = [t["function"]["name"] for t in kwargs["tools"]]
        if "submit_schema_plan" in tool_names:
            assert "submit_result_schema" not in tool_names
            assert "<source_fragments_json>" in str(kwargs["messages"])
            if len(requests) == 1:
                return completion("submit_schema_plan", {"plan": plan()}, len(requests))
            assert strategy == "plan_confirm"
            assert '"frozen":false' in str(kwargs["messages"])
            return completion("confirm_schema_plan", {}, len(requests))
        text = json.dumps(kwargs["messages"], ensure_ascii=False)
        assert "SchemaPlan" not in text
        assert "source_fragments_json" not in text
        assert "fragment_index" not in text
        if "call-ttp" not in text and not any(
            m.get("role") == "tool" for m in kwargs["messages"]
        ):
            return completion(
                "submit_ttp_template",
                {"ttp_template": "Name: {{ name | WORD }}"},
                len(requests),
            )
        return completion("finish_generation", {}, len(requests))

    monkeypatch.setattr(
        openai.resources.chat.completions.AsyncCompletions, "create", create
    )
    generator = TtpGenerator(
        settings=TtpGeneratorSettings(
            api_key="offline", model_name="offline", stream=False
        ),
        policy=GenerationPolicy(total_timeout_seconds=900, max_agent_rounds=8),
    )
    with schema_strategy_for_testing(strategy):
        result = await generator.generate(
            GenerationRequest(command_outputs=["Name: Atlas\n"])
        )
    assert result.status == "success", result.issues
    assert result.artifact.records == [{"name": "Atlas"}]
    assert result.artifact.result_schema["required"] == ["name"]
    assert len(requests) == (4 if strategy == "plan_confirm" else 3)


async def test_invalid_replacement_clears_pending_plan():
    from cli_parser_agent.ttp_generation.agent.schema_plan_tools import (
        ConfirmSchemaPlanTool,
        SubmitSchemaPlanTool,
    )
    from cli_parser_agent.ttp_generation.agent.session import (
        GenerationSession,
        ValidatorOutcome,
    )
    from cli_parser_agent.ttp_generation.schema_plan import SourceFragment

    session = GenerationSession(
        command_outputs=("Name: Atlas\n",),
        schema_validator=lambda _: ValidatorOutcome(valid=True),
        template_validator=lambda _: None,
        schema_strategy="plan_confirm",
        schema_plan_sources=(SourceFragment(0, 0, "Name: Atlas\n"),),
    )
    submit = SubmitSchemaPlanTool(session)
    await submit.call(plan=plan())
    assert session.pending_schema_plan
    await submit.call(plan={"version": 1, "nodes": []})
    assert session.pending_schema_plan is None
    await ConfirmSchemaPlanTool(session).call()
    assert not session.schema_is_frozen


def test_fragment_offsets_do_not_include_sampling_gap():
    from cli_parser_agent.ttp_generation.agent.schema_plan_prompt import (
        build_schema_plan_task,
        plan_sources,
    )
    from cli_parser_agent.ttp_generation.sampling import TRUNCATION_MARKER

    text = "甲: A\n" + TRUNCATION_MARKER + "尾: Z\n"
    original = "甲: A\nmiddle omitted source\n尾: Z\n"
    sources = plan_sources([text], originals=[original])
    assert [f.text for f in sources] == ["甲: A\n", "尾: Z\n"]
    assert all(not f.complete for f in sources)
    assert TRUNCATION_MARKER not in build_schema_plan_task([text], originals=[original])


async def test_framework_rejected_replacement_cannot_confirm_stale_draft(monkeypatch):
    count = 0

    async def create(self, **kwargs):
        nonlocal count
        count += 1
        if count == 1:
            return completion("submit_schema_plan", {"plan": plan()}, count)
        if count == 2:
            return completion("submit_schema_plan", {"arguments": "bad"}, count)
        return completion("confirm_schema_plan", {}, count)

    monkeypatch.setattr(
        openai.resources.chat.completions.AsyncCompletions, "create", create
    )
    with schema_strategy_for_testing("plan_confirm"):
        result = await TtpGenerator(
            settings=TtpGeneratorSettings(api_key="offline", model_name="offline"),
            policy=GenerationPolicy(max_agent_rounds=4),
        ).propose_schema(GenerationRequest(command_outputs=["Name: Atlas\n"]))
    assert result.status == "failed"
    assert result.proposal is None
    assert count == 4


def tool_session():
    from cli_parser_agent.ttp_generation.agent.session import (
        GenerationSession,
        ValidatorOutcome,
    )
    from cli_parser_agent.ttp_generation.schema_plan import SourceFragment

    return GenerationSession(
        command_outputs=("Name: Atlas\n",),
        schema_validator=lambda _: ValidatorOutcome(valid=True),
        template_validator=lambda _: None,
        schema_strategy="plan_confirm",
        schema_plan_sources=(SourceFragment(0, 0, "Name: Atlas\n"),),
    )


@pytest.mark.parametrize("failure", ["compiler", "validator", "cancelled"])
async def test_plan_execution_failure_is_sanitized_and_cancellation_propagates(
    monkeypatch, failure
):
    from cli_parser_agent.ttp_generation.agent import schema_plan_tools

    session = tool_session()
    submit = schema_plan_tools.SubmitSchemaPlanTool(session)
    session.record_agent_round("schema")
    await submit.call(plan=plan())
    assert session.pending_schema_plan is not None

    def broken(*args):
        raise RuntimeError("PRIVATE error body")

    async def cancelled(*args):
        raise asyncio.CancelledError()

    if failure == "compiler":
        monkeypatch.setattr(schema_plan_tools, "compile_schema_plan", broken)
    else:
        session.schema_validator = cancelled if failure == "cancelled" else broken
    if failure == "cancelled":
        with pytest.raises(asyncio.CancelledError):
            await submit.call(plan=plan())
    else:
        result = await submit.call(plan=plan())
        assert "PRIVATE" not in repr(result)
        assert "schema_plan.validator_failed" in repr(result)
    assert session.pending_schema_plan is None
    assert session.pending_schema_plan_round is None
    assert session.frozen_schema is None


async def test_same_model_round_cannot_confirm_before_feedback(monkeypatch):
    count = 0

    async def create(self, **kwargs):
        nonlocal count
        count += 1
        if count == 1:
            response = completion("submit_schema_plan", {"plan": plan()}, 1)
            confirm = completion("confirm_schema_plan", {}, 2)
            response.choices[0].message.tool_calls.extend(
                confirm.choices[0].message.tool_calls
            )
            return response
        assert "schema_plan.confirmation_requires_review" in str(kwargs["messages"])
        return completion("confirm_schema_plan", {}, 3)

    monkeypatch.setattr(
        openai.resources.chat.completions.AsyncCompletions, "create", create
    )
    with schema_strategy_for_testing("plan_confirm"):
        result = await TtpGenerator(
            settings=TtpGeneratorSettings(api_key="offline", model_name="offline"),
            policy=GenerationPolicy(max_agent_rounds=4),
        ).propose_schema(GenerationRequest(command_outputs=["Name: Atlas\n"]))
    assert result.status == "success"
    assert count == 2
    assert result.metadata.schema_agent_rounds == 2
    assert result.metadata.schema_submissions == 1


async def test_pending_plan_at_round_limit_is_not_a_proposal(monkeypatch):
    async def create(self, **kwargs):
        return completion("submit_schema_plan", {"plan": plan()}, 1)

    monkeypatch.setattr(
        openai.resources.chat.completions.AsyncCompletions, "create", create
    )
    with schema_strategy_for_testing("plan_confirm"):
        result = await TtpGenerator(
            settings=TtpGeneratorSettings(api_key="offline", model_name="offline"),
            policy=GenerationPolicy(max_agent_rounds=1),
        ).propose_schema(GenerationRequest(command_outputs=["Name: Atlas\n"]))
    assert result.status == "failed"
    assert result.proposal is None
    assert result.metadata.termination_reason == "agent_round_limit"


@pytest.mark.parametrize("strategy", ["plan", "plan_confirm"])
async def test_injected_schema_keeps_template_only_protocol(monkeypatch, strategy):
    requests = []

    async def create(self, **kwargs):
        requests.append(kwargs)
        names = [t["function"]["name"] for t in kwargs["tools"]]
        assert names == [
            "submit_ttp_template",
            "test_ttp_template",
            "finish_generation",
        ]
        assert "source_fragments_json" not in str(kwargs["messages"])
        assert "submit_schema_plan" not in str(kwargs["messages"])
        return completion(
            "submit_ttp_template" if len(requests) == 1 else "finish_generation",
            {"ttp_template": "Name: {{ provided | WORD }}"}
            if len(requests) == 1
            else {},
            len(requests),
        )

    monkeypatch.setattr(
        openai.resources.chat.completions.AsyncCompletions, "create", create
    )
    supplied = {
        "type": "object",
        "properties": {"provided": {"type": "string"}},
        "additionalProperties": False,
    }
    with schema_strategy_for_testing(strategy):
        result = await TtpGenerator(
            settings=TtpGeneratorSettings(api_key="offline", model_name="offline"),
            policy=GenerationPolicy(max_agent_rounds=4),
        ).generate_from_schema(
            TemplateRequest(
                command_outputs=["Name: Atlas\n"],
                result_schema=supplied,
            )
        )
    assert result.status == "success", result.issues
    assert result.metadata.schema_agent_rounds == 0
    assert result.artifact.result_schema == supplied
    assert len(requests) == 2
