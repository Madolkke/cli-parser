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

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from sse_starlette.sse import EventSourceResponse

from .contracts import (
    CreateRunRequest,
    RerunRunRequest,
    SaveSchemaRequest,
    WebUIProgressEvent,
)
from .runtime_config import (
    RuntimeConfigError,
    RuntimeParameters,
    full_config_payload,
    public_config_snapshot,
)
from .service import GenerationService
from .store import RunStore, RunStoreError

STATIC_DIRECTORY = Path(__file__).resolve().parent / "static"
TERMINAL_RUN_STATUSES = frozenset({"success", "failed", "cancelled"})


class _ProgressQueue:
    """Bounded async queue with time- and size-based delta coalescing."""

    MAX_ITEMS = 512
    MAX_DELTA_CHARS = 4096
    DELTA_WINDOW_SECONDS = 0.05

    def __init__(
        self,
        *,
        coalesce_window_seconds: float = DELTA_WINDOW_SECONDS,
        max_items: int = MAX_ITEMS,
        max_delta_chars: int = MAX_DELTA_CHARS,
    ) -> None:
        if coalesce_window_seconds < 0:
            raise ValueError("coalesce_window_seconds cannot be negative")
        if max_items < 1 or max_delta_chars < 1:
            raise ValueError("queue limits must be positive")
        self._items: deque[dict[str, Any]] = deque()
        self._ready = asyncio.Event()
        self._closed = False
        self._dropped_deltas = 0
        self._pending_delta: dict[str, Any] | None = None
        self._flush_handle: asyncio.TimerHandle | None = None
        self._coalesce_window_seconds = coalesce_window_seconds
        self._max_items = max_items
        self._max_delta_chars = max_delta_chars

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

    @staticmethod
    def _delta_text(item: dict[str, Any]) -> str:
        return str((item.get("detail") or {}).get("text", ""))

    def _enqueue(self, item: dict[str, Any]) -> None:
        if len(self._items) >= self._max_items:
            if self._is_delta(item):
                self._dropped_deltas += 1
                return
            while len(self._items) >= self._max_items:
                delta_index = next(
                    (
                        index
                        for index, queued in enumerate(self._items)
                        if self._is_delta(queued)
                    ),
                    None,
                )
                if delta_index is None:
                    # Structural events are sparse and must never be dropped.
                    break
                del self._items[delta_index]
                self._dropped_deltas += 1
        if self._dropped_deltas:
            item = dict(item)
            detail = dict(item.get("detail") or {})
            detail["dropped_delta_count"] = self._dropped_deltas
            item["detail"] = detail
            self._dropped_deltas = 0
        self._items.append(item)
        self._ready.set()

    def _cancel_flush(self) -> None:
        if self._flush_handle is not None:
            self._flush_handle.cancel()
            self._flush_handle = None

    def _schedule_flush(self) -> None:
        if self._coalesce_window_seconds <= 0:
            self._flush_pending()
            return
        if self._flush_handle is None:
            loop = asyncio.get_running_loop()
            self._flush_handle = loop.call_later(
                self._coalesce_window_seconds,
                self._flush_pending,
            )

    def _flush_pending(self) -> None:
        self._cancel_flush()
        if self._pending_delta is None:
            return
        pending = self._pending_delta
        self._pending_delta = None
        self._enqueue(pending)

    def _start_pending(self, item: dict[str, Any], text: str) -> None:
        pending = dict(item)
        detail = dict(item.get("detail") or {})
        detail["text"] = text
        pending["detail"] = detail
        self._pending_delta = pending
        self._schedule_flush()

    def _merge_pending(self, item: dict[str, Any], text: str) -> bool:
        pending = self._pending_delta
        if pending is None or self._delta_key(pending) != self._delta_key(item):
            return False
        pending_detail = pending.setdefault("detail", {})
        previous_text = str(pending_detail.get("text", ""))
        if len(previous_text) + len(text) > self._max_delta_chars:
            return False
        pending_detail["text"] = previous_text + text
        current_detail = item.get("detail") or {}
        pending_detail["coalesced"] = (
            int(pending_detail.get("coalesced", 0))
            + int(current_detail.get("coalesced", 0))
            + 1
        )
        for key in ("phase", "elapsed_seconds", "round_index"):
            if item.get(key) is not None:
                pending[key] = item[key]
        return True

    def _put_delta(self, item: dict[str, Any]) -> None:
        text = self._delta_text(item)
        if not text:
            if self._pending_delta is not None and self._delta_key(
                self._pending_delta,
            ) != self._delta_key(item):
                self._flush_pending()
            if self._pending_delta is None:
                self._start_pending(item, "")
            return
        offset = 0
        while offset < len(text):
            pending_text = (
                self._delta_text(self._pending_delta)
                if self._pending_delta is not None
                else ""
            )
            available = self._max_delta_chars - len(pending_text)
            if (
                self._pending_delta is None
                or self._delta_key(self._pending_delta) != self._delta_key(item)
                or available <= 0
            ):
                self._flush_pending()
                chunk = text[offset : offset + self._max_delta_chars]
                self._start_pending(item, chunk)
                offset += len(chunk)
                continue
            chunk = text[offset : offset + available]
            if not self._merge_pending(item, chunk):
                self._flush_pending()
                continue
            offset += len(chunk)
            if len(self._delta_text(self._pending_delta)) >= self._max_delta_chars:
                self._flush_pending()

    def put_nowait(self, item: dict[str, Any]) -> None:
        if self._closed:
            return
        if self._is_delta(item):
            self._put_delta(item)
            return
        self._flush_pending()
        self._enqueue(item)

    def close(self) -> None:
        self._flush_pending()
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
        self._active_start_sequences: dict[str, int] = {}

    @property
    def active_run(self) -> str | None:
        if self._task is not None and self._task.done():
            self._active_run = None
            self._task = None
        return self._active_run

    def subscribe(self, run_id: str) -> _ProgressQueue:
        # Deltas are already coalesced before persistence and publication.
        queue = _ProgressQueue(coalesce_window_seconds=0)
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

    def _stored_sequence(self, run_id: str) -> int:
        return max(
            (
                int(event.get("sequence", 0) or 0)
                for event in self.store.read_events(run_id)
            ),
            default=0,
        )

    def active_start_sequence(self, run_id: str) -> int | None:
        return self._active_start_sequences.get(run_id)

    def start(self, run_id: str, coroutine_factory: Any) -> None:
        """Start one background run, rejecting a second concurrent request."""

        if self.active_run is not None:
            raise HTTPException(
                status_code=409,
                detail=f"run {self.active_run} is still in progress",
            )
        stored_sequence = self._stored_sequence(run_id)
        self._sequences[run_id] = stored_sequence
        self._active_start_sequences[run_id] = stored_sequence
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
            self._active_start_sequences.pop(run_id, None)
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


def _schema_for_rerun(
    store: RunStore,
    run_id: str,
) -> tuple[dict[str, Any], str] | None:
    """Return the reviewed Schema first, then a completed artifact Schema."""

    saved_schema = store.read_schema(run_id)
    if saved_schema is not None:
        return saved_schema, "saved_schema"
    result = store.read_result(run_id)
    artifact = result.get("artifact") if isinstance(result, dict) else None
    artifact_schema = (
        artifact.get("result_schema") if isinstance(artifact, dict) else None
    )
    if isinstance(artifact_schema, dict):
        return artifact_schema, "artifact_schema"
    return None


def _schema_rerun_title(meta: dict[str, Any]) -> str:
    """Derive a bounded, locally meaningful title for a rerun child."""

    source_title = str(meta.get("title", "")).strip()
    suffix = "Schema 重新生成"
    if not source_title:
        return suffix
    return f"{source_title[: 200 - len(suffix) - 3]} · {suffix}"


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

    def _public_run_config(
        run_id: str,
    ) -> tuple[dict[str, Any] | None, str | None]:
        raw_config, present = store.read_config_status(run_id)
        if not present:
            return None, None
        if raw_config is None:
            return None, "runtime configuration snapshot is corrupt"
        try:
            return public_config_snapshot(raw_config), None
        except (KeyError, TypeError, ValueError):
            return None, "runtime configuration snapshot is invalid"

    @app.get("/api/runs")
    def list_runs() -> dict[str, Any]:
        return {"runs": store.list_runs(), "active_run": manager.active_run}

    @app.get("/api/runtime-config")
    def runtime_config() -> dict[str, Any]:
        return service.public_runtime_config()

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
        try:
            runtime_config = service.resolve_runtime_config(payload.parameters)
        except RuntimeConfigError as error:
            raise HTTPException(status_code=422, detail=str(error)) from error
        public_config = public_config_snapshot(full_config_payload(runtime_config))

        run_id = store.create(
            mode=payload.mode,
            command_outputs=command_outputs,
            title=payload.title.strip(),
            config=full_config_payload(runtime_config),
        )
        store.update_meta(
            run_id,
            runtime_config_source=runtime_config.source,
            runtime_model_name=runtime_config.settings.model_name,
            runtime_configuration_fingerprint=public_config[
                "configuration_fingerprint"
            ],
        )
        if payload.mode == "propose":
            def factory(observer: Any) -> Any:
                return service.run(
                    "propose",
                    command_outputs,
                    observer=observer,
                    runtime_config=runtime_config,
                )
        else:
            def factory(observer: Any) -> Any:
                return service.run(
                    "full",
                    command_outputs,
                    observer=observer,
                    runtime_config=runtime_config,
                )

        manager.start(run_id, factory)
        return {"run_id": run_id, "mode": payload.mode}

    @app.get("/api/runs/{run_id}")
    def read_run(run_id: str) -> dict[str, Any]:
        meta = _require_run(run_id)
        config, config_error = _public_run_config(run_id)
        return {
            "meta": meta,
            "inputs": store.read_inputs(run_id),
            "schema": store.read_schema(run_id),
            "result": store.read_result(run_id),
            "config": config,
            "config_error": config_error,
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

    async def _start_schema_rerun(
        run_id: str,
        parameters: RuntimeParameters | None = None,
    ) -> dict[str, Any]:
        source_meta = _require_run(run_id)
        if manager.active_run is not None:
            raise HTTPException(
                status_code=409,
                detail=f"run {manager.active_run} is still in progress",
            )
        if source_meta.get("status") not in TERMINAL_RUN_STATUSES:
            raise HTTPException(status_code=409, detail="source run is not finished")
        schema_info = _schema_for_rerun(store, run_id)
        if schema_info is None:
            raise HTTPException(
                status_code=400,
                detail="source run has no usable schema",
            )
        schema, schema_source = schema_info
        if service.validate_schema(schema):
            raise HTTPException(status_code=400, detail="source run schema is invalid")
        try:
            outputs = service.validate_inputs(store.read_inputs(run_id))
        except ValueError as error:
            raise HTTPException(
                status_code=400,
                detail="source run inputs are invalid",
            ) from error
        try:
            runtime_config = service.resolve_runtime_config(parameters)
        except RuntimeConfigError as error:
            raise HTTPException(status_code=422, detail=str(error)) from error
        full_config = full_config_payload(runtime_config)
        public_config = public_config_snapshot(full_config)

        child_run_id = store.create_schema_rerun(
            source_run_id=run_id,
            schema=schema,
            schema_source=schema_source,
            command_outputs=outputs,
            title=_schema_rerun_title(source_meta),
            config=full_config,
        )
        store.update_meta(
            child_run_id,
            runtime_config_source=runtime_config.source,
            runtime_model_name=runtime_config.settings.model_name,
            runtime_configuration_fingerprint=public_config[
                "configuration_fingerprint"
            ],
        )

        def factory(observer: Any) -> Any:
            return service.run_from_schema(
                outputs,
                schema,
                observer=observer,
                runtime_config=runtime_config,
            )

        manager.start(child_run_id, factory)
        return {
            "run_id": child_run_id,
            "source_run_id": run_id,
            "stage": "template",
        }

    @app.post("/api/runs/{run_id}/rerun", status_code=201)
    async def rerun_from_schema(
        run_id: str,
        payload: RerunRunRequest | None = None,
    ) -> dict[str, Any]:
        """Start an independent TTP-only child run from a stored Schema."""

        return await _start_schema_rerun(
            run_id,
            payload.parameters if payload is not None else None,
        )

    @app.post("/api/runs/{run_id}/generate", status_code=201)
    async def generate_from_saved_schema(
        run_id: str,
        payload: RerunRunRequest | None = None,
    ) -> dict[str, Any]:
        """Compatibility alias for the independent Schema rerun endpoint."""

        return await _start_schema_rerun(
            run_id,
            payload.parameters if payload is not None else None,
        )

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
    async def stream_events(
        run_id: str,
        request: Request,
        after_sequence: int = Query(default=0, ge=0),
    ) -> EventSourceResponse:
        _require_run(run_id)
        try:
            header_sequence = int(
                request.headers.get("last-event-id", "0") or "0",
            )
        except ValueError:
            header_sequence = 0
        last_event_id = max(after_sequence, header_sequence)

        async def publisher() -> AsyncIterator[dict[str, Any]]:
            queue = manager.subscribe(run_id)
            try:
                # Replay what already happened so a late listener is not blind.
                replayed_events = store.read_events(run_id)
                terminal_seen = False
                active_start_sequence = manager.active_start_sequence(run_id)
                for event in replayed_events:
                    event_sequence = int(event.get("sequence", 0) or 0)
                    stale_terminal = (
                        event.get("type") == "run.finished"
                        and active_start_sequence is not None
                        and event_sequence <= active_start_sequence
                    )
                    if event_sequence <= last_event_id or stale_terminal:
                        if event.get("type") == "run.finished" and not stale_terminal:
                            terminal_seen = True
                        continue
                    yield {
                        "id": str(event.get("sequence", "")),
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
                                "data": _dumps(terminal),
                            }
                    return
                while True:
                    item = await queue.get()
                    if item is None:
                        return
                    yield {
                        "id": str(item.get("sequence", "")),
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


__all__ = [
    "CreateRunRequest",
    "RunManager",
    "RerunRunRequest",
    "SaveSchemaRequest",
    "create_app",
]
