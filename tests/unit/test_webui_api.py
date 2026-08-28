"""Deterministic tests for the WebUI HTTP surface.

Every test injects a fake generator: the WebUI is exercised without a model,
so these stay in the ordinary offline unit suite.

``TestClient`` must be used as a context manager here.  Without it each request
gets its own event loop, which cancels the background run task and makes the
single-run constraint appear broken.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest
from agentscope.event import CustomEvent
from fastapi.testclient import TestClient

from cli_parser_agent import (
    ArtifactBundle,
    GenerationMetadata,
    GenerationResult,
    SchemaProposal,
    SchemaProposalResult,
)
from cli_parser_agent.webui.agent_service import AgentGenerationService
from cli_parser_agent.webui.app import _ProgressQueue, create_app
from cli_parser_agent.webui.store import RunStore

CLOSED_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {"value": {"type": "string"}},
    "required": ["value"],
    "additionalProperties": False,
}


async def _drain_queue(queue: _ProgressQueue) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    while True:
        item = await queue.get()
        if item is None:
            return items
        items.append(item)


@pytest.mark.asyncio
async def test_progress_queue_merges_deltas_within_window_without_loss() -> None:
    queue = _ProgressQueue(coalesce_window_seconds=0.01)
    queue.put_nowait(
        {
            "type": "agent.text_delta",
            "block_id": "text",
            "detail": {"text": "先"},
        },
    )
    queue.put_nowait(
        {
            "type": "agent.text_delta",
            "block_id": "text",
            "detail": {"text": "检查"},
        },
    )
    await asyncio.sleep(0.02)
    queue.close()

    assert await _drain_queue(queue) == [
        {
            "type": "agent.text_delta",
            "block_id": "text",
            "detail": {"text": "先检查", "coalesced": 1},
        },
    ]


@pytest.mark.asyncio
async def test_progress_queue_flushes_before_structure_and_splits_size_boundary(
) -> None:
    queue = _ProgressQueue(
        coalesce_window_seconds=0,
        max_delta_chars=4,
        max_items=8,
    )
    queue.put_nowait(
        {
            "type": "agent.thinking_delta",
            "block_id": "think",
            "detail": {"text": "abcdef"},
        },
    )
    queue.put_nowait(
        {
            "type": "agent.thinking_completed",
            "block_id": "think",
        },
    )
    queue.close()

    assert await _drain_queue(queue) == [
        {
            "type": "agent.thinking_delta",
            "block_id": "think",
            "detail": {"text": "abcd"},
        },
        {
            "type": "agent.thinking_delta",
            "block_id": "think",
            "detail": {"text": "ef"},
        },
        {
            "type": "agent.thinking_completed",
            "block_id": "think",
        },
    ]


def _metadata(**overrides: Any) -> GenerationMetadata:
    return GenerationMetadata(
        model_name="fake-model",
        command_output_count=1,
        termination_reason="success",
        **overrides,
    )


class FakeGenerator:
    """A generator stub that never contacts a model."""

    def __init__(self, *, events: list[tuple[str, str]] | None = None) -> None:
        self.events = events or []
        self.calls: list[str] = []

    async def _emit(self, observer: Any) -> None:
        for index, (name, phase) in enumerate(self.events, start=1):
            event = CustomEvent(name=name, value={"status": "ok"})
            event.metadata = {
                "phase": phase,
                "elapsed_seconds": index * 0.1,
                "sequence": index,
            }
            if observer is not None:
                observer(event)
            await asyncio.sleep(0)

    async def generate(self, request: Any, *, observer: Any = None) -> Any:
        self.calls.append("generate")
        await self._emit(observer)
        return GenerationResult(
            status="success",
            artifact=ArtifactBundle(
                ttp_template="value: {{ value }}",
                result_schema=CLOSED_SCHEMA,
                records=[{"value": "one"}],
            ),
            metadata=_metadata(),
        )

    async def propose_schema(self, request: Any, *, observer: Any = None) -> Any:
        self.calls.append("propose_schema")
        await self._emit(observer)
        return SchemaProposalResult(
            status="success",
            proposal=SchemaProposal(
                result_schema=CLOSED_SCHEMA,
            ),
            metadata=_metadata(),
        )

    async def generate_from_schema(self, request: Any, *, observer: Any = None) -> Any:
        self.calls.append("generate_from_schema")
        self.received_schema = dict(request.result_schema)
        await self._emit(observer)
        return await self.generate(request, observer=None)


class BlockingGenerator(FakeGenerator):
    """Stays running until released, so concurrency can be observed."""

    def __init__(self) -> None:
        super().__init__()
        self.release = asyncio.Event()

    async def generate(self, request: Any, *, observer: Any = None) -> Any:
        self.calls.append("generate")
        await self.release.wait()
        return await super().generate(request, observer=observer)


def _client(tmp_path: Path, generator: Any) -> TestClient:
    app = create_app(
        store=RunStore(tmp_path / "data"),
        service=AgentGenerationService(generator),
    )
    return TestClient(app)


def _wait_for_status(client: TestClient, run_id: str, *, timeout: float = 5.0) -> dict:
    """Poll until the run leaves the running state."""

    import time

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        payload = client.get(f"/api/runs/{run_id}").json()
        if payload["meta"]["status"] != "running":
            return payload
        time.sleep(0.02)
    raise AssertionError(f"run {run_id} never finished")


def test_index_references_current_static_asset_versions(tmp_path: Path) -> None:
    with _client(tmp_path, FakeGenerator()) as client:
        response = client.get("/")

    assert response.status_code == 200
    assert 'href="/static/style.css?v=6"' in response.text
    assert 'src="/static/app.js?v=10"' in response.text
    assert 'src="/static/agent-timeline.js?v=3"' in response.text
    assert 'src="/static/highlight.js?v=1"' in response.text
    assert 'src="/static/ui.js?v=1"' in response.text


def test_index_static_dependencies_are_served(tmp_path: Path) -> None:
    with _client(tmp_path, FakeGenerator()) as client:
        paths = (
            "/static/style.css",
            "/static/agent-timeline.js",
            "/static/schema-model.js",
            "/static/highlight.js",
            "/static/ui.js",
            "/static/app.js",
        )
        for path in paths:
            response = client.get(path)
            assert response.status_code == 200, path


def test_runtime_config_can_be_overridden_and_is_redacted_from_api(
    tmp_path: Path,
) -> None:
    with _client(tmp_path, FakeGenerator()) as client:
        defaults = client.get("/api/runtime-config")
        assert defaults.status_code == 200
        assert defaults.json()["settings"]["model_name"] == "webui-test-model"
        defaults_text = json.dumps(defaults.json())
        assert '"api_key":' not in defaults_text

        created = client.post(
            "/api/runs",
            json={
                "command_outputs": ["value: one"],
                "parameters": {
                    "settings": {
                        "api_key": "run-secret",
                        "model_name": "run-model",
                        "temperature": 0.3,
                    },
                    "policy": {"max_agent_rounds": 24},
                },
            },
        )
        assert created.status_code == 201
        run_id = created.json()["run_id"]
        payload = _wait_for_status(client, run_id)

    config_path = tmp_path / "data" / "runs" / run_id / "config.json"
    config = json.loads(config_path.read_text(encoding="utf-8"))
    assert config["settings"]["api_key"] == "run-secret"
    assert config["settings"]["model_name"] == "run-model"
    assert config["policy"]["max_agent_rounds"] == 24
    assert "run-secret" not in json.dumps(payload)
    assert payload["config"]["settings"]["api_key_configured"] is True


def test_runtime_config_rejects_parallel_tool_calls_before_creating_run(
    tmp_path: Path,
) -> None:
    with _client(tmp_path, FakeGenerator()) as client:
        response = client.post(
            "/api/runs",
            json={
                "command_outputs": ["value: one"],
                "parameters": {"settings": {"parallel_tool_calls": True}},
            },
        )
        assert response.status_code == 422
        assert client.get("/api/runs").json()["runs"] == []


def test_full_run_persists_result_and_reports_success(tmp_path: Path) -> None:
    generator = FakeGenerator()
    with _client(tmp_path, generator) as client:
        created = client.post(
            "/api/runs",
            json={
                "mode": "full",
                "title": "brief",
                "command_outputs": ["value: one"],
            },
        )
        assert created.status_code == 201
        run_id = created.json()["run_id"]

        payload = _wait_for_status(client, run_id)

    assert generator.calls == ["generate"]
    assert payload["meta"]["status"] == "success"
    assert payload["meta"]["elapsed_seconds"] is not None
    assert payload["result"]["artifact"]["ttp_template"] == "value: {{ value }}"
    assert "assumptions" not in payload["result"]["artifact"]
    assert payload["inputs"] == ["value: one"]


def test_propose_run_saves_the_schema_for_review(tmp_path: Path) -> None:
    generator = FakeGenerator()
    with _client(tmp_path, generator) as client:
        run_id = client.post(
            "/api/runs",
            json={"mode": "propose", "command_outputs": ["value: one"]},
        ).json()["run_id"]

        payload = _wait_for_status(client, run_id)

    assert generator.calls == ["propose_schema"]
    assert payload["schema"] == CLOSED_SCHEMA
    assert "assumptions" not in payload["result"]["proposal"]
    assert payload["result"].get("artifact") is None


def test_historical_assumptions_remain_readable_and_do_not_block_rerun(
    tmp_path: Path,
) -> None:
    generator = FakeGenerator()
    with _client(tmp_path, generator) as client:
        run_id = client.post(
            "/api/runs",
            json={"mode": "propose", "command_outputs": ["value: one"]},
        ).json()["run_id"]
        source = _wait_for_status(client, run_id)
        historical_result = dict(source["result"])
        historical_result["proposal"] = {
            **historical_result["proposal"],
            "assumptions": ["legacy local record"],
        }
        client.app.state.store.write_result(run_id, historical_result)

        reloaded = client.get(f"/api/runs/{run_id}").json()
        rerun = client.post(f"/api/runs/{run_id}/rerun")
        assert rerun.status_code == 201
        child = _wait_for_status(client, rerun.json()["run_id"])

    assert reloaded["result"]["proposal"]["assumptions"] == [
        "legacy local record",
    ]
    assert generator.received_schema == CLOSED_SCHEMA
    assert child["meta"]["source_run_id"] == run_id
    assert child["meta"]["schema_source"] == "saved_schema"
    assert "assumptions" not in child["result"]["artifact"]


def test_edited_schema_is_validated_before_it_is_saved(tmp_path: Path) -> None:
    generator = FakeGenerator()
    with _client(tmp_path, generator) as client:
        run_id = client.post(
            "/api/runs",
            json={"mode": "propose", "command_outputs": ["value: one"]},
        ).json()["run_id"]
        _wait_for_status(client, run_id)

        # An open object is not part of the closed Draft 2020-12 subset.
        rejected = client.put(
            f"/api/runs/{run_id}/schema",
            json={"result_schema": {"type": "object", "properties": {}}},
        ).json()
        assert rejected["saved"] is False
        assert "schema.object_not_closed" in {
            issue["code"] for issue in rejected["issues"]
        }
        # The stored schema is untouched by a rejected edit.
        assert client.get(f"/api/runs/{run_id}").json()["schema"] == CLOSED_SCHEMA

        reserved = {
            "type": "object",
            "properties": {"ignore": {"type": "string"}},
            "required": ["ignore"],
            "additionalProperties": False,
        }
        rejected = client.put(
            f"/api/runs/{run_id}/schema",
            json={"result_schema": reserved},
        ).json()
        assert rejected["saved"] is False
        assert [issue["code"] for issue in rejected["issues"]] == [
            "schema.reserved_scalar_field_name",
        ]
        assert client.get(f"/api/runs/{run_id}").json()["schema"] == CLOSED_SCHEMA

        renamed = {
            "type": "object",
            "properties": {"as": {"type": "string"}},
            "required": ["as"],
            "additionalProperties": False,
        }
        accepted = client.put(
            f"/api/runs/{run_id}/schema",
            json={"result_schema": renamed},
        ).json()
        assert accepted == {"saved": True, "issues": []}
        assert client.get(f"/api/runs/{run_id}").json()["schema"] == renamed


def test_schema_rerun_creates_an_independent_child_with_the_edited_schema(
    tmp_path: Path,
) -> None:
    generator = FakeGenerator()
    renamed = {
        "type": "object",
        "properties": {"name": {"type": "string"}},
        "required": ["name"],
        "additionalProperties": False,
    }
    with _client(tmp_path, generator) as client:
        run_id = client.post(
            "/api/runs",
            json={"mode": "propose", "command_outputs": ["value: one"]},
        ).json()["run_id"]
        _wait_for_status(client, run_id)
        client.put(f"/api/runs/{run_id}/schema", json={"result_schema": renamed})

        source_before = client.get(f"/api/runs/{run_id}").json()
        started = client.post(
            f"/api/runs/{run_id}/rerun",
            json={
                "parameters": {
                    "settings": {"model_name": "rerun-model"},
                    "policy": {"max_ttp_submissions": 12},
                },
            },
        )
        assert started.status_code == 201
        child_run_id = started.json()["run_id"]
        assert child_run_id != run_id
        payload = _wait_for_status(client, child_run_id)
        source_after = client.get(f"/api/runs/{run_id}").json()

    # The reviewer's schema, not the model's original proposal, was used.
    assert generator.received_schema == renamed
    assert payload["result"]["artifact"]["ttp_template"] == "value: {{ value }}"
    assert payload["inputs"] == ["value: one"]
    assert payload["schema"] == renamed
    assert payload["meta"]["mode"] == "schema_rerun"
    assert payload["meta"]["execution_kind"] == "schema_rerun"
    assert payload["config"]["settings"]["model_name"] == "rerun-model"
    assert payload["config"]["policy"]["max_ttp_submissions"] == 12
    assert payload["meta"]["source_run_id"] == run_id
    assert payload["meta"]["schema_source"] == "saved_schema"
    assert source_after == source_before
    assert [event["sequence"] for event in payload["events"]] == list(
        range(1, len(payload["events"]) + 1),
    )
    assert [event["type"] for event in payload["events"]].count("run.finished") == 1


def test_schema_review_then_rerun_keeps_source_events_unchanged(
    tmp_path: Path,
) -> None:
    generator = FakeGenerator(
        events=[
            ("cli_parser.phase.started", "schema"),
            ("cli_parser.phase.completed", "schema"),
        ],
    )
    with _client(tmp_path, generator) as client:
        run_id = client.post(
            "/api/runs",
            json={"mode": "propose", "command_outputs": ["value: one"]},
        ).json()["run_id"]
        _wait_for_status(client, run_id)

        source_events = client.get(f"/api/runs/{run_id}").json()["events"]
        response = client.post(f"/api/runs/{run_id}/generate")
        assert response.status_code == 201
        child_run_id = response.json()["run_id"]
        child = _wait_for_status(client, child_run_id)

        events = client.get(f"/api/runs/{run_id}").json()["events"]

    assert events == source_events
    assert [event["type"] for event in events].count("run.finished") == 1
    assert [event["sequence"] for event in child["events"]] == list(
        range(1, len(child["events"]) + 1),
    )
    assert [event["type"] for event in child["events"]].count("run.finished") == 1
    assert generator.calls == [
        "propose_schema",
        "generate_from_schema",
        "generate",
    ]


def test_complete_supported_schema_subset_is_saved_and_used(tmp_path: Path) -> None:
    generator = FakeGenerator()
    reviewed = {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "type": "object",
        "title": "Inventory",
        "description": "Reviewed in the WebUI",
        "properties": {
            "hostname": {
                "type": "string",
                "enum": ["r1", "r2"],
                "minLength": 1,
                "maxLength": 64,
            },
            "interfaces": {
                "type": "array",
                "minItems": 0,
                "maxItems": 128,
                "items": {
                    "type": "object",
                    "properties": {
                        "name": {"type": "string"},
                        "mtu": {"type": "integer", "minimum": 0},
                        "enabled": {"type": "boolean"},
                    },
                    "required": ["name"],
                    "additionalProperties": False,
                },
            },
        },
        "required": ["hostname"],
        "additionalProperties": False,
    }
    with _client(tmp_path, generator) as client:
        run_id = client.post(
            "/api/runs",
            json={"mode": "propose", "command_outputs": ["value: one"]},
        ).json()["run_id"]
        _wait_for_status(client, run_id)

        saved = client.put(
            f"/api/runs/{run_id}/schema",
            json={"result_schema": reviewed},
        )
        assert saved.json() == {"saved": True, "issues": []}
        assert client.post(f"/api/runs/{run_id}/generate").status_code == 201
        _wait_for_status(client, run_id)

    assert generator.received_schema == reviewed


def test_schema_rerun_falls_back_to_a_successful_artifact_schema(
    tmp_path: Path,
) -> None:
    generator = FakeGenerator()
    with _client(tmp_path, generator) as client:
        source_run_id = client.post(
            "/api/runs",
            json={"mode": "full", "title": "brief", "command_outputs": ["value: one"]},
        ).json()["run_id"]
        source = _wait_for_status(client, source_run_id)
        assert source["schema"] is None

        rerun = client.post(f"/api/runs/{source_run_id}/rerun")
        assert rerun.status_code == 201
        child = _wait_for_status(client, rerun.json()["run_id"])

    assert generator.received_schema == CLOSED_SCHEMA
    assert child["schema"] == CLOSED_SCHEMA
    assert child["meta"]["schema_source"] == "artifact_schema"
    assert child["meta"]["title"] == "brief · Schema 重新生成"


def test_schema_rerun_requires_a_usable_schema(tmp_path: Path) -> None:
    class FailedGenerator(FakeGenerator):
        async def generate(self, request: Any, *, observer: Any = None) -> Any:
            self.calls.append("generate")
            return GenerationResult(
                status="failed",
                artifact=None,
                metadata=_metadata(termination_reason="failed"),
            )

    with _client(tmp_path, FailedGenerator()) as client:
        run_id = client.post(
            "/api/runs",
            json={"mode": "full", "command_outputs": ["value: one"]},
        ).json()["run_id"]
        _wait_for_status(client, run_id)

        # A failed full run has neither a saved Schema nor an artifact Schema.
        response = client.post(f"/api/runs/{run_id}/rerun")

    assert response.status_code == 400


def test_schema_rerun_rejects_unfinished_or_corrupt_source_runs(
    tmp_path: Path,
) -> None:
    generator = BlockingGenerator()
    with _client(tmp_path, generator) as client:
        active_run_id = client.post(
            "/api/runs",
            json={"mode": "full", "command_outputs": ["value: one"]},
        ).json()["run_id"]
        response = client.post(f"/api/runs/{active_run_id}/rerun")
        assert response.status_code == 409
        generator.release.set()
        _wait_for_status(client, active_run_id)

        store = client.app.state.store
        store.write_schema(active_run_id, {"type": "object", "properties": {}})
        response = client.post(f"/api/runs/{active_run_id}/rerun")
        assert response.status_code == 400
        assert len(store.list_runs()) == 1


def test_only_one_run_may_be_active(tmp_path: Path) -> None:
    generator = BlockingGenerator()
    with _client(tmp_path, generator) as client:
        first = client.post(
            "/api/runs",
            json={"command_outputs": ["value: one"]},
        )
        assert first.status_code == 201
        run_id = first.json()["run_id"]

        second = client.post("/api/runs", json={"command_outputs": ["other"]})
        assert second.status_code == 409
        assert run_id in second.json()["detail"]

        # The rejected request must not leave an orphaned run directory behind:
        # the busy check has to happen before the store creates anything.
        listing = client.get("/api/runs").json()
        assert [run["run_id"] for run in listing["runs"]] == [run_id]
        assert listing["active_run"] == run_id

        generator.release.set()
        _wait_for_status(client, run_id)

        # The slot is free again once the run finishes.
        assert client.post(
            "/api/runs",
            json={"command_outputs": ["third"]},
        ).status_code == 201


def test_running_run_cannot_be_deleted(tmp_path: Path) -> None:
    generator = BlockingGenerator()
    with _client(tmp_path, generator) as client:
        run_id = client.post(
            "/api/runs",
            json={"command_outputs": ["value: one"]},
        ).json()["run_id"]

        assert client.delete(f"/api/runs/{run_id}").status_code == 409

        generator.release.set()
        _wait_for_status(client, run_id)
        assert client.delete(f"/api/runs/{run_id}").json() == {"deleted": True}
        assert client.get(f"/api/runs/{run_id}").status_code == 404


def test_unknown_and_malformed_run_ids_are_rejected(tmp_path: Path) -> None:
    with _client(tmp_path, FakeGenerator()) as client:
        assert client.get("/api/runs/20990101T000000.000000Z").status_code == 404
        assert client.get("/api/runs/not-a-run-id").status_code == 400


def test_history_lists_runs_newest_first(tmp_path: Path) -> None:
    with _client(tmp_path, FakeGenerator()) as client:
        first = client.post(
            "/api/runs",
            json={"command_outputs": ["a"], "title": "first"},
        ).json()["run_id"]
        _wait_for_status(client, first)
        second = client.post(
            "/api/runs",
            json={"command_outputs": ["b"], "title": "second"},
        ).json()["run_id"]
        _wait_for_status(client, second)

        listing = client.get("/api/runs").json()

    assert [run["run_id"] for run in listing["runs"]] == [second, first]
    assert listing["active_run"] is None


def test_event_stream_forwards_bounded_facts_only(tmp_path: Path) -> None:
    generator = FakeGenerator(
        events=[
            ("cli_parser.generation.started", "generation"),
            ("cli_parser.phase.started", "schema"),
            # Context snapshots must never reach the WebUI channel.
            ("cli_parser.model.context_snapshot", "schema"),
            ("cli_parser.tool.result", "schema"),
            ("cli_parser.phase.completed", "schema"),
        ],
    )
    with _client(tmp_path, generator) as client:
        run_id = client.post(
            "/api/runs",
            json={"command_outputs": ["value: one"]},
        ).json()["run_id"]

        received: list[dict[str, Any]] = []
        raw_lines: list[str] = []
        with client.stream("GET", f"/api/runs/{run_id}/events") as stream:
            for line in stream.iter_lines():
                raw_lines.append(line)
                if not line.startswith("data:"):
                    continue
                event = json.loads(line[len("data:") :].strip())
                received.append(event)
                if event.get("type") == "run.finished":
                    break

    types = [event["type"] for event in received]
    assert "cli_parser.model.context_snapshot" not in types
    assert "cli_parser.tool.result" in types
    assert "cli_parser.phase.started" in types
    assert "cli_parser.phase.completed" in types
    assert types[-1] == "run.finished"
    assert not any(line.startswith("event:") for line in raw_lines)
    for event in received[:-1]:
        assert set(event) <= {"phase", "elapsed_seconds", "sequence", "type", "detail"}
        assert event["sequence"] >= 1


def test_event_stream_replays_a_finished_run(tmp_path: Path) -> None:
    generator = FakeGenerator(
        events=[("cli_parser.phase.started", "schema")],
    )
    with _client(tmp_path, generator) as client:
        run_id = client.post(
            "/api/runs",
            json={"command_outputs": ["value: one"]},
        ).json()["run_id"]
        _wait_for_status(client, run_id)

        received = []
        with client.stream("GET", f"/api/runs/{run_id}/events") as stream:
            for line in stream.iter_lines():
                if not line.startswith("data:"):
                    continue
                event = json.loads(line[len("data:") :].strip())
                received.append(event)
                if event.get("type") == "run.finished":
                    break

    assert [event["type"] for event in received] == [
        "cli_parser.phase.started",
        "run.finished",
    ]


def test_event_stream_supports_last_event_id_replay(tmp_path: Path) -> None:
    generator = FakeGenerator(
        events=[
            ("cli_parser.phase.started", "schema"),
            ("cli_parser.phase.completed", "schema"),
        ],
    )
    with _client(tmp_path, generator) as client:
        run_id = client.post(
            "/api/runs",
            json={"command_outputs": ["value: one"]},
        ).json()["run_id"]
        _wait_for_status(client, run_id)

        received: list[dict[str, Any]] = []
        with client.stream(
            "GET",
            f"/api/runs/{run_id}/events",
            headers={"Last-Event-ID": "1"},
        ) as stream:
            for line in stream.iter_lines():
                if not line.startswith("data:"):
                    continue
                event = json.loads(line[len("data:") :].strip())
                received.append(event)
                if event.get("type") == "run.finished":
                    break

    assert received
    assert all(event["sequence"] > 1 for event in received)


def test_event_stream_supports_after_sequence_query_without_duplicates(
    tmp_path: Path,
) -> None:
    generator = FakeGenerator(
        events=[
            ("cli_parser.phase.started", "schema"),
            ("cli_parser.phase.completed", "schema"),
        ],
    )
    with _client(tmp_path, generator) as client:
        run_id = client.post(
            "/api/runs",
            json={"command_outputs": ["value: one"]},
        ).json()["run_id"]
        _wait_for_status(client, run_id)

        received: list[dict[str, Any]] = []
        with client.stream(
            "GET",
            f"/api/runs/{run_id}/events?after_sequence=1",
        ) as stream:
            for line in stream.iter_lines():
                if not line.startswith("data:"):
                    continue
                event = json.loads(line[len("data:") :].strip())
                received.append(event)
                if event.get("type") == "run.finished":
                    break

    assert received
    assert all(event["sequence"] > 1 for event in received)
    assert len({event["sequence"] for event in received}) == len(received)


def test_failed_generation_is_recorded_without_crashing_the_server(
    tmp_path: Path,
) -> None:
    class ExplodingGenerator(FakeGenerator):
        async def generate(self, request: Any, *, observer: Any = None) -> Any:
            raise RuntimeError("model exploded")

    with _client(tmp_path, ExplodingGenerator()) as client:
        run_id = client.post(
            "/api/runs",
            json={"command_outputs": ["value: one"]},
        ).json()["run_id"]
        payload = _wait_for_status(client, run_id)

        # The failure is contained and the app still accepts work.
        assert client.get("/api/runs").status_code == 200

    assert payload["meta"]["status"] == "failed"
    assert payload["meta"]["termination_reason"] == "exception:RuntimeError"


@pytest.mark.parametrize(
    "payload",
    [
        {"command_outputs": []},
        {"command_outputs": ["   "]},
        {"command_outputs": ["ok"], "mode": "unknown-mode"},
        {"command_outputs": ["ok"], "unexpected": True},
    ],
)
def test_invalid_create_requests_are_rejected(
    tmp_path: Path,
    payload: dict[str, Any],
) -> None:
    with _client(tmp_path, FakeGenerator()) as client:
        assert client.post("/api/runs", json=payload).status_code == 422
