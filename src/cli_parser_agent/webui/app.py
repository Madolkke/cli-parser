"""FastAPI application for the local single-user WebUI.

The app owns one run at a time.  A generation is started in a background task
so the HTTP request returns immediately: a full run can take the whole default
900-second budget, which no browser request should ever hold open.
"""

from __future__ import annotations

import asyncio
import json
import time
from collections import deque
from collections.abc import AsyncIterator
from contextlib import suppress
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from sse_starlette.sse import EventSourceResponse

from .contracts import CreateRunRequest, SaveSchemaRequest, WebUIProgressEvent
from .service import GenerationService
from .store import RunStore, RunStoreError

STATIC_DIRECTORY = Path(__file__).resolve().parent / "static"


class _ProgressQueue:
    """Bounded async queue that coalesces adjacent text deltas."""

    MAX_ITEMS = 512
    MAX_DELTA_CHARS = 4096

    def __init__(self) -> None:
        self._items: deque[dict[str, Any]] = deque()
        self._ready = asyncio.Event()
        self._closed = False
        self._dropped_deltas = 0

    @staticmethod
    def _is_delta(item: dict[str, Any]) -> bool:
        return str(item.get("type", "")).endswith("_delta")

    @staticmethod
    def _delta_key(item: dict[str, Any]) -> tuple[Any, ...]:
        detail = item.get("detail") or {}
        return (
            item.get("type"),
            item.get("block_id"),
            item.get("tool_call_id"),
            detail.get("tool_name"),
        )

    def put_nowait(self, item: dict[str, Any]) -> None:
        if self._closed:
            return
        if self._is_delta(item) and self._items:
            previous = self._items[-1]
            if self._delta_key(previous) == self._delta_key(item):
                previous_detail = previous.setdefault("detail", {})
                current_detail = item.get("detail") or {}
                previous_text = str(previous_detail.get("text", ""))
                current_text = str(current_detail.get("text", ""))
                if len(previous_text) + len(current_text) <= self.MAX_DELTA_CHARS:
                    previous_detail["text"] = previous_text + current_text
                    previous_detail["coalesced"] = (
                        int(previous_detail.get("coalesced", 0)) + 1
                    )
                    return
        if len(self._items) >= self.MAX_ITEMS:
            if self._is_delta(item):
                self._dropped_deltas += 1
                return
            while self._items and len(self._items) >= self.MAX_ITEMS:
                if self._is_delta(self._items[0]):
                    self._items.popleft()
                    self._dropped_deltas += 1
                else:
                    break
            if len(self._items) >= self.MAX_ITEMS:
                return
        if self._dropped_deltas:
            item = dict(item)
            detail = dict(item.get("detail") or {})
            detail["dropped_delta_count"] = self._dropped_deltas
            item["detail"] = detail
            self._dropped_deltas = 0
        self._items.append(item)
        self._ready.set()

    def close(self) -> None:
        self._closed = True
        self._ready.set()

    async def get(self) -> dict[str, Any] | None:
        while not self._items:
            if self._closed:
                return None
            self._ready.clear()
            await self._ready.wait()
        item = self._items.popleft()
        if not self._items:
            self._ready.clear()
        return item

class RunManager:
    """Run one generation at a time and fan progress out to SSE listeners."""

    def __init__(self, store: RunStore) -> None:
        self.store = store
        self._task: asyncio.Task[None] | None = None
        self._active_run: str | None = None
        self._queues: dict[str, list[_ProgressQueue]] = {}
        self._sequences: dict[str, int] = {}

    @property
    def active_run(self) -> str | None:
        if self._task is not None and self._task.done():
            self._active_run = None
            self._task = None
        return self._active_run

    def subscribe(self, run_id: str) -> _ProgressQueue:
        queue = _ProgressQueue()
        self._queues.setdefault(run_id, []).append(queue)
        return queue

    def unsubscribe(self, run_id: str, queue: _ProgressQueue) -> None:
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

    def _number(self, run_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        self._sequences[run_id] = self._sequences.get(run_id, 0) + 1
        numbered = dict(payload)
        numbered["sequence"] = self._sequences[run_id]
        return numbered

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
        queue = _ProgressQueue()
        def observer(event: WebUIProgressEvent) -> None:
            queue.put_nowait(event)

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
            queue.put_nowait(
                {
                    "type": "run.finished",
                    "status": "cancelled",
                    "detail": {"reason": "cancelled"},
                },
            )
            raise
        except Exception as error:
            self.store.update_meta(
                run_id,
                status="failed",
                finished_at=_now(),
                elapsed_seconds=round(time.monotonic() - started, 3),
                termination_reason=f"exception:{type(error).__name__}",
            )
            queue.put_nowait(
                {
                    "type": "run.finished",
                    "status": "failed",
                    "detail": {"reason": f"exception:{type(error).__name__}"},
                },
            )
        else:
            payload = result
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
            queue.put_nowait(
                {
                    "type": "run.finished",
                    "status": payload.get("status"),
                    "detail": {
                        "reason": payload.get("metadata", {}).get("termination_reason"),
                    },
                },
            )
        finally:
            queue.close()
            await pump
            self._active_run = None
            self._sequences.pop(run_id, None)

    async def _pump(self, run_id: str, queue: _ProgressQueue) -> None:
        """Number, persist, and fan out progress events until the run ends."""

        while True:
            item = await queue.get()
            if item is None:
                return
            numbered = self._number(run_id, item)
            self.store.append_event(run_id, numbered)
            self._publish(run_id, numbered)

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
    service: GenerationService,
) -> FastAPI:
    """Build the WebUI application around a store and service boundary."""

    app = FastAPI(title="CLI Parser Agent", docs_url=None, redoc_url=None)
    manager = RunManager(store)
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
            command_outputs = service.validate_inputs(payload.command_outputs)
        except ValueError as error:
            raise HTTPException(status_code=422, detail=str(error)) from error

        run_id = store.create(
            mode=payload.mode,
            command_outputs=command_outputs,
            title=payload.title.strip(),
        )
        if payload.mode == "propose":
            def factory(observer: Any) -> Any:
                return service.run(
                    "propose",
                    payload.command_outputs,
                    observer=observer,
                )
        else:
            def factory(observer: Any) -> Any:
                return service.run(
                    "full",
                    payload.command_outputs,
                    observer=observer,
                )

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
        issues = service.validate_schema(payload.result_schema)
        if issues:
            return {
                "saved": False,
                "issues": issues,
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

        store.update_meta(
            run_id,
            status="running",
            mode="propose",
            stage="template",
            finished_at=None,
            termination_reason=None,
        )

        def factory(observer: Any) -> Any:
            return service.run_from_schema(
                outputs,
                schema,
                observer=observer,
            )

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
    async def stream_events(run_id: str, request: Request) -> EventSourceResponse:
        _require_run(run_id)
        try:
            last_event_id = int(request.headers.get("last-event-id", "0") or "0")
        except ValueError:
            last_event_id = 0

        async def publisher() -> AsyncIterator[dict[str, Any]]:
            queue = manager.subscribe(run_id)
            try:
                # Replay what already happened so a late listener is not blind.
                replayed_events = store.read_events(run_id)
                terminal_seen = False
                for event in replayed_events:
                    if int(event.get("sequence", 0) or 0) <= last_event_id:
                        if event.get("type") == "run.finished":
                            terminal_seen = True
                        continue
                    yield {
                        "id": str(event.get("sequence", "")),
                        "event": event.get("type", "message"),
                        "data": _dumps(event),
                    }
                    terminal_seen = terminal_seen or event.get("type") == "run.finished"
                # The terminal event can be persisted just before the manager
                # clears active_run, so never wait on a queue after replay has
                # already observed completion.
                if terminal_seen:
                    return
                if manager.active_run != run_id:
                    if not any(
                        event.get("type") == "run.finished"
                        for event in replayed_events
                    ):
                        meta = store.read_meta(run_id) or {}
                        terminal = manager._number(
                            run_id,
                            {"type": "run.finished", "status": meta.get("status")},
                        )
                        store.append_event(run_id, terminal)
                        if terminal["sequence"] > last_event_id:
                            yield {
                                "id": str(terminal["sequence"]),
                                "event": "run.finished",
                                "data": _dumps(terminal),
                            }
                    return
                while True:
                    item = await queue.get()
                    if item is None:
                        return
                    yield {
                        "id": str(item.get("sequence", "")),
                        "event": item.get("type", "message"),
                        "data": _dumps(item),
                    }
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
