"""WebUI-owned request and progress contracts.

These types deliberately contain no AgentScope or generation-workflow types.
The WebUI talks to a service boundary using plain JSON-compatible values.
"""

from __future__ import annotations

from typing import Any, Literal, TypedDict

from pydantic import BaseModel, ConfigDict, Field

from .runtime_config import RuntimeParameters

RunMode = Literal["full", "propose"]


class CreateRunRequest(BaseModel):
    """One new run submitted from the browser."""

    model_config = ConfigDict(extra="forbid")

    mode: RunMode = "full"
    title: str = Field(default="", max_length=200)
    command_outputs: list[str] = Field(min_length=1, max_length=5)
    parameters: RuntimeParameters | None = None


class RerunRunRequest(BaseModel):
    """Optional runtime overrides for a Schema-only rerun."""

    model_config = ConfigDict(extra="forbid")

    parameters: RuntimeParameters | None = None


class SaveSchemaRequest(BaseModel):
    """A reviewed, possibly edited result schema."""

    model_config = ConfigDict(extra="forbid")

    result_schema: dict[str, Any]


class WebUIProgressEvent(TypedDict, total=False):
    """JSON-compatible event projected for the local WebUI transcript."""

    phase: str | None
    elapsed_seconds: float
    sequence: int | None
    round_index: int | None
    block_id: str | None
    tool_call_id: str | None
    type: str
    detail: dict[str, Any]


__all__ = [
    "CreateRunRequest",
    "RerunRunRequest",
    "RunMode",
    "RuntimeParameters",
    "SaveSchemaRequest",
    "WebUIProgressEvent",
]
