"""Deterministic tests for the WebUI's file-backed run storage."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from cli_parser_agent.webui.store import RunStore, RunStoreError


def _store(tmp_path: Path) -> RunStore:
    return RunStore(tmp_path / "data")


def test_create_seeds_metadata_and_inputs(tmp_path: Path) -> None:
    store = _store(tmp_path)

    run_id = store.create(
        mode="full",
        command_outputs=["Interface  Status", "second"],
        title="show ip int brief",
    )

    meta = store.read_meta(run_id)
    assert meta is not None
    assert meta["run_id"] == run_id
    assert meta["mode"] == "full"
    assert meta["status"] == "running"
    assert meta["title"] == "show ip int brief"
    assert meta["command_output_count"] == 2
    assert meta["finished_at"] is None
    assert store.read_inputs(run_id) == ["Interface  Status", "second"]

    directory = store.run_directory(run_id)
    assert (directory / "meta.json").is_file()
    assert (directory / "inputs.json").is_file()


def test_run_ids_sort_chronologically_and_list_is_newest_first(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)

    first = store.create(mode="full", command_outputs=["a"], title="first")
    second = store.create(mode="propose", command_outputs=["b"], title="second")

    assert first < second
    assert [meta["run_id"] for meta in store.list_runs()] == [second, first]


def test_list_runs_ignores_unrelated_directories(tmp_path: Path) -> None:
    store = _store(tmp_path)
    run_id = store.create(mode="full", command_outputs=["a"], title="kept")
    (store.runs_root / "not-a-run").mkdir()
    (store.runs_root / "20260101T000000.000000Z").mkdir()  # no meta.json

    assert [meta["run_id"] for meta in store.list_runs()] == [run_id]


def test_list_runs_is_empty_before_any_run(tmp_path: Path) -> None:
    assert _store(tmp_path).list_runs() == []


@pytest.mark.parametrize(
    "run_id",
    [
        "../escape",
        "..",
        "x/../../y",
        "20260101T000000.000000Z/../..",
        "20260101T000000.000000Z/child",
        "not-a-run-id",
        "",
        "20260101T000000Z",
        "20260101T000000.000000Z ",
    ],
)
def test_run_directory_rejects_ids_outside_the_run_root(
    tmp_path: Path,
    run_id: str,
) -> None:
    with pytest.raises(RunStoreError):
        _store(tmp_path).run_directory(run_id)


def test_schema_result_and_events_round_trip(tmp_path: Path) -> None:
    store = _store(tmp_path)
    run_id = store.create(mode="propose", command_outputs=["a"], title="")

    assert store.read_schema(run_id) is None
    assert store.read_result(run_id) is None
    assert store.read_events(run_id) == []

    store.write_schema(run_id, {"type": "object"})
    store.write_result(run_id, {"status": "success"})
    store.append_event(run_id, {"type": "one"})
    store.append_event(run_id, {"type": "two"})

    assert store.read_schema(run_id) == {"type": "object"}
    assert store.read_result(run_id) == {"status": "success"}
    assert [event["type"] for event in store.read_events(run_id)] == ["one", "two"]


def test_runtime_config_snapshot_round_trips_with_the_run(tmp_path: Path) -> None:
    store = _store(tmp_path)
    config = {
        "version": 1,
        "source": "env_baseline+overrides",
        "settings": {"api_key": "local-key", "model_name": "run-model"},
        "policy": {"max_agent_rounds": 24},
    }

    run_id = store.create(
        mode="full",
        command_outputs=["a"],
        title="snapshot",
        config=config,
    )

    assert store.read_config(run_id) == config
    assert (store.run_directory(run_id) / "config.json").is_file()

    assert store.delete(run_id) is True
    assert not (store.runs_root / run_id).exists()


def test_runtime_config_status_distinguishes_corrupt_snapshot(tmp_path: Path) -> None:
    store = _store(tmp_path)
    run_id = store.create(mode="full", command_outputs=["a"], title="snapshot")
    (store.run_directory(run_id) / "config.json").write_text(
        "not json\n",
        encoding="utf-8",
    )

    config, present = store.read_config_status(run_id)

    assert config is None
    assert present is True


def test_create_schema_rerun_copies_schema_and_records_its_source(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    source_run_id = store.create(
        mode="propose",
        command_outputs=["a"],
        title="source",
    )
    schema = {"type": "object", "properties": {}, "additionalProperties": False}

    child_run_id = store.create_schema_rerun(
        source_run_id=source_run_id,
        schema=schema,
        schema_source="saved_schema",
        command_outputs=["a"],
        title="source · Schema 重新生成",
    )

    meta = store.read_meta(child_run_id)
    assert meta is not None
    assert meta["mode"] == "schema_rerun"
    assert meta["execution_kind"] == "schema_rerun"
    assert meta["source_run_id"] == source_run_id
    assert meta["schema_source"] == "saved_schema"
    assert store.read_schema(child_run_id) == schema
    assert store.read_inputs(child_run_id) == ["a"]
    assert store.read_result(child_run_id) is None
    assert store.read_events(child_run_id) == []


def test_read_events_skips_corrupt_lines(tmp_path: Path) -> None:
    store = _store(tmp_path)
    run_id = store.create(mode="full", command_outputs=["a"], title="")
    path = store.run_directory(run_id) / "events.jsonl"
    path.write_text('{"type": "ok"}\nnot json\n\n[1, 2]\n', encoding="utf-8")

    assert [event["type"] for event in store.read_events(run_id)] == ["ok"]


def test_update_meta_merges_without_dropping_existing_fields(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    run_id = store.create(mode="propose", command_outputs=["a"], title="kept")

    merged = store.update_meta(run_id, status="success", elapsed_seconds=1.25)

    assert merged["status"] == "success"
    assert merged["elapsed_seconds"] == 1.25
    assert merged["mode"] == "propose"
    assert merged["title"] == "kept"
    assert store.read_meta(run_id) == merged


def test_delete_removes_the_directory_and_is_idempotent(tmp_path: Path) -> None:
    store = _store(tmp_path)
    run_id = store.create(mode="full", command_outputs=["a"], title="")

    assert store.delete(run_id) is True
    assert store.delete(run_id) is False
    assert store.read_meta(run_id) is None


def test_written_json_is_utf8_and_human_readable(tmp_path: Path) -> None:
    store = _store(tmp_path)
    run_id = store.create(mode="full", command_outputs=["接口 状态"], title="中文")

    raw = (store.run_directory(run_id) / "inputs.json").read_text(encoding="utf-8")

    # Not escaped to \uXXXX, and indented for human inspection.
    assert "接口 状态" in raw
    assert raw.endswith("\n")
    assert json.loads(raw)["command_outputs"] == ["接口 状态"]
