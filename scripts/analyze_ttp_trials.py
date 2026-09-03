"""Classify TTP evaluation trials by failure stage.

This is a read-only diagnostic projection. It deliberately separates model
transport failures from Agent/template failures so prompt experiments are not
credited or blamed for endpoint instability.
"""

from __future__ import annotations

import argparse
import json
from collections import Counter
from collections.abc import Iterator, Mapping
from pathlib import Path
from typing import Any

_FAILURE_CLASSES = (
    "transport_failure",
    "no_template",
    "first_template_rejected",
    "accepted_but_semantically_wrong",
    "partial_input_match",
    "budget_exhausted",
    "passed",
)


def _number(value: Any) -> float:
    return float(value) if isinstance(value, int | float) else 0.0


def _metric(trial: Mapping[str, Any], name: str) -> float:
    score = trial.get("score")
    if not isinstance(score, Mapping):
        return 0.0
    metrics = score.get("metrics")
    if not isinstance(metrics, Mapping):
        return 0.0
    return _number(metrics.get(name))


def _metadata(trial: Mapping[str, Any]) -> Mapping[str, Any]:
    result = trial.get("generation_result")
    metadata = result.get("metadata") if isinstance(result, Mapping) else None
    return metadata if isinstance(metadata, Mapping) else {}


def _issues(trial: Mapping[str, Any]) -> tuple[Mapping[str, Any], ...]:
    result = trial.get("generation_result")
    raw = result.get("issues") if isinstance(result, Mapping) else None
    if not isinstance(raw, list):
        return ()
    return tuple(item for item in raw if isinstance(item, Mapping))


def classify_trial(trial: Mapping[str, Any]) -> str:
    """Return the first applicable bounded failure class for one trial."""

    metadata = _metadata(trial)
    issues = _issues(trial)
    issue_codes = {str(item.get("code")) for item in issues}
    fault_domain = str(metadata.get("fault_domain", ""))
    termination = str(metadata.get("termination_reason", ""))

    if fault_domain == "model" or any(
        code.startswith(("model.", "provider.")) for code in issue_codes
    ):
        return "transport_failure"

    if termination in {"ttp_submission_limit", "generation_timeout"} or (
        _metric(trial, "ttp_submissions") >= 24
        or _metric(trial, "ttp_agent_rounds") >= 32
    ):
        return "budget_exhausted"

    if _metric(trial, "candidate_pass") == 1.0:
        return "passed"

    if _metric(trial, "generation_success") == 0.0:
        return "no_template"

    if _metric(trial, "first_ttp_passed") == 0.0:
        return "first_template_rejected"

    if _metric(trial, "input_exact_match_rate") < 1.0:
        return "partial_input_match"

    return "accepted_but_semantically_wrong"


def _iter_trials(run_directory: Path) -> Iterator[tuple[str, int, Mapping[str, Any]]]:
    for path in sorted(run_directory.glob("datasets/*/trials/trial-*.json")):
        try:
            document = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(document, Mapping):
            continue
        case = document.get("case")
        if isinstance(case, Mapping):
            case_id = str(case.get("id", path.parent.parent.name))
        else:
            case_id = path.parent.parent.name
        trial_index = document.get("trial_index")
        if isinstance(trial_index, int):
            index = trial_index
        else:
            index = int(path.stem.split("-")[-1]) - 1
        yield case_id, index, document


def analyze_run(run_directory: Path) -> dict[str, Any]:
    """Build a stable JSON report from all completed trial artifacts."""

    rows: list[dict[str, Any]] = []
    by_case: dict[str, Counter[str]] = {}
    for case_id, trial_index, trial in _iter_trials(run_directory):
        classification = classify_trial(trial)
        metadata = _metadata(trial)
        row = {
            "case_id": case_id,
            "trial_index": trial_index,
            "classification": classification,
            "candidate_pass": _metric(trial, "candidate_pass"),
            "generation_success": _metric(trial, "generation_success"),
            "independent_acceptance": _metric(trial, "independent_acceptance"),
            "first_ttp_passed": _metric(trial, "first_ttp_passed"),
            "input_exact_match_rate": _metric(trial, "input_exact_match_rate"),
            "ttp_agent_rounds": _metric(trial, "ttp_agent_rounds"),
            "ttp_submissions": _metric(trial, "ttp_submissions"),
            "ttp_test_calls": _metric(trial, "ttp_test_calls"),
            "model_retries_observed": _metric(trial, "model_retries_observed"),
            "input_tokens_total": _metric(trial, "input_tokens_total"),
            "output_tokens_total": _metric(trial, "output_tokens_total"),
            "termination_reason": metadata.get("termination_reason"),
            "fault_domain": metadata.get("fault_domain"),
        }
        rows.append(row)
        by_case.setdefault(case_id, Counter())[classification] += 1

    return {
        "run_directory": str(run_directory),
        "trial_count": len(rows),
        "classification_counts": dict(Counter(row["classification"] for row in rows)),
        "cases": {case_id: dict(counts) for case_id, counts in sorted(by_case.items())},
        "trials": rows,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("run_directory", type=Path)
    parser.add_argument("--json", action="store_true", help="emit the full JSON report")
    args = parser.parse_args()
    report = analyze_run(args.run_directory)
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    else:
        print(f"trials: {report['trial_count']}")
        for key, value in report["classification_counts"].items():
            print(f"{key}: {value}")
        print("\ncase breakdown:")
        for case_id, counts in report["cases"].items():
            print(f"{case_id}: {counts}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
