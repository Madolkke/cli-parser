from __future__ import annotations

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
from cli_parser_agent.ttp_generation.agent import prompt as prompt_module


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
                [{"value": f"Value: {values[index]}"} for index in range(input_count)],
            )
            + "\n",
            encoding="utf-8",
            newline="\n",
        )
    lines = [
        "version = 2",
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
        f"  {{ file = 'inputs/{index + 1:03d}.txt' }}," for index in range(input_count)
    )
    lines.append("]")
    if default_input is not None:
        lines.append(f'default_input = "{default_input}"')
    if template or complete:
        lines.append("template = { file = 'template.ttp' }")
    if complete:
        lines.extend(
            [
                "schema = { file = 'schema.json' }",
                "expected = { file = 'expected.json' }",
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

    assert registry.version == 2
    assert registry.datasets[0].stage == stage
    reports = preflight_dataset_registry(registry)
    assert reports[0].status == ("pending" if stage == "inputs-only" else "passed")


def test_registry_rejects_version_one(tmp_path: Path) -> None:
    registry_path = _write_dataset(tmp_path)
    registry_path.write_text(
        registry_path.read_text(encoding="utf-8").replace("version = 2", "version = 1"),
        encoding="utf-8",
    )

    with pytest.raises(HarnessError, match="version must be 2"):
        load_dataset_registry(registry_path)


@pytest.mark.parametrize(
    "file_name",
    ["inputs/001.txt", "template.ttp", "schema.json", "expected.json"],
)
def test_version_two_rejects_legacy_sha256_fields(
    tmp_path: Path,
    file_name: str,
) -> None:
    registry_path = _write_dataset(tmp_path, complete=True)
    registry_path.write_text(
        registry_path.read_text(encoding="utf-8").replace(
            f"file = '{file_name}'",
            f"file = '{file_name}', sha256 = '{'0' * 64}'",
        ),
        encoding="utf-8",
    )

    with pytest.raises(HarnessError, match="unsupported keys: sha256"):
        load_dataset_registry(registry_path)


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


def test_crlf_inputs_are_normalized_before_parsing(
    tmp_path: Path,
) -> None:
    """CRLF must never reach the parser.

    TTP anchors rows with (?=\\n|\\r\\n) so CRLF still matches, but a greedy
    capture swallows the trailing CR while goldens hold CR-free values, which
    scores otherwise-correct templates as failures.
    """
    registry_path = _write_dataset(tmp_path, template=True, complete=True)
    source = tmp_path / "test_sets" / "demo.case" / "inputs" / "001.txt"
    source.write_bytes(b"Value: alpha\r\n")

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
    expected_path.write_text(
        json.dumps([{"value": "Value: alpha"}, {"value": "Value: wrong"}]) + "\n",
        encoding="utf-8",
    )
    registry = load_dataset_registry(registry_path)

    assert preflight_dataset_registry(registry)[0].status == "passed"
    full_report = preflight_dataset_registry(registry, input_scope="full")[0]
    assert full_report.status == "failed"
    assert "baseline mismatch" in full_report.errors[0]


def test_input_changes_do_not_require_registry_updates(tmp_path: Path) -> None:
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

    registry = load_dataset_registry(registry_path)

    assert registry.datasets[0].input_texts[1].text == "Value: changed\n"
    assert preflight_dataset_registry(registry)[0].status == "passed"
    assert (
        preflight_dataset_registry(registry, input_scope="full")[0].status == "passed"
    )


def test_complete_asset_changes_do_not_require_registry_updates(tmp_path: Path) -> None:
    registry_path = _write_dataset(tmp_path, complete=True)
    registry_bytes = registry_path.read_bytes()
    case = tmp_path / "test_sets" / "demo.case"
    (case / "inputs" / "001.txt").write_text("Name: beta\n", encoding="utf-8")
    (case / "template.ttp").write_text("Name: {{ name }}\n", encoding="utf-8")
    schema_path = case / "schema.json"
    schema_path.write_text(
        schema_path.read_text(encoding="utf-8").replace('"value"', '"name"'),
        encoding="utf-8",
    )
    (case / "expected.json").write_text('[{"name": "beta"}]\n', encoding="utf-8")

    report = preflight_dataset_registry(load_dataset_registry(registry_path))[0]

    assert registry_path.read_bytes() == registry_bytes
    assert report.status == "passed", report.errors
    assert report.case is not None
    assert report.case.expected_records == ({"name": "beta"},)


@pytest.mark.parametrize(
    ("payload", "error"),
    [
        (b"\xef\xbb\xbfValue: beta\n", "BOM"),
        (b"\xff\n", "UTF-8"),
        (b" \t\n", "whitespace"),
        (b"", "emptiness"),
        (b"x" * (1024 * 1024 + 1), "size"),
    ],
    ids=["bom", "invalid-utf8", "whitespace", "empty", "oversized"],
)
def test_input_format_is_checked_after_asset_changes(
    tmp_path: Path,
    payload: bytes,
    error: str,
) -> None:
    registry_path = _write_dataset(tmp_path, input_count=2)
    source = tmp_path / "test_sets" / "demo.case" / "inputs" / "002.txt"
    source.write_bytes(payload)

    with pytest.raises(HarnessError, match=error):
        load_dataset_registry(registry_path)


@pytest.mark.parametrize(
    ("file_name", "payload", "error"),
    [
        ("schema.json", '{"type": "string"}', "not supported"),
        ("expected.json", '[{"value": 1}]', "violates schema"),
        ("expected.json", '[{"value": "wrong"}]', "baseline mismatch"),
        ("expected.json", '[{"value": "a", "value": "b"}]', "duplicate"),
    ],
)
def test_invalid_assets_are_rejected_without_hash_checks(
    tmp_path: Path,
    file_name: str,
    payload: str,
    error: str,
) -> None:
    registry_path = _write_dataset(tmp_path, complete=True)
    (tmp_path / "test_sets" / "demo.case" / file_name).write_text(
        payload,
        encoding="utf-8",
    )

    report = preflight_dataset_registry(load_dataset_registry(registry_path))[0]

    assert report.status == "failed"
    assert error in report.errors[0]


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
    from agentscope.event import CustomEvent, ModelCallEndEvent

    tracer = _load_runner()._RoundTracer()
    tracer(
        CustomEvent(
            metadata={"sensitive": True, "sequence": 1, "phase": "ttp"},
            name="cli_parser.untrusted",
            value={"command_output": "secret"},
        ),
    )
    tracer(
        ModelCallEndEvent(
            metadata={"sensitive": False, "sequence": 2, "phase": "ttp"},
            reply_id="reply",
            input_tokens=41233,
            output_tokens=812,
        ),
    )
    tracer(
        CustomEvent(
            metadata={"sensitive": False, "sequence": 3, "phase": "ttp"},
            name="cli_parser.ttp.submission",
            value={
                "template_sha256": "ab12",
                "submission_index": 2,
                "template_chars": 640,
                "records": [{"leaked": True}],
                "command_output": "secret-scalar",
            },
        ),
    )

    assert [row["sequence"] for row in tracer.rows] == [2, 3]
    assert tracer.rows[0]["input_tokens"] == 41233
    assert tracer.rows[0]["output_tokens"] == 812
    assert tracer.rows[1]["value"] == {
        "submission_index": 2,
        "template_chars": 640,
    }
    assert "secret" not in json.dumps(tracer.rows)


def test_round_tracer_collects_execution_facts_without_recording_rows() -> None:
    from agentscope.event import CustomEvent

    runner = _load_runner()
    tracer = runner._RoundTracer(record_rows=False)
    other = runner._RoundTracer(record_rows=False)
    tracer(
        CustomEvent(
            metadata={"sensitive": False},
            name="cli_parser.generation.execution_facts",
            value={
                "schema_frozen": True,
                "finish_succeeded": False,
                "finish_called": "true",
                "records": "secret",
            },
        )
    )
    assert tracer.execution_facts == {"schema_frozen": True, "finish_succeeded": False}
    assert not tracer.rows
    assert not other.execution_facts
    tracer(
        CustomEvent(
            metadata={"sensitive": True},
            name="cli_parser.generation.execution_facts",
            value={"schema_frozen": False},
        )
    )
    assert tracer.execution_facts["schema_frozen"] is True


@pytest.mark.asyncio
async def test_runner_collects_facts_without_rows_on_schema_rejection() -> None:
    from types import SimpleNamespace

    from cli_parser_agent import GenerationPolicy, TtpGeneratorSettings

    runner = _load_runner()
    tracer = runner._RoundTracer(record_rows=False)
    result = await runner._run_ttp_trial(
        SimpleNamespace(
            inputs=(SimpleNamespace(text="Value: one"),),
            schema={"type": "object", "properties": {}, "additionalProperties": True},
            expected_records=({},),
        ),
        TtpGeneratorSettings(api_key="unused", model_name="unused"),
        GenerationPolicy(),
        tracer,
    )
    assert result["exception_type"] is None
    assert result["generation_result"]["metadata"]["termination_reason"] == (
        "invalid_injected_schema"
    )
    assert len(result["execution_facts"]) == 7
    assert not any(result["execution_facts"].values())
    assert result["score"]["metrics"]["entered_ttp"] == 0.0
    assert not tracer.rows


def test_round_tracer_keeps_attempt_diagnostics_without_exception_text() -> None:
    from agentscope.event import CustomEvent

    tracer = _load_runner()._RoundTracer()
    tracer(
        CustomEvent(
            metadata={"sensitive": False, "phase": "ttp"},
            name="cli_parser.model.attempt",
            value={
                "event": "finished",
                "round_index": 2,
                "attempt_index": 2,
                "request_attempt_index": 3,
                "is_retry": True,
                "outcome": "exception",
                "error_category": "timeout",
                "elapsed_seconds": 60.1,
                "exception": "secret",
                "client": "secret",
            },
        )
    )
    assert tracer.rows[0]["value"]["error_category"] == "timeout"
    assert tracer.rows[0]["value"]["elapsed_seconds"] == 60.1
    assert "secret" not in json.dumps(tracer.rows)


def test_persisted_trial_projection_excludes_raw_generation_and_acceptance() -> None:
    runner = _load_runner()
    payload = {
        "generation_result": {
            "status": "failed",
            "artifact": {"ttp_template": "secret-template", "records": ["secret"]},
            "last_attempt": {"ttp_template": "secret-last"},
            "metadata": {
                "termination_reason": "model_error",
                "fault_domain": "model",
                "laminar_trace_id": "trace-id",
            },
            "issues": [
                {"code": "model.failed", "message": "secret-error"},
                {"code": "secret code"},
            ],
        },
        "independent_acceptance": {
            "valid": False,
            "records": ["secret"],
            "issue_codes": ["ttp.no_match", "secret code"],
        },
        "execution_facts": {
            "finish_called": True,
            "finish_succeeded": False,
            "model_text": "secret",
        },
        "exception_type": None,
        "score": {"metrics": {"candidate_pass": 0.0}, "inputs": []},
    }
    projected = runner._safe_trial_projection(payload)
    assert projected["trace_id"] == "trace-id"
    assert projected["issue_codes"] == ["model.failed", "ttp.no_match"]
    assert projected["last_attempt_present"] is True
    assert projected["execution_facts"] == {
        "finish_called": True,
        "finish_succeeded": False,
    }
    assert "generation_result" not in projected
    assert "independent_acceptance" not in projected
    assert "secret" not in json.dumps(projected)
    assert payload["generation_result"]["artifact"]["ttp_template"] == "secret-template"


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


def test_configuration_preserves_prompt_version_and_policy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.setenv("OPENAI_MODEL", "test-model")
    monkeypatch.setenv("OPENAI_BASE_URL", "https://example.invalid/v1")
    runner = _load_runner()

    _, policy, _, configuration = runner._configuration()

    assert configuration["prompt"] == {"version": prompt_module.PROMPT_VERSION}
    assert configuration["policy"] == policy.model_dump(mode="json")
    assert runner.RUNNER_VERSION == 5
    assert runner.BASELINE_VERSION == 1
    assert "test-key" not in json.dumps(configuration)


def test_configuration_preserves_retry_tls_and_redacts_extra_body(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from cli_parser_agent import TtpGeneratorSettings

    settings = TtpGeneratorSettings(
        api_key="secret-key",
        model_name="test-model",
        base_url="https://example.invalid/v1",
        model_max_retries=3,
        verify_tls=False,
        extra_body={"provider_setting": "secret-body"},
    )
    runner = _load_runner()
    monkeypatch.setattr(runner.TtpGeneratorSettings, "from_env", lambda: settings)
    _, _, _, configuration = runner._configuration()
    model = configuration["model"]
    assert model["model_max_retries"] == 3
    assert model["verify_tls"] is False
    assert model["extra_body_configured"] is True
    assert "extra_body" not in model
    assert "extra_body_sha256" not in model
    assert "secret" not in json.dumps(configuration)


def test_baseline_export_preserves_scores_and_reads_historical_fields(
    tmp_path: Path,
) -> None:
    runner = _load_runner()
    counts = _counts(example=(2, 3))
    summary = {
        "captured_at": "2026-09-06T00:00:00Z",
        "runner_version": 5,
        "configuration": {"prompt": {"version": "test-version"}},
        "input_scope": "full",
        "trial_count": 3,
        "case_pass_counts": counts,
        "strict_pass_count": 2,
        "input_exact_match_micro": {"successes": 2, "observations": 3},
    }
    document = runner._baseline_document(summary)
    assert document == {
        "baseline_version": 1,
        "captured_at": summary["captured_at"],
        "runner_version": 5,
        "configuration": summary["configuration"],
        "input_scope": "full",
        "trial_count": 3,
        "cases": counts,
        "overall": {
            "candidate_pass": {"successes": 2, "observations": 3},
            "input_exact_match_micro": summary["input_exact_match_micro"],
        },
    }
    historical = {
        **document,
        "runner_version": 4,
        "registry_sha256": "old-registry",
        "config_fingerprint": "old-config",
    }
    for name, payload in (("new", document), ("old", historical)):
        path = tmp_path / f"{name}.json"
        path.write_text(json.dumps(payload), encoding="utf-8")
        original = path.read_bytes()
        loaded = runner._read_baseline(path)
        assert not runner._compare_to_baseline(loaded, counts, 0)["regressed_cases"]
        assert path.read_bytes() == original


@pytest.mark.asyncio
async def test_runner_writes_safe_trials_without_content_digests(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from dataclasses import replace

    from cli_parser_agent import TtpGeneratorSettings

    registry = load_dataset_registry(
        _write_dataset(tmp_path, template=True, complete=True),
    )
    report = preflight_dataset_registry(registry)[0]
    assert report.case is not None
    # Exercise the real public API's pre-model rejection and persisted reports.
    case = replace(
        report.case,
        schema={"type": "object", "properties": {}, "additionalProperties": True},
    )
    report = replace(report, case=case)
    runner = _load_runner()
    settings = TtpGeneratorSettings(
        api_key="private-key",
        model_name="unused-model",
        extra_body={"provider_setting": "private-body"},
    )
    monkeypatch.setattr(runner.TtpGeneratorSettings, "from_env", lambda: settings)
    output = tmp_path / "runs"
    monkeypatch.setenv("CLI_PARSER_TEST_SET_ARTIFACT_ROOT", str(output))
    baseline = tmp_path / "baseline.json"
    args = runner._build_parser().parse_args(
        [
            "run",
            "--registry",
            str(registry.path),
            "--mode",
            "ttp-only",
            "--write-baseline",
            str(baseline),
        ],
    )
    assert await runner._run_ttp(args, registry, (report,)) == 0
    summary_path = next(output.glob("*/summary.json"))
    summary = json.loads(summary_path.read_text(encoding="utf-8"))
    assert summary["runner_version"] == 5
    assert summary["registry"] == {"path": str(registry.path)}
    assert summary["configuration"]["model"]["extra_body_configured"] is True
    assert summary["case_pass_counts"] == _counts(**{case.id: (0, 1)})
    trial_path = next(output.glob("*/datasets/*/trials/trial-01.json"))
    trial = json.loads(trial_path.read_text(encoding="utf-8"))
    assert trial["termination_reason"] == "invalid_injected_schema"
    assert trial["execution_facts"]["entered_ttp"] is False
    assert trial["case"]["selected_inputs"] == [
        {"input_index": 0, "display_number": 1, "path": case.inputs[0].path},
    ]
    assert "files" not in trial["case"]
    assert "configuration" not in trial

    for path in [*output.rglob("*.json"), baseline]:
        text = path.read_text(encoding="utf-8")
        assert not any(value in text for value in ("sha256", "fingerprint", "private-"))
    assert runner._read_baseline(baseline)["cases"] == summary["case_pass_counts"]


def test_round_tracer_redacts_unknown_tool_names_and_reason_values() -> None:
    from agentscope.event import ModelCallEndEvent, ToolCallStartEvent

    tracer = _load_runner()._RoundTracer()
    metadata = {"sensitive": False, "phase": "ttp"}
    for name in (
        "submit_result_schema",
        "submit_ttp_template",
        "test_ttp_template",
        "finish_generation",
        "secret input in tool name",
    ):
        tracer(
            ToolCallStartEvent(
                metadata=metadata,
                reply_id="reply",
                tool_call_id="call",
                tool_call_name=name,
            )
        )
    for reason in ("completed", "interrupted", "secret provider reason"):
        event = ModelCallEndEvent(
            metadata=metadata,
            reply_id="reply",
            input_tokens=1,
            output_tokens=2,
        ).model_copy(update={"finished_reason": reason})
        tracer(event)
    assert [row["name"] for row in tracer.rows[:5]] == [
        "submit_result_schema",
        "submit_ttp_template",
        "test_ttp_template",
        "finish_generation",
        "unknown_tool",
    ]
    assert [row.get("finished_reason") for row in tracer.rows[5:]] == [
        "completed",
        "interrupted",
        None,
    ]
    assert "secret" not in json.dumps(tracer.rows)


def test_round_tracer_rejects_unknown_event_classes() -> None:
    from agentscope.event import CustomEvent, ReplyStartEvent, TextBlockDeltaEvent

    tracer = _load_runner()._RoundTracer()
    metadata = {"sensitive": False, "phase": "ttp"}
    tracer(
        TextBlockDeltaEvent(
            metadata=metadata, reply_id="reply", block_id="block", delta="secret"
        )
    )
    tracer(
        CustomEvent(
            metadata=metadata, name="secret unknown event", value={"status": "secret"}
        )
    )
    assert tracer.rows == []
    tracer(
        ReplyStartEvent(
            metadata=metadata,
            session_id="session",
            reply_id="reply",
            name="secret agent name",
        )
    )
    assert len(tracer.rows) == 1
    assert tracer.rows[0]["event"] == "ReplyStartEvent"
    assert "name" not in tracer.rows[0]
    assert "secret" not in json.dumps(tracer.rows)
