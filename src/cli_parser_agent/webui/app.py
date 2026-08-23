"""FastAPI application for the local single-user WebUI.

The app owns one run at a time.  A generation is started in a background task
so the HTTP request returns immediately: a full run can take the whole default
900-second budget, which no browser request should ever hold open.
"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import AsyncIterator
from contextlib import suppress
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from agentscope.event import AgentEvent, CustomEvent, ModelCallStartEvent
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field
from sse_starlette.sse import EventSourceResponse

from ..ttp_generation.contracts import (
    MAX_COMMAND_OUTPUTS,
    GenerationRequest,
    TemplateRequest,
)
from ..ttp_generation.generator import TtpGenerator
from ..ttp_generation.validation import validate_result_schema
from .store import RunStore, RunStoreError

STATIC_DIRECTORY = Path(__file__).resolve().parent / "static"

# Progress events worth forwarding.  Everything else is either a stream of
# model text or a multi-megabyte context snapshot, neither of which belongs on
# a progress channel.
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
    },
)

RunMode = Literal["full", "propose"]


class CreateRunRequest(BaseModel):
    """One new run submitted from the browser."""

    model_config = ConfigDict(extra="forbid")

    mode: RunMode = "full"
    title: str = Field(default="", max_length=200)
    command_outputs: list[str] = Field(min_length=1, max_length=MAX_COMMAND_OUTPUTS)


class SaveSchemaRequest(BaseModel):
    """A reviewed, possibly edited result schema."""

    model_config = ConfigDict(extra="forbid")

    result_schema: dict[str, Any]


class _QueueObserver:
    """A synchronous observer whose only normal action is ``put_nowait``."""

    def __init__(self, queue: asyncio.Queue[Any]) -> None:
        self._queue = queue
        self.failed = False

    def __call__(self, event: AgentEvent) -> None:
        if self.failed:
            return
        try:
            payload = _project_event(event)
            if payload is not None:
                self._queue.put_nowait(payload)
        except BaseException:
            self.failed = True


def _project_event(event: AgentEvent) -> dict[str, Any] | None:
    """Reduce one raw event to a bounded progress fact."""

    metadata = dict(getattr(event, "metadata", None) or {})
    common = {
        "phase": metadata.get("phase"),
        "elapsed_seconds": round(float(metadata.get("elapsed_seconds") or 0.0), 3),
        "sequence": metadata.get("sequence"),
    }
    if isinstance(event, ModelCallStartEvent):
        return {**common, "type": "model_call"}
    if isinstance(event, CustomEvent):
        if event.name not in _FORWARDED_EVENTS:
            return None
        detail: dict[str, Any] = {}
        value = event.value if isinstance(event.value, dict) else {}
        for key in ("status", "termination_reason", "retry_number", "reason"):
            if key in value:
                detail[key] = value[key]
        return {**common, "type": event.name, "detail": detail}
    return None


class RunManager:
    """Run one generation at a time and fan progress out to SSE listeners."""

    def __init__(self, store: RunStore, generator: Any) -> None:
        self.store = store
        self.generator = generator
        self._task: asyncio.Task[None] | None = None
        self._active_run: str | None = None
        self._queues: dict[str, list[asyncio.Queue[Any]]] = {}

    @property
    def active_run(self) -> str | None:
        if self._task is not None and self._task.done():
            self._active_run = None
            self._task = None
        return self._active_run

    def subscribe(self, run_id: str) -> asyncio.Queue[Any]:
        queue: asyncio.Queue[Any] = asyncio.Queue()
        self._queues.setdefault(run_id, []).append(queue)
        return queue

    def unsubscribe(self, run_id: str, queue: asyncio.Queue[Any]) -> None:
        listeners = self._queues.get(run_id)
        if not listeners:
            return
        if queue in listeners:
            listeners.remove(queue)
        if not listeners:
            self._queues.pop(run_id, None)

    def _publish(self, run_id: str, payload: dict[str, Any]) -> None:
        for queue in self._queues.get(run_id, []):
            queue.put_nowait(payload)

    def start(self, run_id: str, coroutine_factory: Any) -> None:
        """Start one background run, rejecting a second concurrent request."""

        if self.active_run is not None:
            raise HTTPException(
                status_code=409,
                detail=f"run {self.active_run} is still in progress",
            )
        self._active_run = run_id
        self._task = asyncio.create_task(self._execute(run_id, coroutine_factory))

    async def _execute(self, run_id: str, coroutine_factory: Any) -> None:
        queue: asyncio.Queue[Any] = asyncio.Queue()
        observer = _QueueObserver(queue)
        started = time.monotonic()
        pump = asyncio.create_task(self._pump(run_id, queue))
        try:
            result = await coroutine_factory(observer)
        except asyncio.CancelledError:
            self.store.update_meta(
                run_id,
                status="cancelled",
                finished_at=_now(),
                elapsed_seconds=round(time.monotonic() - started, 3),
                termination_reason="cancelled",
            )
            self._publish(run_id, {"type": "run.finished", "status": "cancelled"})
            raise
        except Exception as error:
            self.store.update_meta(
                run_id,
                status="failed",
                finished_at=_now(),
                elapsed_seconds=round(time.monotonic() - started, 3),
                termination_reason=f"exception:{type(error).__name__}",
            )
            self._publish(run_id, {"type": "run.finished", "status": "failed"})
        else:
            payload = result.model_dump(mode="json")
            self.store.write_result(run_id, payload)
            proposal = payload.get("proposal")
            if isinstance(proposal, dict) and isinstance(
                proposal.get("result_schema"),
                dict,
            ):
                self.store.write_schema(run_id, proposal["result_schema"])
            self.store.update_meta(
                run_id,
                status=payload.get("status", "failed"),
                finished_at=_now(),
                elapsed_seconds=round(time.monotonic() - started, 3),
                termination_reason=payload.get("metadata", {}).get(
                    "termination_reason",
                ),
            )
            self._publish(
                run_id,
                {"type": "run.finished", "status": payload.get("status")},
            )
        finally:
            queue.put_nowait(None)
            await pump
            self._active_run = None

    async def _pump(self, run_id: str, queue: asyncio.Queue[Any]) -> None:
        """Persist and fan out progress facts until the run signals stop."""

        while True:
            item = await queue.get()
            if item is None:
                return
            self.store.append_event(run_id, item)
            self._publish(run_id, item)

    async def cancel(self) -> bool:
        task = self._task
        if task is None or task.done():
            return False
        task.cancel()
        # The task's own handler records the cancelled state; nothing it raises
        # on the way out should propagate into this request.
        with suppress(asyncio.CancelledError, Exception):
            await task
        return True


def _now() -> str:
    return datetime.now(UTC).isoformat()


def create_app(
    *,
    store: RunStore,
    generator: Any | None = None,
) -> FastAPI:
    """Build the WebUI application around one run store and generator."""

    app = FastAPI(title="CLI Parser Agent", docs_url=None, redoc_url=None)
    resolved = generator if generator is not None else TtpGenerator.from_env()
    manager = RunManager(store, resolved)
    app.state.store = store
    app.state.manager = manager

    def _require_run(run_id: str) -> dict[str, Any]:
        try:
            meta = store.read_meta(run_id)
        except RunStoreError as error:
            raise HTTPException(status_code=400, detail=str(error)) from error
        if meta is None:
            raise HTTPException(status_code=404, detail="run not found")
        return meta

    @app.get("/api/runs")
    def list_runs() -> dict[str, Any]:
        return {"runs": store.list_runs(), "active_run": manager.active_run}

    @app.post("/api/runs", status_code=201)
    async def create_run(payload: CreateRunRequest) -> dict[str, Any]:
        if manager.active_run is not None:
            raise HTTPException(
                status_code=409,
                detail=f"run {manager.active_run} is still in progress",
            )
        try:
            request = GenerationRequest(command_outputs=payload.command_outputs)
        except ValueError as error:
            raise HTTPException(status_code=422, detail=str(error)) from error

        run_id = store.create(
            mode=payload.mode,
            command_outputs=list(request.command_outputs),
            title=payload.title.strip(),
        )
        if payload.mode == "propose":
            def factory(observer: Any) -> Any:
                return resolved.propose_schema(request, observer=observer)
        else:
            def factory(observer: Any) -> Any:
                return resolved.generate(request, observer=observer)

        manager.start(run_id, factory)
        return {"run_id": run_id, "mode": payload.mode}

    @app.get("/api/runs/{run_id}")
    def read_run(run_id: str) -> dict[str, Any]:
        meta = _require_run(run_id)
        return {
            "meta": meta,
            "inputs": store.read_inputs(run_id),
            "schema": store.read_schema(run_id),
            "result": store.read_result(run_id),
            "events": store.read_events(run_id),
            "active": manager.active_run == run_id,
        }

    @app.put("/api/runs/{run_id}/schema")
    def save_schema(run_id: str, payload: SaveSchemaRequest) -> dict[str, Any]:
        _require_run(run_id)
        issues = validate_result_schema(payload.result_schema)
        if issues:
            return {
                "saved": False,
                "issues": [issue.model_dump(mode="json") for issue in issues],
            }
        store.write_schema(run_id, payload.result_schema)
        return {"saved": True, "issues": []}

    @app.post("/api/runs/{run_id}/generate", status_code=201)
    async def generate_from_saved_schema(run_id: str) -> dict[str, Any]:
        _require_run(run_id)
        if manager.active_run is not None:
            raise HTTPException(
                status_code=409,
                detail=f"run {manager.active_run} is still in progress",
            )
        schema = store.read_schema(run_id)
        if schema is None:
            raise HTTPException(status_code=400, detail="run has no saved schema")
        outputs = store.read_inputs(run_id)
        try:
            request = TemplateRequest(
                command_outputs=outputs,
                result_schema=schema,
            )
        except ValueError as error:
            raise HTTPException(status_code=422, detail=str(error)) from error

        store.update_meta(
            run_id,
            status="running",
            mode="propose",
            stage="template",
            finished_at=None,
            termination_reason=None,
        )

        def factory(observer: Any) -> Any:
            return resolved.generate_from_schema(request, observer=observer)

        manager.start(run_id, factory)
        return {"run_id": run_id, "stage": "template"}

    @app.post("/api/runs/{run_id}/cancel")
    async def cancel_run(run_id: str) -> dict[str, Any]:
        _require_run(run_id)
        if manager.active_run != run_id:
            raise HTTPException(status_code=409, detail="run is not active")
        return {"cancelled": await manager.cancel()}

    @app.delete("/api/runs/{run_id}")
    def delete_run(run_id: str) -> dict[str, Any]:
        _require_run(run_id)
        if manager.active_run == run_id:
            raise HTTPException(status_code=409, detail="run is still in progress")
        return {"deleted": store.delete(run_id)}

    @app.get("/api/runs/{run_id}/events")
    async def stream_events(run_id: str) -> EventSourceResponse:
        _require_run(run_id)

        async def publisher() -> AsyncIterator[dict[str, Any]]:
            queue = manager.subscribe(run_id)
            try:
                # Replay what already happened so a late listener is not blind.
                for event in store.read_events(run_id):
                    yield {"data": _dumps(event)}
                if manager.active_run != run_id:
                    meta = store.read_meta(run_id) or {}
                    yield {
                        "data": _dumps(
                            {"type": "run.finished", "status": meta.get("status")},
                        ),
                    }
                    return
                while True:
                    item = await queue.get()
                    yield {"data": _dumps(item)}
                    if item.get("type") == "run.finished":
                        return
            finally:
                manager.unsubscribe(run_id, queue)

        return EventSourceResponse(publisher())

    if STATIC_DIRECTORY.is_dir():
        app.mount(
            "/static",
            StaticFiles(directory=STATIC_DIRECTORY),
            name="static",
        )

        @app.get("/")
        def index() -> FileResponse:
            return FileResponse(STATIC_DIRECTORY / "index.html")

    return app


def _dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False)


__all__ = ["CreateRunRequest", "RunManager", "SaveSchemaRequest", "create_app"]
