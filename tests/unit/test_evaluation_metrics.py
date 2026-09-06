"""Offline tests for safe evaluation metric projections."""

from __future__ import annotations

import pytest

from cli_parser_agent.evaluation import (
    aggregate_trial_scores,
    attach_human_reviews,
    issue_taxonomy,
    project_candidate_quality,
    project_candidate_trajectory,
    project_human_reviews,
    score_executor_output,
    score_records_by_input,
    score_ttp_template_output,
    summarize_span_metrics,
    wilson_interval,
)


def test_corpus_leaf_metrics_match_per_input_metrics_for_a_perfect_candidate() -> None:
    """TestSetCase.expected_records is a tuple, and the leaf walker only
    recursed into list, so the whole corpus collapsed to a single leaf whose
    value was the entire JSON blob. Overlap with the actual (a real list) was
    then always zero, which pinned corpus leaf_f1 at 0.0 on every trial -- even
    ones scoring records_exact_match 1.0, because that path compares canonical
    JSON and is blind to the list/tuple distinction.
    """
    records = [
        {"lines": [{"port": "A1", "status": "up"}]},
        {"lines": [{"port": "B2", "status": "down"}]},
    ]
    result = score_ttp_template_output(
        {
            "generation_result": {
                "status": "success",
                "artifact": {"records": list(records)},
                "metadata": {},
            },
            "independent_acceptance": {"valid": True},
        },
        tuple(records),
    )

    metrics = result["metrics"]
    assert metrics["records_exact_match"] == 1.0
    assert metrics["input_leaf_f1_macro"] == 1.0
    assert metrics["leaf_precision"] == 1.0
    assert metrics["leaf_recall"] == 1.0
    assert metrics["leaf_f1"] == 1.0


def test_score_ttp_template_output_projects_history_compaction_metrics() -> None:
    result = score_ttp_template_output(
        {
            "generation_result": {
                "status": "success",
                "artifact": {"records": [{"value": "one"}]},
                "metadata": {
                    "ttp_history_compaction_events": 2,
                    "ttp_history_compacted_interactions": 5,
                    "ttp_history_compacted_input_chars": 120,
                    "ttp_history_compacted_result_chars": 340,
                    "ttp_history_compaction_skips": 1,
                },
            },
            "independent_acceptance": {"valid": True},
        },
        ({"value": "one"},),
    )

    metrics = result["metrics"]
    assert metrics["ttp_history_compaction_events"] == 2.0
    assert metrics["ttp_history_compacted_interactions"] == 5.0
    assert metrics["ttp_history_compacted_input_chars"] == 120.0
    assert metrics["ttp_history_compacted_result_chars"] == 340.0
    assert metrics["ttp_history_compaction_skips"] == 1.0


def test_score_ttp_template_output_omits_unobserved_history_compaction_metrics() -> (
    None
):
    result = score_ttp_template_output({}, ())

    for name in (
        "ttp_history_compaction_events",
        "ttp_history_compacted_interactions",
        "ttp_history_compacted_input_chars",
        "ttp_history_compacted_result_chars",
        "ttp_history_compaction_skips",
    ):
        assert name not in result["metrics"]


def test_wilson_interval_is_bounded_and_handles_empty_samples() -> None:
    lower, upper = wilson_interval(5, 10)

    assert 0.0 < lower < 0.5 < upper < 1.0
    assert wilson_interval(0, 0) == (0.0, 0.0)
    full_lower, full_upper = wilson_interval(10, 10)
    assert 0.0 < full_lower < full_upper <= 1.0


def test_aggregate_trial_scores_includes_binary_wilson_summary() -> None:
    result = aggregate_trial_scores(
        [
            {"metrics": {"candidate_pass": 1.0, "elapsed_seconds": 2.0}},
            {"metrics": {"candidate_pass": 0.0, "elapsed_seconds": 4.0}},
            {"metrics": {"candidate_pass": 1.0, "elapsed_seconds": 3.0}},
        ],
        metric_names=("candidate_pass", "elapsed_seconds"),
    )

    assert result["trial_count"] == 3
    assert result["metrics"]["elapsed_seconds"]["mean"] == 3.0
    assert result["binary"]["candidate_pass"]["successes"] == 2
    assert result["binary"]["candidate_pass"]["observations"] == 3
    assert result["binary"]["candidate_pass"]["wilson_95"]["lower"] >= 0.0
    assert result["binary"]["candidate_pass"]["wilson_95"]["upper"] <= 1.0
    assert "elapsed_seconds" not in result["binary"]


def test_score_records_by_input_preserves_alignment_and_marks_missing_output() -> None:
    diagnostics = score_records_by_input(
        [{"items": [{"name": "one"}]}],
        [
            {"items": [{"name": "one"}]},
            {"items": [{"name": "two"}]},
        ],
    )

    assert diagnostics[0]["records_exact_match"] is True
    assert diagnostics[0]["leaf_f1"] == 1.0
    assert diagnostics[1]["actual_present"] is False
    assert diagnostics[1]["records_exact_match"] is False
    assert diagnostics[1]["leaf_recall"] == 0.0


def test_score_records_by_input_distinguishes_empty_string_from_missing_key() -> None:
    diagnostics = score_records_by_input(
        [{"item": {"name": ""}}],
        [{"item": {}}],
    )

    assert diagnostics[0]["records_exact_match"] is False
    assert diagnostics[0]["actual_empty_string_count"] == 1
    assert diagnostics[0]["actual_leaf_count"] == 1
    assert diagnostics[0]["expected_leaf_count"] == 0


def test_issue_taxonomy_is_coarse_and_deterministic() -> None:
    taxonomy = issue_taxonomy(
        [
            "schema.record_mismatch",
            "schema.record_mismatch",
            "ttp.worker_timeout",
            "record_count_mismatch",
            "unclassified_code",
        ],
    )

    assert taxonomy["total"] == 5
    assert taxonomy["unique"] == 4
    assert taxonomy["domains"] == {
        "records": 1,
        "schema": 2,
        "ttp": 1,
        "unknown": 1,
    }
    assert taxonomy["codes"]["schema.record_mismatch"] == 2


def test_candidate_projection_excludes_template_and_capture_values() -> None:
    spans = [
        {
            "name": "submit_ttp_template",
            "output": {
                "accepted": False,
                "ttp_submission": 1,
                "issues": [{"code": "record_count_mismatch", "message": "secret"}],
                "capture": {
                    "available": True,
                    "complete": True,
                    "records": [{"name": "secret-value"}],
                },
            },
            "input": {"ttp_template": "secret-template"},
        },
        {
            "name": "submit_ttp_template",
            "start_time": 2.0,
            "end_time": 3.0,
            "output": {
                "accepted": True,
                "validated_candidate_available": True,
                "ttp_submission": 2,
                "issues": [],
                "capture": {
                    "available": True,
                    "complete": True,
                    "records": [{"name": "secret-value"}],
                },
            },
        },
        {
            "name": "finish_generation",
            "start_time": 4.0,
            "output": {"generation_finished": True, "accepted": True},
        },
    ]

    quality = project_candidate_quality(spans[1], expected_records=[{"name": "x"}])
    trajectory = project_candidate_trajectory(
        spans,
        expected_records=[{"name": "x"}],
    )

    assert quality["accepted"] is True
    assert quality["capture_record_count"] == 1
    assert "secret-template" not in repr(trajectory)
    assert "secret-value" not in repr(trajectory)
    assert "message" not in trajectory["candidates"][0]
    assert trajectory["submission_count"] == 2
    assert trajectory["accepted_count"] == 1
    assert trajectory["first_accepted_submission"] == 2
    assert trajectory["finish_after_first_accepted"] is True


@pytest.mark.parametrize(
    "facts",
    [
        {"schema_frozen": True, "entered_ttp": False, "finish_called": False},
        {
            "valid_ttp_candidate": False,
            "finish_called": True,
            "finish_succeeded": False,
        },
        {"valid_ttp_candidate": True, "finish_called": True, "finish_succeeded": False},
        {
            "finish_succeeded": True,
            "final_acceptance_started": True,
            "final_acceptance_passed": False,
        },
    ],
)
def test_scoring_uses_execution_facts_without_changing_correctness(facts: dict) -> None:
    output = {
        "generation_result": {
            "status": "success",
            "issues": [],
            "artifact": {"records": [{}], "result_schema": {}},
            "last_attempt": {"ttp_template": "untrusted"},
            "metadata": {
                "termination_reason": "success",
                "model_attempts_observed": 3,
                "model_retries_observed": 1,
            },
        },
        "independent_acceptance": {"valid": True},
    }
    target = {"records": [{}], "schema_contract": []}
    for score in (
        lambda value: score_ttp_template_output(value, [{}])["metrics"],
        lambda value: score_executor_output(value, target),
    ):
        original = score(output)
        observed = score({**output, "execution_facts": {**facts, "payload": "secret"}})
        assert "finish_called" not in original
        assert observed["candidate_pass"] == original["candidate_pass"]
        assert observed["model_attempts_observed"] == 3.0
        assert observed["model_retries_observed"] == 1.0
        assert "payload" not in observed
        for key, value in facts.items():
            assert observed[key] == float(value)


def test_execution_facts_survive_missing_result_and_reject_nonbooleans() -> None:
    output = {
        "execution_facts": {
            "entered_ttp": False,
            "finish_called": 1,
            "finish_succeeded": "true",
        }
    }
    for score in (
        score_ttp_template_output(output, [])["metrics"],
        score_executor_output(output, {}),
    ):
        assert score["entered_ttp"] == 0.0
        assert "finish_called" not in score
        assert "finish_succeeded" not in score
    aggregate = aggregate_trial_scores(
        [
            {"metrics": {"finish_called": 1.0}},
            {"metrics": {}},
        ]
    )
    assert aggregate["binary"]["finish_called"]["observations"] == 1


def test_unobserved_resources_do_not_become_zero_measurements() -> None:
    for result in (
        score_ttp_template_output({}, [])["metrics"],
        score_executor_output({}, {}),
    ):
        for name in (
            "model_attempts_observed",
            "model_retries_observed",
            "elapsed_seconds",
            "agent_rounds",
            "input_tokens_total",
            "ttp_submissions",
        ):
            assert name not in result
        assert result["candidate_pass"] == 0.0
    result = score_ttp_template_output(
        {
            "generation_result": {
                "status": "failed",
                "metadata": {
                    "model_attempts_observed": 0,
                    "model_retries_observed": None,
                    "agent_rounds": float("nan"),
                    "elapsed_seconds": float("inf"),
                },
            },
            "independent_acceptance": {"valid": False},
        },
        [],
    )
    assert result["metrics"]["model_attempts_observed"] == 0.0
    for name in ("model_retries_observed", "agent_rounds", "elapsed_seconds"):
        assert name not in result["metrics"]


@pytest.mark.parametrize(
    ("accepted_end", "finish_start", "succeeded", "expected"),
    [
        (1.0, 2.0, True, True),
        (3.0, 2.0, True, False),
        (2.0, 2.0, True, False),
        (None, 2.0, True, None),
        (1.0, None, True, None),
        (1.0, 2.0, None, None),
        (1.0, 2.0, False, False),
        ("2026-09-06T00:00:01+00:00", "2026-09-06T00:00:02+00:00", True, True),
    ],
)
def test_candidate_finish_order_requires_successful_finish_and_timestamps(
    accepted_end,
    finish_start,
    succeeded,
    expected,
) -> None:
    trajectory = project_candidate_trajectory(
        [
            {
                "name": "finish_generation",
                "start_time": finish_start,
                "output": {"generation_finished": succeeded, "accepted": succeeded},
            },
            {
                "name": "submit_ttp_template",
                "end_time": accepted_end,
                "output": {"accepted": True, "ttp_submission": 1},
            },
        ]
    )
    assert trajectory["finish_called"] is True
    assert trajectory["finish_succeeded"] is succeeded
    assert trajectory["finish_after_first_accepted"] is expected
    unknown = project_candidate_trajectory([])
    assert unknown["finish_called"] is None
    assert unknown["finish_succeeded"] is None
    assert unknown["finish_after_first_accepted"] is None


def test_rejected_finish_with_previous_success_is_not_successful_finish() -> None:
    result = project_candidate_trajectory(
        [
            {
                "name": "finish_generation",
                "start_time": 2.0,
                "output": {"generation_finished": True, "accepted": False},
            },
            {
                "name": "submit_ttp_template",
                "end_time": 1.0,
                "output": {"accepted": True, "ttp_submission": 1},
            },
        ]
    )
    assert result["finish_called"] is True
    assert result["finish_succeeded"] is False
    assert result["finish_after_first_accepted"] is False


def test_human_review_projection_is_bounded_and_merges_duplicate_labels() -> None:
    reviews = project_human_reviews(
        [
            {
                "attributes": {
                    "lmnr.association.properties.metadata.review_submission_index": 2,
                    "lmnr.association.properties.metadata.review_label": "repairable",
                    "lmnr.association.properties.metadata.review_dimensions": {
                        "boundary": "mixed",
                    },
                    "lmnr.association.properties.metadata.review_issue_codes": [
                        "template.header_capture",
                    ],
                },
                "output": {
                    "label": "repairable",
                    "dimensions": {"boundary": "mixed"},
                    "issue_codes": ["template.header_capture"],
                },
            },
            {
                "input": {"submission_index": 2},
                "output": {
                    "label": "reasonable",
                    "dimensions": {"security": "good"},
                    "issue_codes": [],
                },
            },
            {
                "input": {"submission_index": 3},
                "output": {
                    "label": "unreasonable",
                    "dimensions": {"boundary": "poor"},
                    "issue_codes": ["template.no_match", "secret message"],
                },
            },
            {
                "input": {"phase": "schema", "submission_index": 1},
                "output": {
                    "phase": "schema",
                    "label": "repairable",
                    "dimensions": {"field_semantics": "mixed"},
                    "issue_codes": [],
                },
            },
        ],
    )

    assert reviews["review_count"] == 4
    assert reviews["reviewed_submission_count"] == 3
    assert reviews["label_counts"] == {
        "reasonable": 1,
        "repairable": 2,
        "unreasonable": 1,
    }
    assert reviews["submissions"]["2"]["label"] == "reasonable"
    assert reviews["submissions"]["3"]["issue_codes"] == ["template.no_match"]
    assert reviews["submissions"]["schema:1"]["phase"] == "schema"

    trajectory = attach_human_reviews(
        {
            "candidates": [{"submission_index": 2}, {"submission_index": 4}],
            "schema_candidates": [{"phase": "schema", "submission_index": 1}],
        },
        reviews,
    )
    assert trajectory["candidates"][0]["human_review"]["label"] == "reasonable"
    assert "human_review" not in trajectory["candidates"][1]
    assert trajectory["schema_candidates"][0]["human_review"]["phase"] == "schema"


def test_span_summary_reports_segment_percentiles_and_context_growth() -> None:
    summary = summarize_span_metrics(
        [
            {
                "name": "ttp.generate",
                "span_type": "DEFAULT",
                "start_time": 0,
                "duration": 10,
                "input_tokens": 0,
            },
            {
                "name": "schema.phase",
                "span_type": "DEFAULT",
                "start_time": 1,
                "duration": 2,
                "input_tokens": 0,
            },
            {
                "name": "ttp.phase",
                "span_type": "DEFAULT",
                "start_time": 3,
                "duration": 7,
                "input_tokens": 0,
            },
            {
                "name": "agent.round",
                "span_type": "DEFAULT",
                "start_time": 3,
                "duration": 7,
                "input_tokens": 0,
            },
            {
                "name": "model.call",
                "span_type": "LLM",
                "start_time": 4,
                "duration": 3,
                "input_tokens": 100,
            },
            {
                "name": "model.call",
                "span_type": "LLM",
                "start_time": 5,
                "duration": 4,
                "input_tokens": 300,
            },
        ],
    )

    assert summary["segment_stats"]["LLM"]["count"] == 2
    assert summary["segment_stats"]["LLM"]["p95_seconds"] == 4.0
    assert summary["token_growth"]["max_input_tokens"] == 300.0
    assert summary["token_growth"]["growth_slope_tokens_per_call"] == 200.0
    assert summary["explained_duration_ratio"] == 0.9
    assert summary["unexplained_duration_ratio"] == pytest.approx(0.1)
