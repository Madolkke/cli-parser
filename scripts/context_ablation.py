"""Opt-in, process-local context experiments around the standard eval runner."""

from __future__ import annotations

import argparse
import asyncio
import importlib
import json
import sys
from collections.abc import Iterator, Sequence
from contextlib import contextmanager, suppress
from copy import deepcopy
from dataclasses import dataclass, field
from importlib.metadata import version
from pathlib import Path
from typing import Any
from unittest.mock import patch
from uuid import UUID

import openai
from agentscope.agent import ContextConfig
from agentscope.message import (
    Msg,
    TextBlock,
    ThinkingBlock,
    ToolCallBlock,
    ToolResultBlock,
)
from openai.types.chat import ChatCompletion

from cli_parser_agent import GenerationPolicy, TtpGeneratorSettings, observability
from cli_parser_agent.ttp_generation.agent import builder, runner
from cli_parser_agent.ttp_generation.agent.runner import run_generation_phase
from cli_parser_agent.ttp_generation.agent.session import (
    GenerationSession,
    ValidatorOutcome,
)
from cli_parser_agent.ttp_generation.validation import TtpParseResult

VARIANTS = ("current", "estimator", "retention", "combined")
_ACTIVE = False
_MAX_ROWS = 256


def _updated_candidate_index(result: ToolResultBlock) -> int | None:
    if isinstance(result.output, str):
        text = result.output
    elif (
        isinstance(result.output, list)
        and result.output
        and isinstance(result.output[0], TextBlock)
    ):
        text = result.output[0].text
    else:
        return None
    prefix = "<validation_feedback>\n"
    end = text.find("\n</validation_feedback>", len(prefix), 9 * 1024)
    if not text.startswith(prefix) or end < 0:
        return None
    try:
        feedback = json.loads(text[len(prefix) : end])
    except ValueError:
        return None
    if not isinstance(feedback, dict) or not all(
        (
            feedback.get("feedback_version") == 1,
            feedback.get("scope") == "full_input_validation",
            feedback.get("accepted") is True,
            feedback.get("candidate_updated") is True,
        )
    ):
        return None
    index = feedback.get("retained_candidate_submission_index")
    return index if type(index) is int and index > 0 else None


def _compact_retained_history(
    agent: Any, session: GenerationSession, *, progress: Any = None
) -> None:
    """Fold old submissions except the newest result and retained valid candidate."""

    if session.ttp_submissions < 2 and session.validated_ttp_submission_index is None:
        return
    calls: dict[str, ToolCallBlock] = {}
    results: dict[str, tuple[int, ToolResultBlock]] = {}
    for index, message in enumerate(agent.state.context):
        for block in message.get_content_blocks():
            if not isinstance(
                block, ToolCallBlock | ToolResultBlock
            ) or block.name not in {"submit_ttp_template", "test_ttp_template"}:
                continue
            target = calls if isinstance(block, ToolCallBlock) else results
            if block.id in target:
                session.ttp_history_compaction_skips += 1
                return
            if isinstance(block, ToolCallBlock):
                calls[block.id] = block
            else:
                results[block.id] = index, block
    if set(calls) != set(results) or any(
        call.name != results[call_id][1].name for call_id, call in calls.items()
    ):
        session.ttp_history_compaction_skips += 1
        return
    submissions = [
        call_id for call_id, call in calls.items() if call.name == "submit_ttp_template"
    ]
    retained = session.validated_ttp_submission_index
    protected = set(submissions[-1:])
    if retained is not None:
        candidate_calls = [
            call_id
            for call_id in submissions
            if _updated_candidate_index(results[call_id][1]) == retained
        ]
        if len(candidate_calls) != 1:
            session.ttp_history_compaction_skips += 1
            _fail_context_integrity(agent)
        protected.add(candidate_calls[0])
    replacements: dict[str, ToolResultBlock] = {}
    removed_chars = 0
    for call_id in submissions:
        if call_id in protected:
            continue
        result = results[call_id][1]
        if isinstance(result.output, str):
            text = result.output
            replacement = runner._SUPERSEDED_TTP_RESULT
        elif isinstance(result.output, list) and all(
            isinstance(block, TextBlock) for block in result.output
        ):
            text = "".join(block.text for block in result.output)
            replacement = [TextBlock(text=runner._SUPERSEDED_TTP_RESULT)]
        else:
            session.ttp_history_compaction_skips += 1
            return
        if text != runner._SUPERSEDED_TTP_RESULT:
            replacements[call_id] = result.model_copy(update={"output": replacement})
            removed_chars += len(text)
    if not replacements:
        return
    for index, message in enumerate(agent.state.context):
        agent.state.context[index] = message.model_copy(
            update={
                "content": [
                    replacements.get(block.id, block)
                    if isinstance(block, ToolResultBlock)
                    else block
                    for block in message.get_content_blocks()
                ]
            }
        )
    session.ttp_history_compaction_events += 1
    session.ttp_history_compacted_interactions += len(replacements)
    session.ttp_history_compacted_result_chars += removed_chars
    if progress is not None:
        progress.custom(
            "cli_parser.ttp.history_compacted",
            {
                "compacted_interactions": len(replacements),
                "retained_interactions": len(protected)
                + sum(call.name == "test_ttp_template" for call in calls.values()),
                "compacted_input_chars": 0,
                "compacted_result_chars": removed_chars,
            },
            phase="ttp",
            sensitive=False,
        )


class ContextAblationBudgetExceeded(RuntimeError):
    """The retained request exceeds the experimental estimated input budget."""


class ContextAblationIntegrityError(RuntimeError):
    """A protected task or a tool call/result pair changed unexpectedly."""


def _fail_context_integrity(agent: Any) -> None:
    agent._ablation_row["stop_category"] = "context_integrity_failed"
    raise ContextAblationIntegrityError("context integrity failed")


def _without_thinking(messages: list[Msg]) -> list[Msg]:
    return [
        message.model_copy(
            update={
                "content": [
                    block
                    for block in message.get_content_blocks()
                    if not isinstance(block, ThinkingBlock)
                ],
            },
        )
        for message in messages
    ]


def _pair_facts(messages: list[Msg]) -> dict[str, int | bool]:
    calls: dict[str, str] = {}
    results: dict[str, str] = {}
    duplicates = 0
    ordered = True
    for message in messages:
        for block in message.get_content_blocks():
            if isinstance(block, ToolResultBlock) and calls.get(block.id) != block.name:
                ordered = False
            target = (
                calls
                if isinstance(block, ToolCallBlock)
                else results
                if isinstance(block, ToolResultBlock)
                else None
            )
            if target is not None:
                duplicates += int(block.id in target)
                target[block.id] = block.name
    return {
        "tool_calls": len(calls),
        "tool_results": len(results),
        "tool_pair_order_valid": ordered,
        "tool_pairs_complete": calls == results and duplicates == 0 and ordered,
    }


def _wire_facts(
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]] | None,
    task: str | None,
) -> dict[str, Any]:
    body = json.dumps(
        {"messages": messages, "tools": tools},
        ensure_ascii=False,
        separators=(",", ":"),
    )
    size = len(body.encode("utf-8"))
    intact = any(
        message.get("role") == "user"
        and (
            message.get("content") == task
            or isinstance(message.get("content"), list)
            and "".join(
                block.get("text", "")
                for block in message["content"]
                if block.get("type") == "text"
            )
            == task
        )
        for message in messages
    )
    return {
        "message_count": len(messages),
        "wire_utf8_bytes": size,
        "wire_estimated_tokens": (size + 3) // 4,
        "task_intact": intact if task is not None else None,
        "reasoning_content_sent": any("reasoning_content" in m for m in messages),
        "role_counts": {
            role: sum(message.get("role") == role for message in messages)
            for role in ("system", "developer", "user", "assistant", "tool")
        },
        "role_estimated_tokens": {
            role: (
                len(
                    json.dumps(
                        [
                            message
                            for message in messages
                            if message.get("role") == role
                        ],
                        ensure_ascii=False,
                        separators=(",", ":"),
                    ).encode("utf-8")
                )
                + 3
            )
            // 4
            for role in ("system", "developer", "user", "assistant", "tool")
        },
    }


@dataclass
class AblationReport:
    """Only counts, controlled categories and in-memory equality results persist."""

    variant: str
    agents: list[dict[str, Any]] = field(default_factory=list)

    def document(self) -> dict[str, Any]:
        return {
            "ablation_version": 1,
            "variant": self.variant,
            "agentscope_version": version("agentscope"),
            "estimator": "utf8_quarter_approximation_not_provider_tokenizer",
            "agents": deepcopy(self.agents),
        }


def _append(row: dict[str, Any], kind: str, value: dict[str, Any]) -> None:
    if len(row[kind]) < _MAX_ROWS:
        row[kind].append(value)
    else:
        row["observations_omitted"] += 1


@contextmanager
def context_ablation(variant: str) -> Iterator[AblationReport]:
    """Patch only newly built agents until the standard runner finishes.

    One variant per process; concurrent trials within that run are supported.
    The subclasses intentionally adapt the locked AgentScope private API.
    """

    global _ACTIVE
    if variant not in VARIANTS:
        raise ValueError("unknown context ablation variant")
    if _ACTIVE:
        raise RuntimeError("nested context experiments are not supported")
    agent_base = builder.Agent
    model_base = builder.ObservedOpenAIChatModel
    if not version("agentscope").startswith("2.0.4") or not all(
        hasattr(agent_base, name)
        for name in (
            "compress_context",
            "_prepare_model_input",
            "_split_tool_result_for_compression",
        )
    ):
        raise RuntimeError("unsupported AgentScope context experiment adapter")
    report = AblationReport(variant)
    retain = variant in {"retention", "combined"}
    corrected = variant in {"estimator", "combined"}

    class ExperimentModel(model_base):
        async def count_tokens(
            self, messages: list[Msg], tools: list[dict] | None
        ) -> int:
            return await super().count_tokens(
                _without_thinking(messages) if corrected else messages,
                tools,
            )

        async def _call_api(
            self,
            model_name: str,
            messages: list[Msg],
            tools: list[dict] | None = None,
            tool_choice: Any = None,
            **kwargs: Any,
        ) -> Any:
            formatted = await self.formatter.format(messages)
            facts = _wire_facts(formatted, tools, self._ablation_task)
            facts["forced_tool_choice"] = tool_choice is not None
            _append(self._ablation_row, "requests", facts)
            try:
                return await super()._call_api(
                    model_name, messages, tools, tool_choice, **kwargs
                )
            except openai.BadRequestError as error:
                if getattr(error, "code", None) == "context_length_exceeded":
                    self._ablation_row["stop_category"] = "provider_context_rejected"
                raise

    class ExperimentAgent(agent_base):
        def __init__(self, **kwargs: Any) -> None:
            super().__init__(**kwargs)
            recorder = self.model._attempt_recorder
            self._ablation_session = recorder.session
            self._ablation_phase = recorder.phase
            self._ablation_task: str | None = None
            self._ablation_row = {
                "agent_index": len(report.agents),
                "phase": recorder.phase,
                "request_id": None,
                "trace_id": None,
                "checks": [],
                "requests": [],
                "observations_omitted": 0,
                "compression_triggers": 0,
                "compressions_completed": 0,
                "stop_category": None,
            }
            if recorder.progress is not None:
                with suppress(TypeError, ValueError, AttributeError):
                    self._ablation_row["request_id"] = str(
                        UUID(recorder.progress.request_id)
                    )
            with suppress(TypeError, ValueError, AttributeError):
                self._ablation_row["trace_id"] = str(
                    UUID(observability.current_laminar_trace_id())
                )
            report.agents.append(self._ablation_row)
            self.model._ablation_row = self._ablation_row
            self.model._ablation_task = None

        async def compress_context(self, *args: Any, **kwargs: Any) -> None:
            if self._ablation_task is None:
                self._ablation_task = next(
                    (
                        message.get_text_content()
                        for message in self.state.context
                        if message.role == "user"
                    ),
                    None,
                )
                self.model._ablation_task = self._ablation_task
            if retain and not _pair_facts(self.state.context)["tool_pairs_complete"]:
                _fail_context_integrity(self)
            if retain and self._ablation_phase == "ttp":
                runner._compact_ttp_history(self, self._ablation_session)
            model_input = await self._prepare_model_input()
            estimated = await self.model.count_tokens(**model_input)
            before = {
                **_pair_facts(model_input["messages"]),
                **_wire_facts(
                    await self.model.formatter.format(model_input["messages"]),
                    model_input["tools"],
                    self._ablation_task,
                ),
            }
            triggered = (
                estimated >= self.context_config.trigger_ratio * self.model.context_size
            )
            if triggered:
                self._ablation_row["compression_triggers"] += 1
            completed = False
            if not retain:
                summary_before = self.state.summary
                context_before = self.state.context
                await super().compress_context(*args, **kwargs)
                completed = triggered and (
                    self.state.summary != summary_before
                    or self.state.context is not context_before
                )
                if completed:
                    self._ablation_row["compressions_completed"] += 1
                model_input = await self._prepare_model_input()
            formatted = await self.model.formatter.format(model_input["messages"])
            facts = {
                "estimated_tokens_before": estimated,
                "compression_triggered": triggered,
                "compression_suppressed": retain and triggered,
                "compression_completed": completed,
                "before": before,
                "summary_present": bool(self.state.summary),
                **_pair_facts(model_input["messages"]),
                **_wire_facts(formatted, model_input["tools"], self._ablation_task),
            }
            _append(self._ablation_row, "checks", facts)
            if retain:
                if (
                    not facts["task_intact"]
                    or not facts["tool_pairs_complete"]
                    or facts["summary_present"]
                ):
                    _fail_context_integrity(self)
                reserve = self.model.parameters.max_tokens or 0
                if facts["wire_estimated_tokens"] + reserve > self.model.context_size:
                    self._ablation_row["stop_category"] = "context_budget_exceeded"
                    raise ContextAblationBudgetExceeded("context budget exceeded")

        async def _split_tool_result_for_compression(
            self, tool_result: ToolResultBlock
        ) -> tuple[ToolResultBlock, ToolResultBlock | None]:
            if retain:
                return tool_result, None
            return await super()._split_tool_result_for_compression(tool_result)

    _ACTIVE = True
    try:
        with (
            patch.object(builder, "Agent", ExperimentAgent),
            patch.object(builder, "ObservedOpenAIChatModel", ExperimentModel),
            patch.object(
                runner,
                "_compact_ttp_history",
                _compact_retained_history if retain else runner._compact_ttp_history,
            ),
        ):
            yield report
    finally:
        _ACTIVE = False


def _synthetic_schema() -> dict[str, Any]:
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "type": "object",
        "properties": {"value": {"type": "string"}},
        "required": ["value"],
        "additionalProperties": False,
    }


async def offline_variant(
    variant: str, *, parameter_rejection: bool = False
) -> dict[str, Any]:
    """Exercise the real AgentScope/OpenAI formatter with a synthetic transport."""

    stage_calls = 0
    summary_calls = 0
    transport_checks: list[dict[str, Any]] = []
    task = builder.build_ttp_task_message(
        ["value: sample"], _synthetic_schema()
    ).get_text_content()
    parsed_value = "sample" + "y" * 40_000

    async def completion(_client: Any, **kwargs: Any) -> ChatCompletion:
        nonlocal stage_calls, summary_calls
        tools = kwargs.get("tools", [])
        is_summary = any(
            tool["function"]["name"] == "generate_structured_output" for tool in tools
        )
        transport_checks.append(
            {
                **_wire_facts(kwargs["messages"], tools, task),
                "forced_tool_choice": "tool_choice" in kwargs,
                "summary_request": is_summary,
                "large_result_present": any(
                    parsed_value in json.dumps(message.get("content"))
                    for message in kwargs["messages"]
                    if message.get("role") == "tool"
                ),
                "tool_result_count": sum(
                    message.get("role") == "tool" for message in kwargs["messages"]
                ),
                "full_submission_result_count": sum(
                    message.get("role") == "tool"
                    and "full_input_validation" in str(message.get("content"))
                    for message in kwargs["messages"]
                ),
            },
        )
        if is_summary:
            summary_calls += 1
            name = "generate_structured_output"
            arguments = {
                key: "synthetic summary"
                for key in (
                    "task_overview",
                    "current_state",
                    "important_discoveries",
                    "next_steps",
                    "context_to_preserve",
                )
            }
        else:
            stage_calls += 1
            steps = [
                ("submit_ttp_template", {"ttp_template": "value: {{ value }}"}),
                (
                    "test_ttp_template",
                    {"text": "value: probe", "ttp_template": "value: {{ value }}"},
                ),
                ("submit_ttp_template", {"ttp_template": "other: {{ value }}"}),
                ("finish_generation", {}),
            ]
            if parameter_rejection:
                steps.insert(0, ("submit_ttp_template", {"ttp_template": ""}))
            name, arguments = steps[stage_calls - 1]
        return ChatCompletion.model_validate(
            {
                "id": "synthetic-completion",
                "object": "chat.completion",
                "created": 0,
                "model": "synthetic-model",
                "choices": [
                    {
                        "index": 0,
                        "finish_reason": "tool_calls",
                        "message": {
                            "role": "assistant",
                            "content": None,
                            "reasoning_content": "x" * 240_000
                            if stage_calls == 1 and not is_summary
                            else None,
                            "tool_calls": [
                                {
                                    "id": f"call-{stage_calls}-{summary_calls}",
                                    "type": "function",
                                    "function": {
                                        "name": name,
                                        "arguments": json.dumps(arguments),
                                    },
                                },
                            ],
                        },
                    },
                ],
                "usage": {
                    "prompt_tokens": 10,
                    "completion_tokens": 10,
                    "total_tokens": 20,
                },
            },
        )

    session = GenerationSession(
        command_outputs=("value: sample",),
        schema_validator=lambda _: ValidatorOutcome(valid=True),
        template_validator=lambda candidate: ValidatorOutcome(
            valid=candidate.ttp_template == "value: {{ value }}",
            records=({"value": parsed_value},),
        ),
        ttp_test_validator=lambda _: TtpParseResult(
            result=[{"value": "probe"}], issues=[]
        ),
    )
    session.frozen_schema = _synthetic_schema()
    with (
        context_ablation(variant) as report,
        patch("openai.resources.chat.completions.AsyncCompletions.create", completion),
    ):
        # Patch the SDK boundary rather than replacing the AgentScope model.
        agent = builder.build_agent(
            settings=TtpGeneratorSettings(
                api_key="offline-only",
                model_name="synthetic-model",
                context_size=48_000,
                max_tokens=512,
                model_max_retries=0,
            ),
            policy=GenerationPolicy(),
            session=session,
            phase="ttp",
        )
        agent.context_config = ContextConfig(reserve_ratio=0.01)
        outcome = await run_generation_phase(
            agent,
            builder.build_ttp_task_message(
                session.command_outputs, session.frozen_schema
            ),
            session,
            "ttp",
        )
    return {
        **report.document(),
        "synthetic_only": True,
        "stage_calls": stage_calls,
        "summary_calls": summary_calls,
        "phase_completed": outcome.phase_completed,
        "candidate_retained": session.has_validated_ttp_candidate,
        "tests_used": session.ttp_test_calls,
        "retained_candidate_submission_index": session.validated_ttp_submission_index,
        "compacted_interactions": session.ttp_history_compacted_interactions,
        "transport_checks": transport_checks,
    }


def _write(path: Path, document: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(document, indent=2) + "\n", encoding="utf-8")


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    offline = subparsers.add_parser(
        "offline", help="run four synthetic variants offline"
    )
    offline.add_argument("--output", type=Path, required=True)
    run = subparsers.add_parser("run", help="forward to the standard evaluation entry")
    run.add_argument("--variant", choices=VARIANTS, required=True)
    run.add_argument("--diagnostics", type=Path, required=True)
    run.add_argument("runner_args", nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)
    if args.command == "offline":
        results = [asyncio.run(offline_variant(variant)) for variant in VARIANTS]
        _write(
            args.output,
            {"ablation_version": 1, "synthetic_only": True, "runs": results},
        )
        return 0
    runner_args = args.runner_args
    if runner_args[:1] == ["--"]:
        runner_args = runner_args[1:]
    if not runner_args:
        parser.error("standard runner arguments are required after --")
    script_directory = str(Path(__file__).resolve().parent)
    if script_directory not in sys.path:
        sys.path.insert(0, script_directory)
    standard_runner = importlib.import_module("run_test_sets")
    with context_ablation(args.variant) as report:
        try:
            return standard_runner.main(runner_args)
        finally:
            _write(args.diagnostics, report.document())


if __name__ == "__main__":
    raise SystemExit(main())
