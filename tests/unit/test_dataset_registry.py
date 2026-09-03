from __future__ import annotations

import hashlib
import importlib.util
import json
import re
from pathlib import Path
from types import ModuleType

import pytest

from cli_parser_agent.evaluation import (
    HarnessError,
    load_dataset_registry,
    preflight_dataset_registry,
    select_dataset_entries,
)
from cli_parser_agent.ttp_generation.agent import prompt as prompt_module


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
            newline="\n",
        )
    if template or complete:
        (case / "template.ttp").write_text(
            '{{ value | re("[^\\r\\n]+") }}\n',
            encoding="utf-8",
            newline="\n",
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
            newline="\n",
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
            newline="\n",
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
    registry.write_text(
        "\n".join(lines) + "\n",
        encoding="utf-8",
        newline="\n",
    )
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


def test_crlf_inputs_are_normalized_after_the_digest_is_verified(
    tmp_path: Path,
) -> None:
    """CRLF must survive hashing but never reach the parser.

    TTP anchors rows with (?=\\n|\\r\\n) so CRLF still matches, but a greedy
    capture swallows the trailing CR while goldens hold CR-free values, which
    scores otherwise-correct templates as failures.
    """
    registry_path = _write_dataset(tmp_path, template=True, complete=True)
    source = tmp_path / "test_sets" / "demo.case" / "inputs" / "001.txt"
    source.write_bytes(b"Value: alpha\r\n")
    # Re-pin the digest to the CRLF bytes now on disk, so this exercises
    # normalization rather than a hash mismatch.
    registry_path.write_text(
        re.sub(
            r"(inputs/001\.txt', sha256 = ')[0-9a-f]{64}",
            lambda match: match.group(1) + _sha(source),
            registry_path.read_text(encoding="utf-8"),
        ),
        encoding="utf-8",
        newline="\n",
    )

    report = preflight_dataset_registry(load_dataset_registry(registry_path))[0]

    assert report.status == "passed", report.as_dict()
    assert report.case is not None
    assert [item.text for item in report.case.inputs] == ["Value: alpha\n"]


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


def _counts(**cases: tuple[int, int]) -> dict[str, dict[str, int]]:
    return {
        case_id: {"candidate_pass_successes": passed, "trials": total}
        for case_id, (passed, total) in cases.items()
    }


def test_round_tracer_keeps_only_non_sensitive_scalar_facts() -> None:
    """The trace must never carry a template, record, or command output.

    Sensitivity is decided by the emitter; the tracer trusts that flag and
    additionally keeps only scalars, so a future non-sensitive event carrying a
    nested payload cannot leak it into the artifact.
    """
    runner = _load_runner()
    tracer = runner._RoundTracer()

    class _Event:
        def __init__(self, **fields: object) -> None:
            self.__dict__.update(fields)

    tracer(
        _Event(
            metadata={"sensitive": True, "sequence": 1, "phase": "ttp"},
            name="cli_parser.untrusted",
            value={"command_output": "secret"},
        ),
    )
    tracer(
        _Event(
            metadata={"sensitive": False, "sequence": 2, "phase": "ttp"},
            input_tokens=41233,
            output_tokens=812,
        ),
    )
    tracer(
        _Event(
            metadata={"sensitive": False, "sequence": 3, "phase": "ttp"},
            name="cli_parser.ttp.submission",
            value={
                "template_sha256": "ab12",
                "template_chars": 640,
                "records": [{"leaked": True}],
            },
        ),
    )

    assert [row["sequence"] for row in tracer.rows] == [2, 3]
    assert tracer.rows[0]["input_tokens"] == 41233
    assert tracer.rows[0]["output_tokens"] == 812
    assert tracer.rows[1]["value"] == {
        "template_sha256": "ab12",
        "template_chars": 640,
    }
    assert "secret" not in json.dumps(tracer.rows)


def test_case_pass_counts_group_trials_by_case() -> None:
    runner = _load_runner()
    trials = [
        {"case_id": "a", "strict_pass": True},
        {"case_id": "a", "strict_pass": False},
        {"case_id": "b", "strict_pass": True},
    ]

    assert runner._case_pass_counts(trials) == _counts(a=(1, 2), b=(1, 1))


def test_baseline_comparison_gates_per_case_and_tolerates_trial_count_changes() -> None:
    """Per-case, because the aggregate cannot move detectably on 8 clusters.

    Rates rather than raw counts, because a baseline frozen at 5 trials is
    routinely compared against a 3-trial run.
    """
    runner = _load_runner()
    baseline = {
        "baseline_version": 1,
        "cases": _counts(
            steady=(5, 5),
            broken=(5, 5),
            fixed=(0, 5),
            gone=(3, 5),
        ),
    }
    current = _counts(steady=(3, 3), broken=(0, 3), fixed=(3, 3), added=(1, 3))

    result = runner._compare_to_baseline(baseline, current, 0)

    assert [item["case_id"] for item in result["regressed_cases"]] == ["broken"]
    assert [item["case_id"] for item in result["improved_cases"]] == ["fixed"]
    # 5/5 vs 3/3 is the same rate, so a changed trial count is not a regression.
    assert "steady" not in {item["case_id"] for item in result["regressed_cases"]}
    assert result["missing_cases"] == ["gone"]
    assert result["unknown_cases"] == ["added"]


def test_regression_tolerance_is_expressed_in_trials() -> None:
    runner = _load_runner()
    baseline = {"baseline_version": 1, "cases": _counts(wobbly=(3, 3))}
    current = _counts(wobbly=(2, 3))

    assert runner._compare_to_baseline(baseline, current, 0)["regressed_cases"]
    assert not runner._compare_to_baseline(baseline, current, 1)["regressed_cases"]


def test_baseline_version_mismatch_is_rejected(tmp_path: Path) -> None:
    runner = _load_runner()
    path = tmp_path / "baseline.json"
    path.write_text(
        json.dumps({"baseline_version": 999, "cases": {}}),
        encoding="utf-8",
        newline="\n",
    )

    with pytest.raises(runner.ScriptConfigurationError, match="version"):
        runner._read_baseline(path)


def test_input_exact_match_micro_pools_inputs_rather_than_averaging_trials() -> None:
    """Pooling raw counts scores each input; averaging rates scores each trial.

    A 1-input case matching and a 5-input case missing everything is 1/6 pooled,
    but 0.5 macro-averaged. Pooling is both the honest number and the tighter
    one, since a full run then observes one point per input.
    """
    runner = _load_runner()
    trials = [
        {"metrics": {"input_exact_match_count": 1.0, "input_count": 1.0}},
        {"metrics": {"input_exact_match_count": 0.0, "input_count": 5.0}},
    ]

    micro = runner._input_exact_match_micro(trials)

    assert micro["successes"] == 1.0
    assert micro["observations"] == 6.0
    assert micro["rate"] == pytest.approx(1 / 6)
    assert set(micro["wilson_95"]) == {"lower", "upper"}
    assert micro["wilson_95"]["lower"] <= micro["rate"] <= micro["wilson_95"]["upper"]

    empty = runner._input_exact_match_micro([])
    assert empty["observations"] == 0.0
    assert empty["rate"] == 0.0


def test_config_fingerprint_covers_prompt_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A prompt-only change must not fingerprint as the same configuration.

    Editing the system prompt is the most likely A/B, and before this the
    fingerprint hashed only model settings and policy, so two runs of different
    prompts were indistinguishable in summary.json.
    """
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.setenv("OPENAI_MODEL", "test-model")
    monkeypatch.setenv("OPENAI_BASE_URL", "https://example.invalid/v1")
    runner = _load_runner()

    _, _, _, configuration = runner._configuration()
    baseline = runner._fingerprint(configuration)

    assert configuration["prompt"]["version"] == prompt_module.PROMPT_VERSION
    assert configuration["prompt"]["ttp_system_sha256"] == hashlib.sha256(
        prompt_module.TTP_SYSTEM_PROMPT.encode("utf-8"),
    ).hexdigest()

    # Bumping the version alone moves it, and so does an unbumped content edit.
    for key, value in (
        ("version", "some-other-version"),
        ("ttp_system_sha256", "0" * 64),
    ):
        mutated = json.loads(json.dumps(configuration))
        mutated["prompt"][key] = value
        assert runner._fingerprint(mutated) != baseline, key
