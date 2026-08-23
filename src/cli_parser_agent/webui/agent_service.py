"""Adapter from the public generator API to the WebUI service boundary."""

from __future__ import annotations

import re
from collections.abc import Sequence
from typing import Any

from agentscope.event import (
    AgentEvent,
    CustomEvent,
    ModelCallEndEvent,
    ModelCallStartEvent,
    TextBlockDeltaEvent,
    TextBlockEndEvent,
    TextBlockStartEvent,
    ThinkingBlockDeltaEvent,
    ThinkingBlockEndEvent,
    ThinkingBlockStartEvent,
    ToolCallDeltaEvent,
    ToolCallEndEvent,
    ToolCallStartEvent,
    ToolResultEndEvent,
    ToolResultStartEvent,
    ToolResultTextDeltaEvent,
)

from ..ttp_generation.contracts import GenerationRequest, TemplateRequest
from ..ttp_generation.generator import TtpGenerator
from ..ttp_generation.validation import validate_result_schema
from .contracts import RunMode, WebUIProgressEvent
from .service import ProgressObserver

_FORWARDED_EVENTS = frozenset(
    {
        "cli_parser.generation.started",
        "cli_parser.generation.completed",
        "cli_parser.generation.cancelled",
        "cli_parser.generation.exception",
        "cli_parser.phase.started",
        "cli_parser.phase.completed",
        "cli_parser.no_tool.retry",
        "cli_parser.round.skipped",
        "cli_parser.final_validation.started",
        "cli_parser.final_validation.completed",
        "cli_parser.tool.result",
    },
)

_MAX_EVENT_TEXT = 32_768
_MAX_EVENT_JSON = 131_072
_CREDENTIAL_PATTERNS = (
    re.compile(r"(?i)(bearer\s+)[A-Za-z0-9._~+/=-]+"),
    re.compile(r"\bsk-[A-Za-z0-9_-]{16,}\b"),
    re.compile(r"(?i)(api[_-]?key\s*[:=]\s*)[^,\s]+"),
)


def _redact_text(value: str, *, limit: int = _MAX_EVENT_TEXT) -> str:
    result = value
    for pattern in _CREDENTIAL_PATTERNS:
        result = pattern.sub(
            lambda match: match.group(1) + "[redacted]"
            if match.lastindex
            else "[redacted]",
            result,
        )
    if len(result) <= limit:
        return result
    return result[:limit] + "… [truncated]"


def _safe_value(value: Any, *, budget: int = _MAX_EVENT_JSON) -> Any:
    if isinstance(value, str):
        return _redact_text(value, limit=min(_MAX_EVENT_TEXT, budget))
    if isinstance(value, dict):
        output: dict[str, Any] = {}
        remaining = budget
        for key, item in value.items():
            if remaining <= 0:
                break
            safe_item = _safe_value(item, budget=remaining)
            output[str(key)] = safe_item
            remaining -= len(str(safe_item))
        if remaining <= 0:
            output["_truncated"] = True
        return output
    if isinstance(value, (list, tuple)):
        output = []
        remaining = budget
        for item in value:
            if remaining <= 0:
                break
            safe_item = _safe_value(item, budget=remaining)
            output.append(safe_item)
            remaining -= len(str(safe_item))
        return output
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return _redact_text(str(value), limit=min(_MAX_EVENT_TEXT, budget))


def _common(event: AgentEvent) -> WebUIProgressEvent:
    metadata = dict(getattr(event, "metadata", None) or {})
    common: WebUIProgressEvent = {
        "phase": metadata.get("phase"),
        "elapsed_seconds": round(float(metadata.get("elapsed_seconds") or 0.0), 3),
        "sequence": metadata.get("sequence"),
    }
    if metadata.get("round_index") is not None:
        common["round_index"] = metadata["round_index"]
    return common


def _block_event(
    event: AgentEvent,
    *,
    start_type: str,
    delta_type: str,
    end_type: str,
    block_id: str,
    detail: dict[str, Any] | None = None,
) -> WebUIProgressEvent:
    common = _common(event)
    common["block_id"] = block_id
    if isinstance(
        event,
        (
            ThinkingBlockStartEvent,
            TextBlockStartEvent,
            ToolCallStartEvent,
            ToolResultStartEvent,
        ),
    ):
        common["type"] = start_type
    elif isinstance(
        event,
        (
            ThinkingBlockDeltaEvent,
            TextBlockDeltaEvent,
            ToolCallDeltaEvent,
            ToolResultTextDeltaEvent,
        ),
    ):
        common["type"] = delta_type
    else:
        common["type"] = end_type
    if detail:
        common["detail"] = detail
    return common


def project_agent_event(event: AgentEvent) -> WebUIProgressEvent | None:
    """Project one internal AgentScope event into a safe local WebUI event."""

    common = _common(event)
    if isinstance(event, ModelCallStartEvent):
        return {
            **common,
            "type": "agent.model_call_started",
            "detail": {"reply_id": event.reply_id, "model_name": event.model_name},
        }
    if isinstance(event, ModelCallEndEvent):
        return {
            **common,
            "type": "agent.model_call_completed",
            "detail": {
                "reply_id": event.reply_id,
                "input_tokens": event.input_tokens,
                "output_tokens": event.output_tokens,
                "finished_reason": str(event.finished_reason),
            },
        }
    if isinstance(
        event,
        ThinkingBlockStartEvent | ThinkingBlockDeltaEvent | ThinkingBlockEndEvent,
    ):
        detail = (
            {"text": _redact_text(event.delta)}
            if isinstance(event, ThinkingBlockDeltaEvent)
            else None
        )
        return _block_event(
            event,
            start_type="agent.thinking_started",
            delta_type="agent.thinking_delta",
            end_type="agent.thinking_completed",
            block_id=event.block_id,
            detail=detail,
        )
    if isinstance(event, TextBlockStartEvent | TextBlockDeltaEvent | TextBlockEndEvent):
        detail = (
            {"text": _redact_text(event.delta)}
            if isinstance(event, TextBlockDeltaEvent)
            else None
        )
        return _block_event(
            event,
            start_type="agent.text_started",
            delta_type="agent.text_delta",
            end_type="agent.text_completed",
            block_id=event.block_id,
            detail=detail,
        )
    if isinstance(event, ToolCallStartEvent | ToolCallDeltaEvent | ToolCallEndEvent):
        detail = None
        if isinstance(event, ToolCallStartEvent):
            detail = {
                "tool_name": event.tool_call_name,
                "tool_call_id": event.tool_call_id,
            }
        elif isinstance(event, ToolCallDeltaEvent):
            detail = {"text": _redact_text(event.delta)}
        return _block_event(
            event,
            start_type="agent.tool_call_started",
            delta_type="agent.tool_call_delta",
            end_type="agent.tool_call_completed",
            block_id=event.tool_call_id,
            detail=detail,
        ) | {"tool_call_id": event.tool_call_id}
    if isinstance(
        event,
        ToolResultStartEvent | ToolResultTextDeltaEvent | ToolResultEndEvent,
    ):
        detail = None
        if isinstance(event, ToolResultStartEvent):
            detail = {
                "tool_name": event.tool_call_name,
                "tool_call_id": event.tool_call_id,
            }
        elif isinstance(event, ToolResultTextDeltaEvent):
            detail = {"text": _redact_text(event.delta)}
        elif isinstance(event, ToolResultEndEvent):
            detail = {"state": str(event.state)}
        return _block_event(
            event,
            start_type="agent.tool_result_started",
            delta_type="agent.tool_result_delta",
            end_type="agent.tool_result_completed",
            block_id=event.tool_call_id,
            detail=detail,
        ) | {"tool_call_id": event.tool_call_id}
    if not isinstance(event, CustomEvent) or event.name not in _FORWARDED_EVENTS:
        return None
    value = event.value if isinstance(event.value, dict) else {}
    detail = _safe_value(value, budget=_MAX_EVENT_JSON)
    return {**common, "type": event.name, "detail": detail}


class AgentGenerationService:
    """Keep all main-agent imports and event translation behind one adapter."""

    def __init__(self, generator: Any | None = None) -> None:
        self.generator = generator if generator is not None else TtpGenerator.from_env()

    @staticmethod
    def validate_inputs(command_outputs: Sequence[str]) -> list[str]:
        request = GenerationRequest(command_outputs=list(command_outputs))
        return list(request.command_outputs)

    async def run(
        self,
        mode: RunMode,
        command_outputs: Sequence[str],
        *,
        observer: ProgressObserver | None = None,
    ) -> dict[str, Any]:
        request = GenerationRequest(command_outputs=list(command_outputs))
        callback = self._observer(observer)
        if mode == "propose":
            result = await self.generator.propose_schema(request, observer=callback)
        else:
            result = await self.generator.generate(request, observer=callback)
        return result.model_dump(mode="json")

    async def run_from_schema(
        self,
        command_outputs: Sequence[str],
        result_schema: dict[str, Any],
        *,
        observer: ProgressObserver | None = None,
    ) -> dict[str, Any]:
        request = TemplateRequest(
            command_outputs=list(command_outputs),
            result_schema=result_schema,
        )
        result = await self.generator.generate_from_schema(
            request,
            observer=self._observer(observer),
        )
        return result.model_dump(mode="json")

    def validate_schema(self, result_schema: dict[str, Any]) -> list[dict[str, Any]]:
        issues = validate_result_schema(result_schema)
        return [issue.model_dump(mode="json") for issue in issues]

    @staticmethod
    def _observer(observer: ProgressObserver | None) -> Any:
        if observer is None:
            return None

        def callback(event: AgentEvent) -> None:
            projected = project_agent_event(event)
            if projected is not None:
                observer(projected)

        return callback


__all__ = ["AgentGenerationService", "project_agent_event"]
