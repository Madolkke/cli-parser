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
    score_records_by_input,
    summarize_span_metrics,
    wilson_interval,
)


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
        {"name": "finish_generation", "output": {"status": "finished"}},
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
