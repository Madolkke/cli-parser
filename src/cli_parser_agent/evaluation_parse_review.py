"""Bounded, offline content review for generated-contract end-to-end runs."""

from __future__ import annotations

import re
from collections import Counter
from uuid import UUID

from .evaluation import HarnessError, summarize_schema_review

PARSE_REVIEW_DIMENSIONS = frozenset(
    {"entities", "coverage", "value_fidelity", "empty_missing", "order"}
)
PARSE_REVIEW_CATEGORIES = frozenset(
    {
        "entity_count",
        "entity_placement",
        "coverage",
        "value_changed",
        "value_boundary",
        "empty_slot",
        "missing_key",
        "order",
        "unresolved_mapping",
        "execution_failure",
    }
)
_PATH = re.compile(
    r"/(?:[a-z][a-z0-9]*(?:_[a-z0-9]+)*|\*)"
    r"(?:/(?:[a-z][a-z0-9]*(?:_[a-z0-9]+)*|\*))*|/"
)


def summarize_parse_review(summary, review, schema_review):
    """Validate exact identities and combine independent, semantic evidence."""

    def require(value):
        if not value:
            raise HarnessError("invalid parse review structure or reference")

    def keys(node, expected):
        require(isinstance(node, dict) and set(node) == set(expected))

    def uuid(value):
        try:
            require(isinstance(value, str) and str(UUID(value)) == value)
        except (ValueError, AttributeError):
            raise HarnessError("invalid parse review identifier") from None

    require(isinstance(summary, dict) and summary.get("mode") == "end-to-end")
    schema_summary = summarize_schema_review(summary, schema_review)
    keys(review, {"review_version", "run_id", "trials"})
    require(type(review["review_version"]) is int and review["review_version"] == 1)
    require(review["run_id"] == schema_summary["run_id"])
    require(isinstance(review["trials"], list))
    trials = {trial["trial_id"]: trial for trial in summary["trials"]}
    require(len(review["trials"]) <= len(trials))
    reviewed = {}
    for row in review["trials"]:
        keys(
            row,
            {
                "trial_id",
                "case_id",
                "trace_id",
                "span_ids",
                "dimensions",
                "overall",
                "categories",
                "paths",
            },
        )
        tid = row["trial_id"]
        require(isinstance(tid, str) and tid in trials and tid not in reviewed)
        trial = trials[tid]
        require(row["case_id"] == trial["case_id"])
        require(row["trace_id"] == trial.get("trace_id"))
        if row["trace_id"] is not None:
            uuid(row["trace_id"])
        require(isinstance(row["span_ids"], list) and len(row["span_ids"]) <= 64)
        for span in row["span_ids"]:
            uuid(span)
        require(len(set(row["span_ids"])) == len(row["span_ids"]))
        keys(row["dimensions"], PARSE_REVIEW_DIMENSIONS)
        require(
            all(
                isinstance(v, str)
                and v in {"passed", "issue", "insufficient_evidence", "not_applicable"}
                for v in row["dimensions"].values()
            )
        )
        require(
            isinstance(row["overall"], str)
            and row["overall"] in {"acceptable", "needs_revision", "unjudgeable"}
        )
        require(isinstance(row["categories"], list))
        require(
            all(
                isinstance(c, str) and c in PARSE_REVIEW_CATEGORIES
                for c in row["categories"]
            )
        )
        require(len(row["categories"]) == len(set(row["categories"])))
        paths = row["paths"]
        require(isinstance(paths, list) and len(paths) <= 24)
        require(
            all(
                isinstance(p, str)
                and len(p) <= 2048
                and _PATH.fullmatch(p)
                and all(len(part) <= 120 for part in p.split("/"))
                for p in paths
            )
        )
        require(len(paths) == len(set(paths)))
        if row["overall"] == "acceptable":
            acceptance = trial.get("independent_acceptance")
            require(trial["generation_success"] is True)
            require(isinstance(acceptance, dict) and acceptance.get("valid") is True)
            require(row["trace_id"] is not None)
            require("issue" not in row["dimensions"].values())
            require("insufficient_evidence" not in row["dimensions"].values())
            # Every parse category describes a defect; unlike schema pair
            # reviews there is no harmless naming/wording difference category.
            require(not row["categories"] and not row["paths"])
            # Major entities, business coverage and fidelity are never waived.
            require(
                all(
                    row["dimensions"][d] == "passed"
                    for d in {"entities", "coverage", "value_fidelity"}
                )
            )
        elif row["overall"] == "needs_revision":
            require("issue" in row["dimensions"].values())
            require(bool(row["categories"]))
        reviewed[tid] = row

    schema_acceptable = {
        r["trial_id"] for r in schema_summary["trials"] if r["overall"] == "acceptable"
    }
    joint = {
        tid
        for tid, row in reviewed.items()
        if tid in schema_acceptable and row["overall"] == "acceptable"
    }
    cases = sorted({trial["case_id"] for trial in trials.values()})
    counts = Counter(row["overall"] for row in reviewed.values())
    planned = schema_summary["planned_trials"]
    return {
        "review_version": 1,
        "run_id": review["run_id"],
        "mode": "end-to-end",
        "parseability": "reviewed" if len(reviewed) == planned else "review_incomplete",
        "planned_trials": planned,
        "reviewed_trials": len(reviewed),
        "missing_trial_reviews": planned - len(reviewed),
        "acceptable_trials": counts["acceptable"],
        "needs_revision_trials": counts["needs_revision"],
        "unjudgeable_trials": counts["unjudgeable"],
        "end_to_end_joint_pass_count": len(joint),
        "end_to_end_joint_pass_rate": len(joint) / planned if planned else None,
        "per_case_joint_pass_count": {
            case: sum(trials[tid]["case_id"] == case for tid in joint) for case in cases
        },
        "schema_review_complete": schema_summary["review_complete"],
        "review_complete": len(reviewed) == planned
        and schema_summary["review_complete"],
        "schema": schema_summary,
        "trials": list(reviewed.values()),
    }
