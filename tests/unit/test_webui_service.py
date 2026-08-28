"""Tests for the WebUI service boundary independent of AgentScope."""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

from agentscope.event import (
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
from agentscope.message import ToolResultState
from fastapi.testclient import TestClient

from cli_parser_agent.config import GenerationPolicy, TtpGeneratorSettings
from cli_parser_agent.webui.agent_service import project_agent_event
from cli_parser_agent.webui.app import create_app
from cli_parser_agent.webui.runtime_config import (
    RuntimeParameters,
    public_config_payload,
    resolve_runtime_config,
)
from cli_parser_agent.webui.store import RunStore

SCHEMA = {
    "type": "object",
    "properties": {"value": {"type": "string"}},
    "required": ["value"],
    "additionalProperties": False,
}


class StubService:
    """A WebUI-facing service with no knowledge of the generation package."""

    def __init__(self) -> None:
        self.calls: list[tuple[Any, ...]] = []
        self.settings = TtpGeneratorSettings(
            api_key="stub-key",
            model_name="stub-model",
        )
        self.policy = GenerationPolicy()

    def resolve_runtime_config(
        self,
        parameters: RuntimeParameters | None = None,
    ) -> Any:
        return resolve_runtime_config(self.settings, self.policy, parameters)

    def public_runtime_config(self) -> dict[str, Any]:
        return public_config_payload(self.resolve_runtime_config())

    def validate_inputs(self, command_outputs: list[str]) -> list[str]:
        return list(command_outputs)

    async def run(
        self,
        mode: str,
        command_outputs: list[str],
        *,
        observer: Any = None,
        runtime_config: Any = None,
    ) -> dict[str, Any]:
        self.calls.append((mode, list(command_outputs)))
        return {
            "status": "success",
            "proposal": None,
            "issues": [],
            "metadata": {"termination_reason": "success"},
            "artifact": {
                "ttp_template": "value: {{ value }}",
                "result_schema": SCHEMA,
                "records": [{"value": command_outputs[0]}],
            },
        }

    async def run_from_schema(
        self,
        command_outputs: list[str],
        result_schema: dict[str, Any],
        *,
        observer: Any = None,
        runtime_config: Any = None,
    ) -> dict[str, Any]:
        self.calls.append(("from_schema", list(command_outputs), result_schema))
        return await self.run("from_schema", command_outputs, observer=observer)

    def validate_schema(self, result_schema: dict[str, Any]) -> list[dict[str, Any]]:
        return []


def _wait_for_status(client: TestClient, run_id: str) -> dict[str, Any]:
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        payload = client.get(f"/api/runs/{run_id}").json()
        if payload["meta"]["status"] != "running":
            return payload
        time.sleep(0.02)
    raise AssertionError("run never finished")


def test_app_uses_only_the_webui_service_boundary(tmp_path: Path) -> None:
    service = StubService()
    app = create_app(store=RunStore(tmp_path / "data"), service=service)
    with TestClient(app) as client:
        created = client.post(
            "/api/runs",
            json={"mode": "full", "command_outputs": ["value: one"]},
        )
        assert created.status_code == 201
        payload = _wait_for_status(client, created.json()["run_id"])

    assert service.calls == [("full", ["value: one"])]
    assert payload["result"]["artifact"]["records"] == [{"value": "value: one"}]


def test_project_agent_event_exposes_stream_blocks_without_context_snapshots() -> None:
    metadata = {"phase": "ttp", "elapsed_seconds": 1.25, "sequence": 3}
    events = [
        ThinkingBlockStartEvent(reply_id="r", block_id="think", metadata=metadata),
        ThinkingBlockDeltaEvent(
            reply_id="r", block_id="think", delta="检查", metadata=metadata,
        ),
        ThinkingBlockEndEvent(reply_id="r", block_id="think", metadata=metadata),
        TextBlockStartEvent(reply_id="r", block_id="text", metadata=metadata),
        TextBlockDeltaEvent(
            reply_id="r", block_id="text", delta="提交", metadata=metadata,
        ),
        TextBlockEndEvent(reply_id="r", block_id="text", metadata=metadata),
        ToolCallStartEvent(
            reply_id="r", tool_call_id="call",
            tool_call_name="submit_ttp_template", metadata=metadata,
        ),
        ToolCallDeltaEvent(
            reply_id="r", tool_call_id="call", delta='{"x":1}', metadata=metadata,
        ),
        ToolCallEndEvent(reply_id="r", tool_call_id="call", metadata=metadata),
        ToolResultStartEvent(
            reply_id="r", tool_call_id="call",
            tool_call_name="submit_ttp_template", metadata=metadata,
        ),
        ToolResultTextDeltaEvent(
            reply_id="r", tool_call_id="call",
            delta='{"accepted":true}', metadata=metadata,
        ),
        ToolResultEndEvent(
            reply_id="r", tool_call_id="call",
            state=ToolResultState.SUCCESS, metadata=metadata,
        ),
        ModelCallStartEvent(reply_id="r", model_name="test", metadata=metadata),
        ModelCallEndEvent(
            reply_id="r", input_tokens=1, output_tokens=2, metadata=metadata,
        ),
    ]

    projected = [project_agent_event(event) for event in events]
    projected = [event for event in projected if event is not None]

    assert [event["type"] for event in projected] == [
        "agent.thinking_started", "agent.thinking_delta",
        "agent.thinking_completed", "agent.text_started", "agent.text_delta",
        "agent.text_completed", "agent.tool_call_started",
        "agent.tool_call_delta", "agent.tool_call_completed",
        "agent.tool_result_started", "agent.tool_result_delta",
        "agent.tool_result_completed",
        "agent.model_call_started", "agent.model_call_completed",
    ]
    assert projected[1]["detail"]["text"] == "检查"
    assert projected[7]["detail"]["text"] == '{"x":1}'
    assert projected[11]["detail"]["state"]
