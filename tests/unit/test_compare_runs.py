"""Offline tests for the cross-run comparison script."""

from __future__ import annotations

import importlib.util
import json
from copy import deepcopy
from pathlib import Path
from types import ModuleType

import pytest


def _load() -> ModuleType:
    path = Path(__file__).resolve().parents[2] / "scripts" / "compare_test_set_runs.py"
    spec = importlib.util.spec_from_file_location("compare_test_set_runs", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _counts(**cases: tuple[int, int]) -> dict[str, dict[str, int]]:
    return {
        case_id: {"candidate_pass_successes": passed, "trials": total}
        for case_id, (passed, total) in cases.items()
    }


def test_case_pass_counts_read_both_baseline_and_summary_shapes() -> None:
    module = _load()
    baseline = {"baseline_version": 1, "cases": _counts(a=(2, 3))}
    summary = {"case_pass_counts": _counts(a=(1, 3))}

    assert module.case_pass_counts(baseline) == {"a": (2, 3)}
    assert module.case_pass_counts(summary) == {"a": (1, 3)}


def test_pre_v3_documents_are_rejected_rather_than_silently_empty() -> None:
    module = _load()

    with pytest.raises(SystemExit, match="runner_version 3"):
        module.case_pass_counts({"strict_pass_count": 10, "trials": []})


def test_config_differences_separate_intended_knobs_from_confounds() -> None:
    """A .env drift must not read as a prompt win."""
    module = _load()
    before = {
        "configuration": {
            "prompt": {"version": "v26", "ttp_system_sha256": "aaa"},
            "policy": {"max_agent_rounds": 32},
        },
    }
    after = {
        "configuration": {
            "prompt": {"version": "v28", "ttp_system_sha256": "bbb"},
            "policy": {"max_agent_rounds": 64},
        },
    }

    intended, confounds = module.config_differences(before, after)

    assert {key for key, _, _ in intended} == {
        "prompt.version",
    }
    assert [key for key, _, _ in confounds] == ["policy.max_agent_rounds"]


def test_historical_hash_fields_do_not_mask_real_configuration_differences() -> None:
    module = _load()
    before = {
        "configuration": {
            "model": {
                "name": "model-a",
                "verify_tls": True,
                "extra_body_configured": True,
                "extra_body_sha256": "old-model-hash",
                "other_sha256": "keep-before",
            },
            "prompt": {
                "version": "v30",
                "schema_system_sha256": "old-schema-hash",
                "ttp_system_sha256": "old-ttp-hash",
            },
            "policy": {"max_agent_rounds": 13},
        },
    }
    after = deepcopy(before)
    after["configuration"]["model"].update(
        name="model-b",
        verify_tls=False,
        extra_body_configured=False,
        other_sha256="keep-after",
    )
    after["configuration"]["model"].pop("extra_body_sha256")
    after["configuration"]["prompt"] = {"version": "v31"}
    after["configuration"]["policy"]["max_agent_rounds"] = 14
    snapshots = deepcopy((before, after))

    intended, confounds = module.config_differences(before, after)

    assert intended == [("prompt.version", "v30", "v31")]
    assert {key for key, _, _ in confounds} == {
        "model.name",
        "model.verify_tls",
        "model.extra_body_configured",
        "model.other_sha256",
        "policy.max_agent_rounds",
    }
    assert (before, after) == snapshots


def test_only_historical_hashes_differing_compare_equally() -> None:
    module = _load()
    before = {
        "configuration": {
            "model": {"extra_body_sha256": "before"},
            "prompt": {
                "schema_system_sha256": "before",
                "ttp_system_sha256": "before",
            },
        },
    }
    after = {
        "configuration": {
            "model": {"extra_body_sha256": "after"},
            "prompt": {"ttp_system_sha256": "after"},
        },
    }

    assert module.config_differences(before, after) == ([], [])


def test_paired_bootstrap_resamples_cases_and_is_deterministic() -> None:
    module = _load()
    before = _counts(a=(0, 5), b=(0, 5), c=(5, 5), d=(5, 5))
    after = _counts(a=(5, 5), b=(5, 5), c=(5, 5), d=(5, 5))
    before_counts = module.case_pass_counts({"case_pass_counts": before})
    after_counts = module.case_pass_counts({"case_pass_counts": after})

    first = module.paired_case_bootstrap(before_counts, after_counts)
    second = module.paired_case_bootstrap(before_counts, after_counts)

    assert first == second
    assert first["cases"] == 4
    assert first["observed"] == pytest.approx(0.5)
    assert first["lower"] <= first["observed"] <= first["upper"]
    # The interval must have width. An LCG's low bits have period 2^k, so
    # indexing with ``state % len(deltas)`` draws each case exactly once every
    # resample and collapses the interval to a point.
    assert first["upper"] > first["lower"]


def test_bootstrap_interval_widens_as_cases_disagree() -> None:
    """Unanimous cases give a tight interval; split ones give a wide one."""
    module = _load()

    def counts(**cases: tuple[int, int]) -> dict[str, tuple[int, int]]:
        return module.case_pass_counts({"case_pass_counts": _counts(**cases)})

    unanimous = module.paired_case_bootstrap(
        counts(**dict.fromkeys("abcd", (0, 5))),
        counts(**dict.fromkeys("abcd", (5, 5))),
    )
    split = module.paired_case_bootstrap(
        counts(a=(0, 5), b=(0, 5), c=(5, 5), d=(5, 5)),
        counts(a=(5, 5), b=(5, 5), c=(0, 5), d=(0, 5)),
    )

    assert unanimous["upper"] - unanimous["lower"] == pytest.approx(0.0)
    assert split["upper"] - split["lower"] > 0.5


def test_bootstrap_handles_no_shared_cases() -> None:
    module = _load()

    result = module.paired_case_bootstrap({"a": (1, 1)}, {"b": (1, 1)})

    assert result["cases"] == 0


def test_render_flags_regressions_and_reports_cluster_count() -> None:
    module = _load()
    before = {
        "configuration": {"prompt": {"version": "v26"}},
        "case_pass_counts": _counts(kept=(3, 3), broken=(3, 3), fixed=(0, 3)),
    }
    after = {
        "configuration": {"prompt": {"version": "v28"}},
        "case_pass_counts": _counts(kept=(3, 3), broken=(0, 3), fixed=(3, 3)),
    }

    output = "\n".join(module.render(before, after, "ttp_test_calls"))

    assert "REGRESSED: broken" in output
    assert "effective_clusters: 3" in output
    assert "prompt.version" in output


def test_main_runs_end_to_end(tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
    module = _load()
    before = tmp_path / "before.json"
    after = tmp_path / "after.json"
    for path, counts in (
        (before, _counts(a=(1, 3))),
        (after, _counts(a=(3, 3))),
    ):
        path.write_text(
            json.dumps({"configuration": {}, "case_pass_counts": counts}),
            encoding="utf-8",
            newline="\n",
        )

    snapshots = {path: path.read_bytes() for path in (before, after)}

    assert module.main([str(before), str(after)]) == 0
    assert "+0.67" in capsys.readouterr().out
    assert {path: path.read_bytes() for path in (before, after)} == snapshots
