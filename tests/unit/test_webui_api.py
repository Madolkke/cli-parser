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
from cli_parser_agent.webui.app import create_app
from cli_parser_agent.webui.store import RunStore

CLOSED_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {"value": {"type": "string"}},
    "required": ["value"],
    "additionalProperties": False,
}


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
                assumptions=["reviewed by hand"],
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
    assert payload["result"]["proposal"]["assumptions"] == ["reviewed by hand"]
    assert payload["result"].get("artifact") is None


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

        renamed = {
            "type": "object",
            "properties": {"name": {"type": "string"}},
            "required": ["name"],
            "additionalProperties": False,
        }
        accepted = client.put(
            f"/api/runs/{run_id}/schema",
            json={"result_schema": renamed},
        ).json()
        assert accepted == {"saved": True, "issues": []}
        assert client.get(f"/api/runs/{run_id}").json()["schema"] == renamed


def test_generate_uses_the_edited_schema(tmp_path: Path) -> None:
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

        started = client.post(f"/api/runs/{run_id}/generate")
        assert started.status_code == 201
        payload = _wait_for_status(client, run_id)

    # The reviewer's schema, not the model's original proposal, was used.
    assert generator.received_schema == renamed
    assert payload["result"]["artifact"]["ttp_template"] == "value: {{ value }}"


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


def test_generate_requires_a_saved_schema(tmp_path: Path) -> None:
    with _client(tmp_path, FakeGenerator()) as client:
        run_id = client.post(
            "/api/runs",
            json={"mode": "full", "command_outputs": ["value: one"]},
        ).json()["run_id"]
        _wait_for_status(client, run_id)

        # A full run stores no reviewable schema file.
        response = client.post(f"/api/runs/{run_id}/generate")

    assert response.status_code == 400


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
        with client.stream("GET", f"/api/runs/{run_id}/events") as stream:
            for line in stream.iter_lines():
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
