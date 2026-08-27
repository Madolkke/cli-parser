"""File-backed run storage for the local WebUI.

Every run is one UTC-stamped directory under ``data/runs``.  The directory name
is the run id and sorts chronologically, so listing history is a directory scan
with no index file to keep in sync.
"""

from __future__ import annotations

import json
import re
import shutil
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

RUN_ID_PATTERN = re.compile(r"^\d{8}T\d{6}\.\d{6}Z$")

META_FILE = "meta.json"
INPUTS_FILE = "inputs.json"
SCHEMA_FILE = "schema.json"
RESULT_FILE = "result.json"
EVENTS_FILE = "events.jsonl"
CONFIG_FILE = "config.json"


class RunStoreError(ValueError):
    """A run id or run directory is not usable."""


def new_run_id() -> str:
    """Return a fresh chronologically sortable run id."""

    return datetime.now(UTC).strftime("%Y%m%dT%H%M%S.%fZ")


def write_json(path: Path, value: Any) -> None:
    """Write deterministic, human-readable UTF-8 JSON."""

    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
        newline="\n",
    )


def read_json(path: Path) -> Any:
    """Read one UTF-8 JSON document, or return ``None`` when absent."""

    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


class RunStore:
    """Create, read, and delete run directories under one root."""

    def __init__(self, root: Path) -> None:
        self.root = Path(root)

    @property
    def runs_root(self) -> Path:
        return self.root / "runs"

    def run_directory(self, run_id: str) -> Path:
        """Resolve one run directory, rejecting anything outside the root.

        The id is pattern-checked first and the resolved path is then confirmed
        to still sit under ``runs_root``, so a crafted id cannot reach an
        unrelated directory.
        """

        if not RUN_ID_PATTERN.fullmatch(run_id):
            raise RunStoreError(f"invalid run id: {run_id!r}")
        runs_root = self.runs_root.resolve()
        candidate = (runs_root / run_id).resolve()
        if candidate != runs_root / run_id or runs_root not in candidate.parents:
            raise RunStoreError(f"run id escapes the run root: {run_id!r}")
        return candidate

    def create(
        self,
        *,
        mode: str,
        command_outputs: list[str],
        title: str,
        config: dict[str, Any] | None = None,
    ) -> str:
        """Create one run directory and seed its metadata and inputs.

        Two runs started within the same clock tick would otherwise collide:
        the timestamp only has microsecond resolution and some platforms report
        it coarsely.  The stamp is advanced until the directory is free, which
        keeps ids unique, still sortable, and still matching ``RUN_ID_PATTERN``.
        """

        self.runs_root.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(UTC)
        while True:
            run_id = stamp.strftime("%Y%m%dT%H%M%S.%fZ")
            directory = self.runs_root / run_id
            try:
                directory.mkdir(exist_ok=False)
            except FileExistsError:
                stamp += timedelta(microseconds=1)
                continue
            break

        started_at = datetime.now(UTC).isoformat()
        write_json(
            directory / META_FILE,
            {
                "run_id": run_id,
                "mode": mode,
                "status": "running",
                "title": title,
                "created_at": started_at,
                "started_at": started_at,
                "finished_at": None,
                "elapsed_seconds": None,
                "termination_reason": None,
                "command_output_count": len(command_outputs),
            },
        )
        write_json(directory / INPUTS_FILE, {"command_outputs": command_outputs})
        if config is not None:
            self.write_config(run_id, config)
        return run_id

    def create_schema_rerun(
        self,
        *,
        source_run_id: str,
        schema: dict[str, Any],
        schema_source: str,
        command_outputs: list[str],
        title: str,
        config: dict[str, Any] | None = None,
    ) -> str:
        """Create an independent TTP-only run from a completed run's Schema."""

        run_id = self.create(
            mode="schema_rerun",
            command_outputs=command_outputs,
            title=title,
            config=config,
        )
        self.write_schema(run_id, schema)
        self.update_meta(
            run_id,
            execution_kind="schema_rerun",
            source_run_id=source_run_id,
            schema_source=schema_source,
        )
        return run_id

    def read_meta(self, run_id: str) -> dict[str, Any] | None:
        meta = read_json(self.run_directory(run_id) / META_FILE)
        return meta if isinstance(meta, dict) else None

    def update_meta(self, run_id: str, **fields: Any) -> dict[str, Any]:
        """Merge fields into one run's metadata."""

        directory = self.run_directory(run_id)
        meta = read_json(directory / META_FILE)
        if not isinstance(meta, dict):
            raise RunStoreError(f"run has no readable metadata: {run_id!r}")
        meta.update(fields)
        write_json(directory / META_FILE, meta)
        return meta

    def read_inputs(self, run_id: str) -> list[str]:
        payload = read_json(self.run_directory(run_id) / INPUTS_FILE)
        if isinstance(payload, dict):
            outputs = payload.get("command_outputs")
            if isinstance(outputs, list):
                return [item for item in outputs if isinstance(item, str)]
        return []

    def write_schema(self, run_id: str, schema: dict[str, Any]) -> None:
        write_json(self.run_directory(run_id) / SCHEMA_FILE, schema)

    def read_schema(self, run_id: str) -> dict[str, Any] | None:
        schema = read_json(self.run_directory(run_id) / SCHEMA_FILE)
        return schema if isinstance(schema, dict) else None

    def write_result(self, run_id: str, result: dict[str, Any]) -> None:
        write_json(self.run_directory(run_id) / RESULT_FILE, result)

    def read_result(self, run_id: str) -> dict[str, Any] | None:
        result = read_json(self.run_directory(run_id) / RESULT_FILE)
        return result if isinstance(result, dict) else None

    def write_config(self, run_id: str, config: dict[str, Any]) -> None:
        """Persist the exact resolved WebUI configuration for local replay."""

        write_json(self.run_directory(run_id) / CONFIG_FILE, config)

    def read_config(self, run_id: str) -> dict[str, Any] | None:
        config = read_json(self.run_directory(run_id) / CONFIG_FILE)
        return config if isinstance(config, dict) else None

    def read_config_status(
        self,
        run_id: str,
    ) -> tuple[dict[str, Any] | None, bool]:
        """Read a config and distinguish a missing snapshot from a corrupt one."""

        path = self.run_directory(run_id) / CONFIG_FILE
        if not path.is_file():
            return None, False
        config = read_json(path)
        return (config if isinstance(config, dict) else None), True

    def append_event(self, run_id: str, event: dict[str, Any]) -> None:
        """Append one sanitized, size-bounded WebUI event to the transcript."""

        path = self.run_directory(run_id) / EVENTS_FILE
        line = json.dumps(event, ensure_ascii=False) + "\n"
        with path.open("a", encoding="utf-8", newline="\n") as stream:
            stream.write(line)

    def read_events(self, run_id: str) -> list[dict[str, Any]]:
        path = self.run_directory(run_id) / EVENTS_FILE
        if not path.is_file():
            return []
        events: list[dict[str, Any]] = []
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                value = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(value, dict):
                events.append(value)
        return events

    def list_runs(self) -> list[dict[str, Any]]:
        """Return run summaries, newest first."""

        if not self.runs_root.is_dir():
            return []
        summaries: list[dict[str, Any]] = []
        for entry in sorted(self.runs_root.iterdir(), reverse=True):
            if not entry.is_dir() or not RUN_ID_PATTERN.fullmatch(entry.name):
                continue
            meta = read_json(entry / META_FILE)
            if isinstance(meta, dict):
                summaries.append(meta)
        return summaries

    def delete(self, run_id: str) -> bool:
        directory = self.run_directory(run_id)
        if not directory.is_dir():
            return False
        shutil.rmtree(directory)
        return True


__all__ = [
    "EVENTS_FILE",
    "INPUTS_FILE",
    "META_FILE",
    "RESULT_FILE",
    "RUN_ID_PATTERN",
    "SCHEMA_FILE",
    "RunStore",
    "RunStoreError",
    "new_run_id",
    "read_json",
    "write_json",
]
