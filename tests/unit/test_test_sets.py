from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from cli_parser_agent.evaluation import (
    HarnessError,
    load_test_set_manifest,
    select_test_sets,
)


def _write_fixture(
    root: Path,
    *,
    input_count: int = 2,
    suites: list[str] | None = None,
) -> Path:
    case_dir = root / "demo.case"
    inputs_dir = case_dir / "inputs"
    inputs_dir.mkdir(parents=True)
    input_payloads: dict[int, bytes] = {}
    for index in range(1, input_count + 1):
        path = inputs_dir / f"{index:03d}.txt"
        path.write_text(f"value-{index}\n", encoding="utf-8")
        input_payloads[index] = path.read_bytes()
    schema = {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "type": "object",
        "properties": {
            "lines": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {"text": {"type": "string"}},
                    "required": ["text"],
                    "additionalProperties": False,
                },
            },
        },
        "required": ["lines"],
        "additionalProperties": False,
    }
    template = (
        '<group name="lines*" method="table">\n'
        '{{ text | re("[^\\r\\n]+") }}\n'
        '</group>\n'
    )
    expected = [
        {"lines": [{"text": f"value-{index}"}]}
        for index in range(1, input_count + 1)
    ]
    files = {
        "schema.json": json.dumps(schema, indent=2).encode() + b"\n",
        "template.ttp": template.encode(),
        "expected.json": json.dumps(expected, indent=2).encode() + b"\n",
    }
    for name, payload in files.items():
        (case_dir / name).write_bytes(payload)
    manifest = {
        "version": 1,
        "cases": [
            {
                "id": "demo.case",
                "path": "demo.case",
                "command": "show demo",
                "suites": suites or ["smoke", "all"],
                "tags": ["fixture"],
                "files": {
                    "schema": {
                        "sha256": hashlib.sha256(files["schema.json"]).hexdigest(),
                    },
                    "template": {
                        "sha256": hashlib.sha256(files["template.ttp"]).hexdigest(),
                    },
                    "expected": {
                        "sha256": hashlib.sha256(files["expected.json"]).hexdigest(),
                    },
                    "inputs": [
                        {
                            "name": f"{index:03d}.txt",
                            "sha256": hashlib.sha256(
                                input_payloads[index],
                            ).hexdigest(),
                        }
                        for index in range(1, input_count + 1)
                    ],
                },
            },
        ],
    }
    manifest_path = root / "manifest.json"
    manifest_path.write_text(
        json.dumps(manifest, indent=2) + "\n",
        encoding="utf-8",
    )
    return manifest_path


def test_load_test_set_manifest_validates_four_parts_and_order(tmp_path: Path) -> None:
    manifest = load_test_set_manifest(_write_fixture(tmp_path))

    assert len(manifest.cases) == 1
    case = manifest.cases[0]
    assert case.id == "demo.case"
    assert [item.text for item in case.inputs] == ["value-1\r\n", "value-2\r\n"]
    assert list(case.expected_records) == [
        {"lines": [{"text": "value-1"}]},
        {"lines": [{"text": "value-2"}]},
    ]


def test_select_test_sets_supports_suite_or_explicit_case(tmp_path: Path) -> None:
    manifest = load_test_set_manifest(_write_fixture(tmp_path))

    assert select_test_sets(manifest, suite="smoke", case_ids=())[0].id == "demo.case"
    assert (
        select_test_sets(manifest, suite=None, case_ids=("demo.case",))[0].id
        == "demo.case"
    )
    with pytest.raises(HarnessError, match="exactly one"):
        select_test_sets(manifest, suite=None, case_ids=())


def test_old_target_shape_is_rejected(tmp_path: Path) -> None:
    manifest_path = _write_fixture(tmp_path)
    raw = json.loads(manifest_path.read_text(encoding="utf-8"))
    raw["cases"][0]["target"] = {"path": "target.json", "sha256": "0" * 64}
    manifest_path.write_text(json.dumps(raw), encoding="utf-8")

    with pytest.raises(HarnessError, match="unsupported keys"):
        load_test_set_manifest(manifest_path)


def test_path_escape_is_rejected_before_loading_case(tmp_path: Path) -> None:
    manifest_path = _write_fixture(tmp_path)
    raw = json.loads(manifest_path.read_text(encoding="utf-8"))
    raw["cases"][0]["path"] = "../outside"
    manifest_path.write_text(json.dumps(raw), encoding="utf-8")

    with pytest.raises(HarnessError, match="outside|traversal"):
        load_test_set_manifest(manifest_path)


@pytest.mark.parametrize("input_count", [1, 5])
def test_input_count_boundaries_are_supported(tmp_path: Path, input_count: int) -> None:
    manifest = load_test_set_manifest(_write_fixture(tmp_path, input_count=input_count))

    assert len(manifest.cases[0].inputs) == input_count


def test_six_inputs_are_rejected(tmp_path: Path) -> None:
    manifest_path = _write_fixture(tmp_path, input_count=5)
    case_dir = tmp_path / "demo.case"
    (case_dir / "inputs" / "006.txt").write_text("value-6\n", encoding="utf-8")
    raw = json.loads(manifest_path.read_text(encoding="utf-8"))
    raw["cases"][0]["files"]["inputs"].append(
        {"name": "006.txt", "sha256": hashlib.sha256(b"value-6\n").hexdigest()},
    )
    manifest_path.write_text(json.dumps(raw), encoding="utf-8")

    with pytest.raises(HarnessError, match="1 to 5"):
        load_test_set_manifest(manifest_path)


def test_hash_drift_is_rejected(tmp_path: Path) -> None:
    manifest_path = _write_fixture(tmp_path)
    (tmp_path / "demo.case" / "template.ttp").write_text(
        "changed",
        encoding="utf-8",
    )

    with pytest.raises(HarnessError, match="SHA-256"):
        load_test_set_manifest(manifest_path)


def test_duplicate_json_keys_are_rejected(tmp_path: Path) -> None:
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(
        '{"version": 1, "version": 1, "cases": []}',
        encoding="utf-8",
    )

    with pytest.raises(HarnessError, match="duplicate"):
        load_test_set_manifest(manifest_path)


def test_expected_records_must_match_the_standard_template(tmp_path: Path) -> None:
    manifest_path = _write_fixture(tmp_path)
    expected_path = tmp_path / "demo.case" / "expected.json"
    expected_path.write_text(
        json.dumps([{"lines": [{"text": "wrong"}]}, {"lines": [{"text": "wrong"}]}]),
        encoding="utf-8",
    )
    raw = json.loads(manifest_path.read_text(encoding="utf-8"))
    raw["cases"][0]["files"]["expected"]["sha256"] = hashlib.sha256(
        expected_path.read_bytes(),
    ).hexdigest()
    manifest_path.write_text(json.dumps(raw), encoding="utf-8")

    with pytest.raises(HarnessError, match="standard template"):
        load_test_set_manifest(manifest_path)


def test_extra_input_file_is_rejected(tmp_path: Path) -> None:
    manifest_path = _write_fixture(tmp_path)
    (tmp_path / "demo.case" / "inputs" / "999.txt").write_text(
        "unexpected\n",
        encoding="utf-8",
    )

    with pytest.raises(HarnessError, match="unexpected files"):
        load_test_set_manifest(manifest_path)


def test_semantic_pilot_rejects_line_text_placeholder(tmp_path: Path) -> None:
    manifest_path = _write_fixture(tmp_path, suites=["semantic-pilot"])

    with pytest.raises(HarnessError, match=r"lines\[\]\.text placeholder"):
        load_test_set_manifest(manifest_path)
