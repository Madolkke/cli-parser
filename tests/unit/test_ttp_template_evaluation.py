"""Offline coverage for external-schema TTP-only evaluation definitions."""

from __future__ import annotations

import hashlib
import importlib.util
import json
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any

import pytest

from cli_parser_agent.evaluation import (
    HarnessError,
    load_ttp_template_manifest,
    score_ttp_template_output,
    select_ttp_template_cases,
)

SCRIPT_PATH = (
    Path(__file__).resolve().parents[2] / "scripts" / "run_ttp_template_evaluation.py"
)


def _load_script() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "run_ttp_template_evaluation",
        SCRIPT_PATH,
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _write_json(path: Path, value: Any) -> str:
    payload = json.dumps(value, ensure_ascii=False, indent=2).encode("utf-8")
    path.write_bytes(payload)
    return hashlib.sha256(payload).hexdigest()


def _manifest(tmp_path: Path) -> Path:
    schema = {
        "type": "object",
        "properties": {"value": {"type": "string"}},
        "required": ["value"],
        "additionalProperties": False,
    }
    schema_hash = _write_json(tmp_path / "schema.json", schema)
    first = tmp_path / "first.raw"
    second = tmp_path / "second.raw"
    first.write_text("value: one\n", encoding="utf-8")
    second.write_text("value: two\n", encoding="utf-8")
    expected_hash = _write_json(
        tmp_path / "expected.json",
        [{"value": "one"}, {"value": "two"}],
    )
    manifest = {
        "version": 1,
        "cases": [
            {
                "id": "example.values",
                "suites": ["smoke"],
                "tags": ["example"],
                "schema": {"path": "schema.json", "sha256": schema_hash},
                "inputs": [
                    {
                        "path": "first.raw",
                        "sha256": hashlib.sha256(first.read_bytes()).hexdigest(),
                    },
                    {
                        "path": "second.raw",
                        "sha256": hashlib.sha256(second.read_bytes()).hexdigest(),
                    },
                ],
                "expected_records": {
                    "path": "expected.json",
                    "sha256": expected_hash,
                },
            },
        ],
    }
    _write_json(tmp_path / "manifest.json", manifest)
    return tmp_path / "manifest.json"


def test_external_ttp_manifest_loads_and_selects_multi_input_case(
    tmp_path: Path,
) -> None:
    manifest = load_ttp_template_manifest(_manifest(tmp_path))

    assert manifest.version == 1
    assert len(manifest.cases) == 1
    case = manifest.cases[0]
    assert len(case.inputs) == 2
    assert case.target.records == ({"value": "one"}, {"value": "two"})
    assert select_ttp_template_cases(manifest, suite="smoke", case_ids=()) == (
        case,
    )
    assert select_ttp_template_cases(
        manifest,
        suite=None,
        case_ids=[case.id],
    ) == (case,)

    raw = json.loads((tmp_path / "manifest.json").read_text(encoding="utf-8"))
    del raw["cases"][0]["tags"]
    _write_json(tmp_path / "manifest.json", raw)
    assert load_ttp_template_manifest(tmp_path / "manifest.json").cases[0].tags == ()


def test_external_ttp_manifest_rejects_invalid_expected_records(
    tmp_path: Path,
) -> None:
    manifest_path = _manifest(tmp_path)
    expected_path = tmp_path / "expected.json"
    expected_hash = _write_json(expected_path, [{"wrong": "one"}, {"value": "two"}])
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["cases"][0]["expected_records"]["sha256"] = expected_hash
    _write_json(manifest_path, manifest)

    with pytest.raises(HarnessError, match="do not satisfy"):
        load_ttp_template_manifest(manifest_path)


def test_external_ttp_manifest_accepts_five_inputs_and_rejects_six(
    tmp_path: Path,
) -> None:
    manifest_path = _manifest(tmp_path)
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    inputs = manifest["cases"][0]["inputs"]
    expected = [{"value": "one"}, {"value": "two"}]
    for index in range(3, 6):
        path = tmp_path / f"input-{index}.raw"
        value = f"value: {index}\n"
        path.write_text(value, encoding="utf-8")
        inputs.append(
            {
                "path": path.name,
                "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            },
        )
        expected.append({"value": str(index)})
    expected_hash = _write_json(tmp_path / "expected.json", expected)
    manifest["cases"][0]["expected_records"]["sha256"] = expected_hash
    _write_json(manifest_path, manifest)
    assert len(load_ttp_template_manifest(manifest_path).cases[0].inputs) == 5

    path = tmp_path / "input-6.raw"
    path.write_text("value: 6\n", encoding="utf-8")
    inputs.append(
        {
            "path": path.name,
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        },
    )
    _write_json(manifest_path, manifest)
    with pytest.raises(HarnessError, match="1 to 5"):
        load_ttp_template_manifest(manifest_path)


def test_external_ttp_manifest_rejects_traversal_and_duplicate_keys(
    tmp_path: Path,
) -> None:
    manifest_path = _manifest(tmp_path)
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["cases"][0]["schema"]["path"] = "../schema.json"
    _write_json(manifest_path, manifest)
    with pytest.raises(HarnessError, match="traversal"):
        load_ttp_template_manifest(manifest_path)

    manifest_path.write_text(
        '{"version": 1, "version": 1, "cases": []}',
        encoding="utf-8",
    )
    with pytest.raises(HarnessError, match="duplicate"):
        load_ttp_template_manifest(manifest_path)


def test_ttp_template_score_requires_generation_acceptance_and_exact_records() -> None:
    output = {
        "generation_result": {
            "status": "success",
            "artifact": {"records": [{"value": "one"}]},
            "metadata": {
                "termination_reason": "success",
                "first_ttp_passed": True,
                "ttp_submissions": 1,
            },
        },
        "independent_acceptance": {"valid": True},
    }
    score = score_ttp_template_output(output, [{"value": "one"}])
    assert score["metrics"]["candidate_pass"] == 1.0
    assert score["metrics"]["records_exact_match"] == 1.0

    changed = score_ttp_template_output(output, [{"value": "two"}])
    assert changed["metrics"]["candidate_pass"] == 0.0
    assert changed["metrics"]["leaf_f1"] == 0.0


def test_list_and_preflight_do_not_require_model_configuration(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    script = _load_script()
    manifest_path = _manifest(tmp_path)
    monkeypatch.setattr(script._run_support, "flush_laminar", lambda: True)
    monkeypatch.setattr(
        script,
        "_configuration",
        lambda: (_ for _ in ()).throw(AssertionError("model configuration read")),
    )

    assert script.main(["list", "--manifest", str(manifest_path)]) == 0
    assert script.main(["preflight", "--manifest", str(manifest_path)]) == 0


def test_run_writes_complete_per_case_artifacts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    script = _load_script()
    manifest_path = _manifest(tmp_path)
    artifact_root = tmp_path / "artifacts"
    monkeypatch.setattr(script._run_support, "flush_laminar", lambda: True)
    monkeypatch.setattr(
        script,
        "_configuration",
        lambda: (SimpleNamespace(), SimpleNamespace(), artifact_root, {"model": {}}),
    )

    async def fake_trial(case: Any, settings: Any, policy: Any) -> dict[str, Any]:
        del settings, policy
        return {
            "generation_result": {
                "status": "success",
                "artifact": {"records": list(case.target.records)},
                "metadata": {"laminar_trace_id": "trace-id"},
            },
            "independent_acceptance": {"valid": True},
            "score": {
                "metrics": {
                    "candidate_pass": 1.0,
                    "records_exact_match": 1.0,
                },
                "inputs": [],
            },
            "exception_type": None,
        }

    monkeypatch.setattr(script, "_run_trial", fake_trial)
    result = script.main(
        ["run", "--manifest", str(manifest_path), "--suite", "smoke"],
    )

    assert result == 0
    run_directory = next(artifact_root.iterdir())
    assert (run_directory / "summary.json").is_file()
    case_files = list((run_directory / "cases").glob("*.json"))
    assert len(case_files) == 1
    payload = json.loads(case_files[0].read_text(encoding="utf-8"))
    assert payload["generation_result"]["artifact"]["records"] == [
        {"value": "one"},
        {"value": "two"},
    ]
    assert payload["case"]["schema"]["path"] == "schema.json"
