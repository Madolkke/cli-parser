from __future__ import annotations

import json

import pytest

from cli_parser_agent.ttp_generation.contracts import FieldEvidence
from cli_parser_agent.ttp_generation.validation import (
    schema_leaf_paths,
    validate_records_against_schema,
    validate_result_schema,
    validate_schema_proposal,
)


def _inventory_schema() -> dict:
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "type": "object",
        "properties": {
            "hostname": {"type": "string", "minLength": 1},
            "interfaces": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "name": {"type": "string"},
                        "mtu": {"type": "integer", "minimum": 0},
                    },
                    "required": ["name", "mtu"],
                    "additionalProperties": False,
                },
            },
        },
        "required": ["hostname", "interfaces"],
        "additionalProperties": False,
    }


def _codes(issues) -> set[str]:
    return {issue.code for issue in issues}


def test_valid_schema_and_evidence_are_accepted() -> None:
    outputs = ["Hostname: edge_1\neth0 up mtu 1500"]
    evidence = [
        FieldEvidence(path="/hostname", output_index=0, excerpt="edge_1"),
        FieldEvidence(path="/interfaces/*/name", output_index=0, excerpt="eth0"),
        FieldEvidence(path="/interfaces/*/mtu", output_index=0, excerpt="1500"),
    ]

    assert schema_leaf_paths(_inventory_schema()) == {
        "/hostname",
        "/interfaces/*/name",
        "/interfaces/*/mtu",
    }
    assert validate_schema_proposal(_inventory_schema(), evidence, outputs) == []


def test_scalar_array_leaf_can_be_evidenced() -> None:
    schema = {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "type": "object",
        "properties": {
            "values": {"type": "array", "items": {"type": "string"}},
        },
        "required": ["values"],
        "additionalProperties": False,
    }

    issues = validate_schema_proposal(
        schema,
        [FieldEvidence(path="/values/*", output_index=0, excerpt="one")],
        ["one\ntwo"],
    )

    assert issues == []


def test_evidence_must_match_a_leaf_and_full_source() -> None:
    evidence = [
        FieldEvidence(path="/hostname", output_index=0, excerpt="not present"),
        FieldEvidence(path="/interfaces/*/name", output_index=0, excerpt="eth0"),
        FieldEvidence(path="/unknown", output_index=0, excerpt="edge_1"),
    ]

    issues = validate_schema_proposal(
        _inventory_schema(),
        evidence,
        ["Hostname: edge_1\neth0 up mtu 1500"],
    )

    assert _codes(issues) == {
        "schema.evidence_missing",
        "schema.evidence_not_found",
        "schema.evidence_unknown_path",
    }
    missing_paths = {
        issue.path for issue in issues if issue.code == "schema.evidence_missing"
    }
    assert missing_paths == {
        "/hostname",
        "/interfaces/*/mtu",
    }


def test_missing_evidence_reports_matching_sample_indexes_without_values() -> None:
    excerpt = "edge_1"
    issues = validate_schema_proposal(
        _inventory_schema(),
        [
            FieldEvidence(path="/hostname", output_index=1, excerpt=excerpt),
            FieldEvidence(path="/interfaces/*/name", output_index=0, excerpt="eth0"),
            FieldEvidence(path="/interfaces/*/mtu", output_index=0, excerpt="1500"),
        ],
        ["Hostname: edge_1\neth0 up mtu 1500", "Hostname: branch_1"],
    )

    issue = next(item for item in issues if item.code == "schema.evidence_not_found")
    assert issue.details == {
        "matching_output_indexes": [0],
        "required_action": "change_output_index",
    }
    assert excerpt not in json.dumps(issue.model_dump(mode="json"))


def test_missing_evidence_requires_replacement_when_no_sample_contains_it() -> None:
    issues = validate_schema_proposal(
        _inventory_schema(),
        [
            FieldEvidence(path="/hostname", output_index=0, excerpt="invented"),
            FieldEvidence(path="/interfaces/*/name", output_index=0, excerpt="eth0"),
            FieldEvidence(path="/interfaces/*/mtu", output_index=0, excerpt="1500"),
        ],
        ["Hostname: edge_1\neth0 up mtu 1500"],
    )

    issue = next(item for item in issues if item.code == "schema.evidence_not_found")
    assert issue.details == {
        "matching_output_indexes": [],
        "required_action": "replace_excerpt",
    }


def test_evidence_rejects_duplicate_leaf_paths() -> None:
    evidence = [
        FieldEvidence(path="/hostname", output_index=0, excerpt="edge_1"),
        FieldEvidence(path="/hostname", output_index=1, excerpt="branch_1"),
        FieldEvidence(path="/interfaces/*/name", output_index=0, excerpt="eth0"),
        FieldEvidence(path="/interfaces/*/mtu", output_index=0, excerpt="1500"),
    ]

    issues = validate_schema_proposal(
        _inventory_schema(),
        evidence,
        ["Hostname: edge_1\neth0 up mtu 1500", "Hostname: branch_1"],
    )

    assert _codes(issues) == {"schema.evidence_duplicate_path"}


def test_required_mismatch_reports_schema_field_names_only() -> None:
    schema = _inventory_schema()
    schema["required"] = ["hostname", "unknown"]

    issue = next(
        item
        for item in validate_result_schema(schema)
        if item.code == "schema.required_mismatch"
    )

    assert issue.details == {
        "missing_required": ["interfaces"],
        "unknown_required": ["unknown"],
    }


@pytest.mark.parametrize(
    ("mutation", "expected_code"),
    [
        (lambda schema: schema.update(type="array"), "schema.root_not_object"),
        (
            lambda schema: schema["properties"].update(BadName={"type": "string"}),
            "schema.invalid_property_name",
        ),
        (
            lambda schema: schema.update(additionalProperties=True),
            "schema.object_not_closed",
        ),
        (
            lambda schema: schema.update(required=["hostname"]),
            "schema.required_mismatch",
        ),
        (
            lambda schema: schema.update(anyOf=[{"type": "object"}]),
            "schema.forbidden_keyword",
        ),
        (
            lambda schema: schema["properties"]["hostname"].update(
                {"$ref": "https://example.invalid/schema.json"},
            ),
            "schema.forbidden_keyword",
        ),
    ],
)
def test_schema_subset_rejects_open_ambiguous_or_remote_constructs(
    mutation,
    expected_code: str,
) -> None:
    schema = _inventory_schema()
    mutation(schema)

    assert expected_code in _codes(validate_result_schema(schema))


def test_schema_must_explicitly_declare_draft_2020_12() -> None:
    schema = _inventory_schema()
    del schema["$schema"]

    assert "schema.draft_required" in _codes(validate_result_schema(schema))

    schema["$schema"] = "https://json-schema.org/draft/2020-12/schema#"
    assert "schema.wrong_draft" in _codes(validate_result_schema(schema))


def test_schema_complexity_limits_are_enforced() -> None:
    too_many = {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "type": "object",
        "properties": {f"field_{index}": {"type": "string"} for index in range(257)},
        "required": [f"field_{index}" for index in range(257)],
        "additionalProperties": False,
    }
    issues = validate_result_schema(too_many)
    assert "schema.property_limit_exceeded" in _codes(issues)

    nested: dict = {"type": "string"}
    for index in range(17):
        field = f"level_{index}"
        nested = {
            **(
                {"$schema": "https://json-schema.org/draft/2020-12/schema"}
                if index == 16
                else {}
            ),
            "type": "object",
            "properties": {field: nested},
            "required": [field],
            "additionalProperties": False,
        }
    assert "schema.depth_exceeded" in _codes(validate_result_schema(nested))

    oversized = _inventory_schema()
    oversized["description"] = "x" * (64 * 1024)
    assert _codes(validate_result_schema(oversized)) == {"schema.too_large"}


def test_untrusted_long_property_names_produce_bounded_issues() -> None:
    field = "a" * 2_500
    schema = {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "type": "object",
        "properties": {field: {"type": "string"}},
        "required": [field],
        "additionalProperties": False,
    }

    issues = validate_result_schema(schema)

    assert "schema.invalid_property_name" in _codes(issues)
    assert all(issue.path is None or len(issue.path) <= 2_048 for issue in issues)


def test_callers_can_tighten_but_not_loosen_schema_limits() -> None:
    schema = _inventory_schema()

    assert "schema.too_large" in _codes(
        validate_result_schema(schema, max_schema_bytes=100),
    )
    assert "schema.depth_exceeded" in _codes(
        validate_result_schema(schema, max_schema_depth=3),
    )
    assert "schema.property_limit_exceeded" in _codes(
        validate_result_schema(schema, max_schema_properties=2),
    )

    oversized = _inventory_schema()
    oversized["description"] = "x" * (64 * 1024)
    assert "schema.too_large" in _codes(
        validate_result_schema(oversized, max_schema_bytes=10**9),
    )


@pytest.mark.parametrize(
    "keyword",
    ["max_schema_bytes", "max_schema_depth", "max_schema_properties"],
)
def test_schema_limits_must_be_positive(keyword: str) -> None:
    with pytest.raises(ValueError, match="positive integer"):
        validate_result_schema(_inventory_schema(), **{keyword: 0})


def test_record_validation_is_value_safe_and_reports_paths() -> None:
    secret = "token-super-secret"
    issues = validate_records_against_schema(
        [{"hostname": secret, "interfaces": [{"name": "eth0", "mtu": "1500"}]}],
        _inventory_schema(),
    )

    assert _codes(issues) == {"schema.record_mismatch"}
    assert issues[0].path == "/interfaces/*/mtu"
    assert secret not in json.dumps([issue.model_dump() for issue in issues])


def test_record_validation_deduplicates_and_names_missing_fields() -> None:
    issues = validate_records_against_schema(
        [{"hostname": "edge_1", "interfaces": [{}]}],
        _inventory_schema(),
    )

    required = [issue for issue in issues if issue.details["keyword"] == "required"]
    assert len(required) == 1
    assert required[0].path == "/interfaces/*"
    assert required[0].details == {
        "keyword": "required",
        "missing_required": ["mtu", "name"],
    }


def test_record_validation_does_not_echo_unexpected_property_names() -> None:
    secret_name = "secret_" + ("x" * 10_000)
    issues = validate_records_against_schema(
        [
            {
                "hostname": "edge_1",
                "interfaces": [],
                "inventory": [],
                "unsafe-key": "ignored in feedback",
                secret_name: "ignored in feedback",
            },
        ],
        _inventory_schema(),
    )

    issue = next(
        item for item in issues if item.details["keyword"] == "additionalProperties"
    )
    assert issue.details == {
        "keyword": "additionalProperties",
        "unexpected_property_count": 3,
    }
    assert secret_name not in json.dumps([item.model_dump() for item in issues])


def test_record_validation_fairly_deduplicates_repeated_array_errors() -> None:
    schema = {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "type": "object",
        "properties": {
            "rows": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {"name": {"type": "string"}},
                    "required": ["name"],
                    "additionalProperties": False,
                },
            },
        },
        "required": ["rows"],
        "additionalProperties": False,
    }

    issues = validate_records_against_schema(
        [{"rows": [{} for _ in range(150)]}, {"rows": [{}]}],
        schema,
    )

    assert len(issues) == 2
    assert {issue.output_index for issue in issues} == {0, 1}
    assert {issue.path for issue in issues} == {"/rows/*"}
