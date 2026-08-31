from __future__ import annotations

import hashlib
import importlib.util
import json
from pathlib import Path
from types import ModuleType

import pytest

from cli_parser_agent.evaluation import (
    HarnessError,
    load_dataset_registry,
    preflight_dataset_registry,
    select_dataset_entries,
)


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _load_runner() -> ModuleType:
    script_path = Path(__file__).resolve().parents[2] / "scripts" / "run_test_sets.py"
    spec = importlib.util.spec_from_file_location("run_test_sets", script_path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _write_dataset(
    root: Path,
    *,
    name: str = "demo.case",
    template: bool = False,
    complete: bool = False,
    input_count: int = 1,
    default_input: str | None = "inputs/001.txt",
) -> Path:
    case = root / "test_sets" / name
    inputs = case / "inputs"
    inputs.mkdir(parents=True)
    values = ("alpha", "beta", "gamma", "delta", "epsilon")
    for index in range(input_count):
        (inputs / f"{index + 1:03d}.txt").write_text(
            f"Value: {values[index]}\n",
            encoding="utf-8",
        )
    if template or complete:
        (case / "template.ttp").write_text(
            '{{ value | re("[^\\r\\n]+") }}\n',
            encoding="utf-8",
        )
    if complete:
        (case / "schema.json").write_text(
            json.dumps(
                {
                    "type": "object",
                    "properties": {"value": {"type": "string"}},
                    "required": ["value"],
                    "additionalProperties": False,
                },
            )
            + "\n",
            encoding="utf-8",
        )
        (case / "expected.json").write_text(
            json.dumps(
                [
                    {"value": f"Value: {values[index]}"}
                    for index in range(input_count)
                ],
            )
            + "\n",
            encoding="utf-8",
        )
    lines = [
        "version = 1",
        "",
        "[[dataset]]",
        "id = 1",
        f'name = "{name}"',
        'command = "show demo"',
        'platform = "demo"',
        'source = "fixture"',
        'tags = ["easy"]',
        "inputs = [",
    ]
    lines.extend(
        "  { file = 'inputs/"
        + f"{index + 1:03d}.txt"
        + "', sha256 = '"
        + _sha(inputs / f"{index + 1:03d}.txt")
        + "' },"
        for index in range(input_count)
    )
    lines.append("]")
    if default_input is not None:
        lines.append(f'default_input = "{default_input}"')
    if template or complete:
        template_hash = _sha(case / "template.ttp")
        lines.append(
            "template = { file = 'template.ttp', sha256 = '" + template_hash + "' }",
        )
    if complete:
        schema_hash = _sha(case / "schema.json")
        expected_hash = _sha(case / "expected.json")
        lines.extend(
            [
                "schema = { file = 'schema.json', sha256 = '" + schema_hash + "' }",
                "expected = { file = 'expected.json', sha256 = '"
                + expected_hash
                + "' }",
            ],
        )
    registry = root / "datasets.toml"
    registry.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return registry


@pytest.mark.parametrize(
    ("template", "complete", "stage"),
    [
        (False, False, "inputs-only"),
        (True, False, "template"),
        (True, True, "complete"),
    ],
)
def test_registry_detects_directory_stage(
    tmp_path: Path,
    template: bool,
    complete: bool,
    stage: str,
) -> None:
    registry = load_dataset_registry(
        _write_dataset(tmp_path, template=template, complete=complete),
    )

    assert registry.datasets[0].stage == stage
    reports = preflight_dataset_registry(registry)
    assert reports[0].status == ("pending" if stage == "inputs-only" else "passed")


def test_registry_selects_by_name_id_and_tag(tmp_path: Path) -> None:
    registry_path = _write_dataset(tmp_path)
    registry = load_dataset_registry(registry_path)

    assert select_dataset_entries(registry, names=("demo.case",))[0].id == 1
    assert select_dataset_entries(registry, ids=(1,))[0].name == "demo.case"
    assert select_dataset_entries(registry, tags=("easy",))[0].name == "demo.case"
    with pytest.raises(HarnessError, match="unknown datasets"):
        select_dataset_entries(registry, names=("missing.case",))


def test_schema_and_expected_must_be_declared_together(tmp_path: Path) -> None:
    registry_path = _write_dataset(tmp_path)
    case = tmp_path / "test_sets" / "demo.case"
    (case / "schema.json").write_text("{}\n", encoding="utf-8")

    with pytest.raises(HarnessError, match="together"):
        load_dataset_registry(registry_path)


def test_unregistered_dataset_directory_is_rejected(tmp_path: Path) -> None:
    registry_path = _write_dataset(tmp_path)
    extra = tmp_path / "test_sets" / "extra.case" / "inputs"
    extra.mkdir(parents=True)
    (extra / "001.txt").write_text("x\n", encoding="utf-8")

    with pytest.raises(HarnessError, match="unregistered"):
        load_dataset_registry(registry_path)


def test_missing_declared_file_is_pending(tmp_path: Path) -> None:
    registry_path = _write_dataset(tmp_path, template=True)
    template_path = tmp_path / "test_sets" / "demo.case" / "template.ttp"
    template_path.unlink()

    registry = load_dataset_registry(registry_path)
    assert registry.datasets[0].missing_files == ("template.ttp",)
    assert preflight_dataset_registry(registry)[0].status == "pending"


def test_path_escape_is_rejected(tmp_path: Path) -> None:
    registry_path = _write_dataset(tmp_path)
    text = registry_path.read_text(encoding="utf-8").replace(
        "file = 'inputs/001.txt'",
        "file = '../outside.txt'",
    )
    registry_path.write_text(text, encoding="utf-8")

    with pytest.raises(HarnessError, match="traversal|outside"):
        load_dataset_registry(registry_path)


def test_default_scope_selects_only_the_registered_input(tmp_path: Path) -> None:
    registry = load_dataset_registry(
        _write_dataset(
            tmp_path,
            template=True,
            complete=True,
            input_count=2,
            default_input="inputs/002.txt",
        ),
    )

    report = preflight_dataset_registry(registry)[0]

    assert report.status == "passed"
    assert report.input_scope == "default"
    assert report.selected_input_indices == (1,)
    assert report.case is not None
    assert report.case.original_input_indices == (1,)
    assert [item.text.strip() for item in report.case.inputs] == ["Value: beta"]
    assert report.case.expected_records == ({"value": "Value: beta"},)
    assert report.as_dict()["selected_inputs"] == [
        {"input_index": 1, "display_number": 2, "file": "inputs/002.txt"},
    ]


def test_default_scope_is_pending_without_a_default_input(tmp_path: Path) -> None:
    registry = load_dataset_registry(
        _write_dataset(
            tmp_path,
            template=True,
            complete=True,
            default_input=None,
        ),
    )

    default_report = preflight_dataset_registry(registry)[0]
    full_report = preflight_dataset_registry(registry, input_scope="full")[0]

    assert default_report.status == "pending"
    assert default_report.selected_input_indices == ()
    assert full_report.status == "passed"
    assert full_report.selected_input_indices == (0,)


def test_default_input_must_be_declared_input(tmp_path: Path) -> None:
    registry_path = _write_dataset(
        tmp_path,
        input_count=2,
        default_input="inputs/002.txt",
    )
    text = registry_path.read_text(encoding="utf-8").replace(
        'default_input = "inputs/002.txt"',
        'default_input = "inputs/003.txt"',
    )
    registry_path.write_text(text, encoding="utf-8")

    with pytest.raises(HarnessError, match="must match a declared input"):
        load_dataset_registry(registry_path)


def test_default_scope_ignores_nondefault_baseline_mismatch(tmp_path: Path) -> None:
    registry_path = _write_dataset(
        tmp_path,
        template=True,
        complete=True,
        input_count=2,
        default_input="inputs/001.txt",
    )
    expected_path = tmp_path / "test_sets" / "demo.case" / "expected.json"
    old_expected_hash = _sha(expected_path)
    expected_path.write_text(
        json.dumps([{"value": "Value: alpha"}, {"value": "Value: wrong"}])
        + "\n",
        encoding="utf-8",
    )
    text = registry_path.read_text(encoding="utf-8").replace(
        old_expected_hash,
        _sha(expected_path),
    )
    registry_path.write_text(text, encoding="utf-8")
    registry = load_dataset_registry(registry_path)

    assert preflight_dataset_registry(registry)[0].status == "passed"
    full_report = preflight_dataset_registry(registry, input_scope="full")[0]
    assert full_report.status == "failed"
    assert "baseline mismatch" in full_report.errors[0]


def test_all_input_hashes_are_checked_for_default_scope(tmp_path: Path) -> None:
    registry_path = _write_dataset(
        tmp_path,
        template=True,
        input_count=2,
        default_input="inputs/001.txt",
    )
    (tmp_path / "test_sets" / "demo.case" / "inputs" / "002.txt").write_text(
        "Value: changed\n",
        encoding="utf-8",
    )

    with pytest.raises(HarnessError, match="SHA-256"):
        load_dataset_registry(registry_path)


def test_runner_defaults_to_registered_default_input_scope() -> None:
    parser = _load_runner()._build_parser()

    default_args = parser.parse_args(
        ["run", "--registry", "evals/datasets.toml", "--mode", "baseline"],
    )
    full_args = parser.parse_args(
        [
            "run",
            "--registry",
            "evals/datasets.toml",
            "--mode",
            "baseline",
            "--input-scope",
            "full",
        ],
    )

    assert default_args.input_scope == "default"
    assert full_args.input_scope == "full"
    with pytest.raises(SystemExit):
        parser.parse_args(
            [
                "run",
                "--registry",
                "evals/datasets.toml",
                "--mode",
                "baseline",
                "--input",
                "inputs/001.txt",
            ],
        )
