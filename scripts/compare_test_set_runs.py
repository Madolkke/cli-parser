"""Compare two test-set evaluation runs, or a run against a frozen baseline.

The per-case delta table is the artifact to judge a change by. The aggregate
cannot move detectably on this corpus: 24 observations are really 8 clusters,
and most cases land on all-pass or all-fail, so Wilson-on-trials is
anticonservative. A paired bootstrap over cases is reported alongside it to
make that clustering explicit.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

SCRIPT_DIRECTORY = Path(__file__).resolve().parent
PROJECT_ROOT = SCRIPT_DIRECTORY.parent
if str(SCRIPT_DIRECTORY) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIRECTORY))

from cli_parser_agent.evaluation import wilson_interval  # noqa: E402

BOOTSTRAP_RESAMPLES = 10_000
# Config keys whose difference is almost always the point of the comparison.
# Anything else differing is reported as a confound.
EXPECTED_KNOBS = ("prompt",)
LEGACY_CONFIG_HASH_FIELDS = frozenset(
    {
        "model.extra_body_sha256",
        "prompt.schema_system_sha256",
        "prompt.ttp_system_sha256",
    }
)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("before", type=Path, help="baseline JSON or summary.json")
    parser.add_argument("after", type=Path, help="summary.json of the new run")
    parser.add_argument(
        "--metric",
        default="ttp_test_calls",
        help="extra per-case metric to show, read from summary cases aggregates",
    )
    return parser


def _read_json(path: Path) -> Mapping[str, Any]:
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except OSError:
        raise SystemExit(f"{path} could not be read") from None
    except ValueError:
        raise SystemExit(f"{path} is not valid JSON") from None
    if not isinstance(document, Mapping):
        raise SystemExit(f"{path} must be a JSON object")
    return document


def case_pass_counts(document: Mapping[str, Any]) -> dict[str, tuple[int, int]]:
    """Read per-case pass counts from either a baseline or a run summary."""
    source = document.get("cases") if "baseline_version" in document else None
    if source is None:
        source = document.get("case_pass_counts")
    if not isinstance(source, Mapping):
        raise SystemExit(
            "document has no per-case pass counts; it predates runner_version 3",
        )
    counts: dict[str, tuple[int, int]] = {}
    for case_id, entry in source.items():
        if isinstance(entry, Mapping):
            counts[str(case_id)] = (
                int(entry.get("candidate_pass_successes", 0)),
                int(entry.get("trials", 0)),
            )
    return counts


def _configuration(document: Mapping[str, Any]) -> Mapping[str, Any]:
    configuration = document.get("configuration")
    return configuration if isinstance(configuration, Mapping) else {}


def _flatten(value: Any, prefix: str = "") -> dict[str, Any]:
    if isinstance(value, Mapping):
        flat: dict[str, Any] = {}
        for key, child in value.items():
            flat.update(_flatten(child, f"{prefix}.{key}" if prefix else str(key)))
        return flat
    return {prefix: value}


def config_differences(
    before: Mapping[str, Any],
    after: Mapping[str, Any],
) -> tuple[list[tuple[str, Any, Any]], list[tuple[str, Any, Any]]]:
    """Split config differences into intended knobs and everything else.

    A .env drift that silently changes a policy limit would otherwise read as a
    prompt win, so unexpected differences are surfaced before any numbers.
    """
    flat_before = _flatten(_configuration(before))
    flat_after = _flatten(_configuration(after))
    intended: list[tuple[str, Any, Any]] = []
    confounds: list[tuple[str, Any, Any]] = []
    for key in sorted(set(flat_before) | set(flat_after)):
        if key in LEGACY_CONFIG_HASH_FIELDS:
            continue
        old = flat_before.get(key)
        new = flat_after.get(key)
        if old == new:
            continue
        target = (
            intended
            if any(key == knob or key.startswith(f"{knob}.") for knob in EXPECTED_KNOBS)
            else confounds
        )
        target.append((key, old, new))
    return intended, confounds


def paired_case_bootstrap(
    before: Mapping[str, tuple[int, int]],
    after: Mapping[str, tuple[int, int]],
    *,
    resamples: int = BOOTSTRAP_RESAMPLES,
) -> dict[str, float]:
    """Resample cases, not trials, so the cluster structure is respected.

    Deterministic: a fixed linear congruential sequence keeps two invocations
    on the same inputs comparable, since the runner forbids seeding elsewhere.
    """
    shared = sorted(set(before) & set(after))
    if not shared:
        return {"observed": 0.0, "lower": 0.0, "upper": 0.0, "cases": 0}
    deltas = [
        (after[case][0] / (after[case][1] or 1))
        - (before[case][0] / (before[case][1] or 1))
        for case in shared
    ]
    observed = sum(deltas) / len(deltas)
    state = 0x2545F491
    means: list[float] = []
    for _ in range(resamples):
        total = 0.0
        for _ in deltas:
            state = (1103515245 * state + 12345) & 0x7FFFFFFF
            # High bits only: an LCG's low k bits have period 2^k, so
            # ``state % len(deltas)`` would cycle through the cases in a fixed
            # order and every resample would return the same mean.
            total += deltas[(state >> 16) % len(deltas)]
        means.append(total / len(deltas))
    means.sort()
    return {
        "observed": observed,
        "lower": means[int(0.025 * (len(means) - 1))],
        "upper": means[int(0.975 * (len(means) - 1))],
        "cases": len(shared),
    }


def _totals(counts: Mapping[str, tuple[int, int]]) -> tuple[int, int]:
    return (
        sum(passed for passed, _ in counts.values()),
        sum(trials for _, trials in counts.values()),
    )


def _rate(passed: int, trials: int) -> float:
    return passed / trials if trials else 0.0


def _case_metric(
    document: Mapping[str, Any],
    case_id: str,
    metric: str,
) -> float | None:
    cases = document.get("cases")
    if not isinstance(cases, Mapping):
        return None
    entry = cases.get(case_id)
    if not isinstance(entry, Mapping):
        return None
    metrics = entry.get("metrics")
    if not isinstance(metrics, Mapping):
        return None
    value = metrics.get(metric)
    if isinstance(value, Mapping):
        value = value.get("p50", value.get("mean"))
    return float(value) if isinstance(value, int | float) else None


def render(
    before: Mapping[str, Any],
    after: Mapping[str, Any],
    metric: str,
) -> list[str]:
    lines: list[str] = []
    intended, confounds = config_differences(before, after)
    lines.append("== configuration ==")
    if confounds:
        lines.append("  UNEXPECTED DIFFERENCES (these confound the comparison):")
        lines.extend(f"    {key}: {old!r} -> {new!r}" for key, old, new in confounds)
    for key, old, new in intended:
        lines.append(f"  {key}: {old!r} -> {new!r}")
    if not intended and not confounds:
        lines.append("  identical")

    before_counts = case_pass_counts(before)
    after_counts = case_pass_counts(after)
    lines.append("")
    lines.append("== per-case candidate_pass ==")
    header = f"  {'case':<42} {'before':>8} {'after':>8} {'delta':>7}  {metric}"
    lines.append(header)
    regressed: list[str] = []
    for case_id in sorted(set(before_counts) | set(after_counts)):
        old = before_counts.get(case_id)
        new = after_counts.get(case_id)
        if old is None:
            lines.append(f"  {case_id:<42} {'--':>8} {new[0]}/{new[1]:<6} (new)")
            continue
        if new is None:
            lines.append(f"  {case_id:<42} {old[0]}/{old[1]:<6} {'--':>8} (missing)")
            continue
        delta = _rate(*new) - _rate(*old)
        old_metric = _case_metric(before, case_id, metric)
        new_metric = _case_metric(after, case_id, metric)
        trend = (
            f"{old_metric:.0f} -> {new_metric:.0f}"
            if old_metric is not None and new_metric is not None
            else ""
        )
        lines.append(
            f"  {case_id:<42} {old[0]}/{old[1]:<6} {new[0]}/{new[1]:<6} "
            f"{delta:+.2f}  {trend}",
        )
        if delta < 0:
            regressed.append(case_id)

    before_total = _totals(before_counts)
    after_total = _totals(after_counts)
    boot = paired_case_bootstrap(before_counts, after_counts)
    lines.append("")
    lines.append("== aggregate ==")
    for label, (passed, trials) in (("before", before_total), ("after", after_total)):
        low, high = wilson_interval(passed, trials)
        lines.append(
            f"  {label:<6} {passed}/{trials} = {_rate(passed, trials):.3f}  "
            f"wilson95 [{low:.3f}, {high:.3f}]",
        )
    lines.append(
        f"  paired bootstrap over {boot['cases']} cases: "
        f"{boot['observed']:+.3f} [{boot['lower']:+.3f}, {boot['upper']:+.3f}]",
    )
    lines.append(
        f"  effective_clusters: {boot['cases']} "
        "(Wilson above assumes independent trials and is anticonservative)",
    )
    if regressed:
        lines.append("")
        lines.append(f"REGRESSED: {', '.join(regressed)}")
    return lines


def main(argv: Sequence[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    before = _read_json(args.before)
    after = _read_json(args.after)
    for line in render(before, after, args.metric):
        print(line)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
