from __future__ import annotations

import importlib.util
from pathlib import Path
from typing import Any

_SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "analyze_ttp_trials.py"
_SPEC = importlib.util.spec_from_file_location("analyze_ttp_trials", _SCRIPT)
assert _SPEC is not None
assert _SPEC.loader is not None
_MODULE = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_MODULE)


def _trial(
    *,
    metrics: dict[str, float] | None = None,
    metadata: dict[str, Any] | None = None,
    issues: list[dict[str, str]] | None = None,
) -> dict[str, Any]:
    return {
        "score": {"metrics": metrics or {}},
        "generation_result": {
            "metadata": metadata or {},
            "issues": issues or [],
        },
    }


def test_classify_trial_separates_model_transport_failure() -> None:
    trial = _trial(
        metrics={"candidate_pass": 0.0},
        metadata={"fault_domain": "model"},
    )

    assert _MODULE.classify_trial(trial) == "transport_failure"


def test_classify_trial_detects_budget_exhaustion_before_generation_result() -> None:
    trial = _trial(
        metrics={"generation_success": 1.0, "ttp_submissions": 24.0},
        metadata={"termination_reason": "ttp_submission_limit"},
    )

    assert _MODULE.classify_trial(trial) == "budget_exhausted"


def test_classify_trial_reports_partial_input_match() -> None:
    trial = _trial(
        metrics={
            "candidate_pass": 0.0,
            "generation_success": 1.0,
            "first_ttp_passed": 1.0,
            "input_exact_match_rate": 0.75,
        },
    )

    assert _MODULE.classify_trial(trial) == "partial_input_match"


def test_classify_trial_reports_passed_before_partial_match() -> None:
    trial = _trial(
        metrics={
            "candidate_pass": 1.0,
            "generation_success": 1.0,
            "first_ttp_passed": 1.0,
            "input_exact_match_rate": 1.0,
        },
    )

    assert _MODULE.classify_trial(trial) == "passed"


def test_analyze_run_ignores_malformed_trial_documents(tmp_path: Path) -> None:
    trials = tmp_path / "datasets" / "demo.case" / "trials"
    trials.mkdir(parents=True)
    (trials / "trial-01.json").write_text(
        '{"case": {"id": "demo.case"}, "trial_index": 0, '
        '"score": {"metrics": {"candidate_pass": 1.0}}, '
        '"generation_result": {"metadata": {}, "issues": []}}',
        encoding="utf-8",
    )
    (trials / "trial-02.json").write_text("not json", encoding="utf-8")
    (trials / "trial-03.json").write_text("[]", encoding="utf-8")

    report = _MODULE.analyze_run(tmp_path)

    assert report["trial_count"] == 1
    assert report["classification_counts"] == {"passed": 1}
    assert report["cases"] == {"demo.case": {"passed": 1}}
    assert report["trials"][0]["trial_index"] == 0
