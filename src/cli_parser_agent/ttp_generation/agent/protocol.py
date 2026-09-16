"""Request-local tool boundary facts and bounded protocol repair guidance.

The boundary is recorded by product code, never inferred from error text or
model-controlled tool output. ContextVar values follow AgentScope's execution
tasks while the mutable tracker remains private to one generation phase.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Literal

ProtocolFailure = Literal[
    "no_tool", "wrong_tool", "framework_arguments", "product_arguments"
]
BoundaryState = Literal["entered", "valid", "rejected", "execution_failed"]

MAX_CONSECUTIVE_PROTOCOL_FAILURES = 4
SCHEMA_PROTOCOL_TOOLS = (
    "submit_result_schema",
    "submit_schema_draft",
    "submit_schema_plan",
    "confirm_schema_plan",
)
TTP_PROTOCOL_TOOLS = (
    "submit_ttp_template",
    "test_ttp_template",
    "finish_generation",
)


@dataclass(slots=True)
class ProtocolBoundaryTracker:
    """Only controlled names and states; never tool arguments or values."""

    boundaries: dict[str, BoundaryState] = field(default_factory=dict)

    def reset(self) -> None:
        self.boundaries.clear()


_ACTIVE_TRACKER: ContextVar[ProtocolBoundaryTracker | None] = ContextVar(
    "generation_protocol_boundary", default=None
)


@contextmanager
def track_tool_boundaries(tracker: ProtocolBoundaryTracker) -> Iterator[None]:
    token = _ACTIVE_TRACKER.set(tracker)
    try:
        yield
    finally:
        _ACTIVE_TRACKER.reset(token)


def _mark(tool_name: str, state: BoundaryState) -> None:
    tracker = _ACTIVE_TRACKER.get()
    if tracker is not None and tool_name in SCHEMA_PROTOCOL_TOOLS + TTP_PROTOCOL_TOOLS:
        tracker.boundaries[tool_name] = state


def mark_tool_entered(tool_name: str) -> None:
    _mark(tool_name, "entered")


def mark_tool_arguments_valid(tool_name: str) -> None:
    _mark(tool_name, "valid")


def mark_tool_arguments_rejected(tool_name: str) -> None:
    _mark(tool_name, "rejected")


def mark_tool_execution_failed(tool_name: str) -> None:
    """Record execution failure after arguments have passed validation."""

    _mark(tool_name, "execution_failed")


def protocol_repair_text(
    failure: ProtocolFailure,
    expected_tools: tuple[str, ...],
) -> str:
    """Fixed examples describe only envelopes, never fabricate candidate data."""

    examples: dict[str, tuple[list[str], str]] = {
        "submit_result_schema": (
            ["result_schema"],
            '{"result_schema": {…完整 Schema…}}',
        ),
        "submit_schema_draft": (["draft"], '{"draft": {…完整 SchemaDraft…}}'),
        "submit_schema_plan": (["plan"], '{"plan": {…完整 SchemaPlan…}}'),
        "confirm_schema_plan": ([], "{}"),
        "submit_ttp_template": (["ttp_template"], '{"ttp_template": "…完整模板…"}'),
        "test_ttp_template": (
            ["text", "ttp_template"],
            '{"text": "…独立输入…", "ttp_template": "…完整模板…"}',
        ),
        "finish_generation": ([], "{}"),
    }
    payload = {
        "feedback_version": 1,
        "scope": "tool_protocol",
        "failure": failure,
        "allowed_tools": [
            {"name": name, "required_keys": examples[name][0]}
            for name in expected_tools
            if name in examples
        ],
    }
    lines = [
        "<protocol_feedback>",
        json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
        "</protocol_feedback>",
        "本轮调用未通过工具协议，尚未进行业务校验。下一轮只调用一个当前阶段工具。",
        "参数必须是工具要求的顶层 JSON 对象，不要额外包装 arguments 字符串。",
    ]
    for name in expected_tools:
        if name in examples:
            lines.append(
                f"{name} 参数结构（省略号须替换为完整内容）：{examples[name][1]}"
            )
    if "submit_result_schema" in expected_tools:
        lines.append(
            "type、properties、required、additionalProperties 等 Schema 关键字"
            "必须置于 result_schema 内，不能放在工具参数顶层。"
        )
    return "\n".join(lines)
