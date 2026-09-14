"""Offline contract comparisons and bounded semantic review protocol."""

import json
from copy import deepcopy
from itertools import combinations
from uuid import UUID

import pytest

from cli_parser_agent.evaluation import (
    HarnessError,
    schema_consistency_overview,
    schema_contract_consistency,
    schema_pair_metrics,
    summarize_schema_review,
)


def obj(**properties):
    return {"type": "object", "properties": properties, "additionalProperties": False}


def sample():
    return obj(
        title={"type": "string"},
        description={"type": "string", "enum": ["PRIVATE_ONE", "PRIVATE_TWO"]},
        entries={"type": "array", "items": obj(value={"type": "integer"})},
    )


def fixtures(n=4):
    trials = [
        {
            "trial_id": f"run/demo/{i}",
            "case_id": "demo",
            "trace_id": str(UUID(int=i + 1)),
            "generation_success": True,
            "proposal_revalidated": True,
        }
        for i in range(n)
    ]
    metrics = schema_contract_consistency([(t["trial_id"], sample()) for t in trials])
    summary = {
        "mode": "schema-only",
        "schema_metrics_version": 2,
        "run_id": "run",
        "planned_trials_per_case": n,
        "trials": trials,
        "contract_consistency": {"demo": metrics},
    }
    reviews = [
        {
            "trial_id": t["trial_id"],
            "case_id": "demo",
            "trace_id": t["trace_id"],
            "span_ids": [],
            "overall": "acceptable",
            "categories": [],
            "paths": [],
            "dimensions": dict.fromkeys(
                [
                    "naming",
                    "coverage",
                    "decomposition",
                    "structure",
                    "types_required",
                    "value_boundaries",
                    "overconstraint",
                ],
                "passed",
            ),
        }
        for t in trials
    ]
    pairs = [
        {
            "case_id": "demo",
            "left_trial_id": a["trial_id"],
            "right_trial_id": b["trial_id"],
            "annotation_semantics": "equivalent",
            "judgment": "both_reasonable",
            "categories": [],
            "paths": [],
        }
        for a, b in combinations(trials, 2)
    ]
    return summary, {
        "review_version": 1,
        "run_id": "run",
        "trials": reviews,
        "pairs": pairs,
    }


def test_order_annotations_and_business_annotation_names():
    a = sample()
    b = deepcopy(a)
    b["properties"] = dict(reversed(list(b["properties"].items())))
    b["properties"]["description"]["enum"].reverse()
    b["required"] = []
    b["description"] = "PRIVATE revised capture instructions"
    m = schema_pair_metrics(a, b)
    assert m["contract_equal"] and m["structure_equal"]
    assert m["annotation_difference_count"] == 1
    b["properties"]["title"]["type"] = "integer"
    assert not schema_pair_metrics(a, b)["contract_equal"]
    b = deepcopy(a)
    del b["properties"]["description"]
    assert schema_pair_metrics(a, b)["path_difference_count"] == 1
    a["required"] = ["title", "description"]
    b = deepcopy(a)
    b["required"].reverse()
    assert schema_pair_metrics(a, b)["contract_equal"]


@pytest.mark.parametrize(
    "key,value",
    [
        ("enum", [2, 3]),
        ("minimum", 2),
        ("maximum", 6),
        ("exclusiveMinimum", 1),
        ("exclusiveMaximum", 7),
        ("multipleOf", 2),
        ("minLength", 1),
        ("maxLength", 9),
        ("minItems", 1),
        ("maxItems", 9),
        ("additionalProperties", True),
    ],
)
def test_each_supported_constraint_is_compared(key, value):
    a = sample()
    if key in {"minItems", "maxItems"}:
        a["properties"]["title"] = {"type": "array", "items": {"type": "string"}}
    elif key in {
        "minimum",
        "maximum",
        "exclusiveMinimum",
        "exclusiveMaximum",
        "multipleOf",
        "enum",
    }:
        a["properties"]["title"] = {"type": "number"}
    b = deepcopy(a)
    node = b if key == "additionalProperties" else b["properties"]["title"]
    node[key] = value
    m = schema_pair_metrics(a, b)
    assert m["structure_equal"] and not m["contract_equal"]
    assert m["constraint_difference_count"] == 1


def test_macro_and_pair_weighted_rates_are_distinct():
    a = sample()
    b = deepcopy(a)
    b["properties"]["title"]["minLength"] = 1
    first = schema_contract_consistency([(str(i), a) for i in range(4)])
    second = schema_contract_consistency([("a", a), ("b", b)])
    combined = schema_consistency_overview({"first": first, "second": second})
    assert combined["macro_contract_consistency"] == 0.5
    assert combined["weighted_contract_consistency"] == 6 / 7
    assert combined["differences"]["constraint"]["affected_pairs"] == 1
    assert combined["differences"]["constraint"]["weighted_affected_pair_rate"] == 1 / 7
    assert second["contract_variants"] == 2
    assert second["dominant_contract_share"] == 0.5


def test_boolean_enum_is_not_numeric_and_numeric_representation_equal():
    a, b = (
        obj(flag={"type": "boolean", "enum": [True]}),
        obj(flag={"type": "boolean", "enum": [1]}),
    )
    assert not schema_pair_metrics(a, b)["contract_equal"]
    a["properties"]["flag"]["enum"] = [1.0]
    assert schema_pair_metrics(a, b)["contract_equal"]


@pytest.mark.parametrize(
    "change", ["rename", "move", "add", "split", "container", "scalar", "required"]
)
def test_differences_have_precise_node_denominators(change):
    a = sample()
    b = deepcopy(a)
    props = b["properties"]
    if change == "rename":
        props["heading"] = props.pop("title")
    elif change == "move":
        props["entries"]["items"]["properties"]["title"] = props.pop("title")
    elif change == "add":
        props["extra"] = {"type": "string"}
    elif change == "split":
        props.pop("title")
        props.update(first={"type": "string"}, last={"type": "string"})
    elif change == "container":
        props["entries"] = obj(value={"type": "integer"})
    elif change == "scalar":
        props["entries"]["items"]["properties"]["value"]["type"] = "number"
    else:
        props["entries"]["items"]["required"] = ["value"]
    m = schema_pair_metrics(a, b)
    assert not m["contract_equal"]
    assert m["required_comparable_nodes"] <= m["type_comparable_nodes"]
    if change in {"scalar", "required"}:
        assert m["path_set_equal"]
        assert (
            m[f"{change if change == 'required' else 'scalar_type'}_difference_count"]
            == 1
        )
    elif change == "container":
        assert m["container_type_difference_count"] == 1
    else:
        assert not m["path_set_equal"]
    assert schema_pair_metrics(b, a) == m


@pytest.mark.parametrize("n", [0, 1, 4])
def test_pair_count_stable_order_and_safe_projection(n):
    proposals = [(f"trial-{i}", sample()) for i in range(n)]
    m = schema_contract_consistency(proposals)
    assert m == schema_contract_consistency(list(reversed(proposals)))
    assert m["pair_count"] == n * (n - 1) // 2
    assert m["pairwise_contract_consistency"] == (1 if n > 1 else None)
    assert "PRIVATE" not in json.dumps(m) and "/entries" not in json.dumps(m)
    overview = schema_consistency_overview({"a": m})
    assert overview["weighted_contract_consistency"] == (1 if n > 1 else None)


def test_complete_missing_unknown_and_historical_reviews():
    summary, review = fixtures()
    result = summarize_schema_review(summary, review)
    assert result["planned_pairs"] == 6
    assert result["planned_pair_confirmed_rate"] == 1
    assert result["all_repeats_reasonable_consistent"] == {"demo": True}
    review["pairs"][0]["annotation_semantics"] = "unknown"
    result = summarize_schema_review(summary, review)
    assert result["unknown_annotation_pairs"] == 1
    assert result["confirmed_reasonable_consistent_pairs"] == 5
    review["pairs"].pop()
    review["trials"].pop()
    result = summarize_schema_review(summary, review)
    assert result["missing_trial_reviews"] == 1
    assert result["missing_pair_reviews"] == 1
    assert not result["review_complete"]
    del summary["schema_metrics_version"]
    del summary["contract_consistency"]
    result = summarize_schema_review(summary, review)
    assert result["planned_pair_confirmed_rate"] is None
    assert result["all_repeats_reasonable_consistent"]["demo"] is None


def test_same_wrong_contract_is_not_reasonable_consistency():
    summary, review = fixtures(2)
    for row in review["trials"]:
        row["overall"] = "needs_revision"
        row["dimensions"]["value_boundaries"] = "issue"
        row["categories"] = ["placeholder"]
        row["paths"] = ["/entries/*/value"]
    review["pairs"][0]["judgment"] = "at_least_one_issue"
    result = summarize_schema_review(summary, review)
    assert result["reasonable_proposal_rate"] == 0
    assert result["planned_pair_confirmed_rate"] == 0
    assert result["reasonable_pair_consistency"] is None


@pytest.mark.parametrize(
    "change",
    [
        "duplicate_trial",
        "duplicate_pair",
        "run",
        "trace",
        "span",
        "case",
        "unknown",
        "prose",
        "long_path",
        "many_paths",
        "malicious_path",
        "trial",
        "cross_case",
        "insufficient",
        "failed",
        "revalidation",
        "contradiction",
        "pair_missing",
    ],
)
def test_invalid_review_rejected_without_echoing_content(change):
    summary, review = fixtures()
    row = review["trials"][0]
    if change == "duplicate_trial":
        review["trials"][1] = deepcopy(row)
    elif change == "duplicate_pair":
        review["pairs"][1] = deepcopy(review["pairs"][0])
    elif change == "run":
        review["run_id"] = "PRIVATE"
    elif change == "trace":
        row["trace_id"] = str(UUID(int=999))
    elif change == "span":
        row["span_ids"] = [{"PRIVATE": "body"}]
    elif change == "case":
        row["case_id"] = "PRIVATE"
    elif change == "unknown":
        row["categories"] = ["PRIVATE"]
    elif change == "prose":
        row["text"] = "PRIVATE"
    elif change == "long_path":
        row["paths"] = ["/" + "a" * 2048]
    elif change == "many_paths":
        row["paths"] = [f"/field{i}" for i in range(25)]
    elif change == "malicious_path":
        row["paths"] = ["/value\nPRIVATE"]
    elif change == "trial":
        row["trial_id"] = "PRIVATE"
    elif change == "cross_case":
        review["pairs"][0]["case_id"] = "other"
    elif change == "insufficient":
        row["dimensions"]["coverage"] = "insufficient_evidence"
    elif change == "failed":
        summary["trials"][0]["generation_success"] = False
    elif change == "revalidation":
        summary["trials"][0]["proposal_revalidated"] = False
    elif change == "contradiction":
        review["pairs"][0]["judgment"] = "at_least_one_issue"
    else:
        summary["contract_consistency"]["demo"]["pairs"].pop(0)
    with pytest.raises(HarnessError) as exc:
        summarize_schema_review(summary, review)
    assert "PRIVATE" not in str(exc.value)


def test_failed_proposal_stays_in_planned_denominator():
    summary, review = fixtures(2)
    summary["trials"][1].update(generation_success=False, proposal_revalidated=None)
    summary["contract_consistency"]["demo"] = schema_contract_consistency(
        [(summary["trials"][0]["trial_id"], sample())]
    )
    review["trials"][1]["overall"] = "unjudgeable"
    review["trials"][1]["dimensions"] = dict.fromkeys(
        review["trials"][1]["dimensions"], "insufficient_evidence"
    )
    review["pairs"] = []
    result = summarize_schema_review(summary, review)
    assert result["generation_failed_trials"] == 1
    assert result["reasonable_proposal_rate"] == 0.5
    assert result["valid_pairs"] == 0 and result["planned_pairs"] == 1
    assert result["planned_pair_confirmed_rate"] == 0
