from __future__ import annotations

import json
from typing import Any

import pytest

from cli_parser_agent.ttp_generation.agent.feedback import submission_feedback
from cli_parser_agent.ttp_generation.agent.session import GenerationSession
from cli_parser_agent.ttp_generation.validation.json_schema import (
    validate_records_against_schema,
)
from cli_parser_agent.ttp_generation.validation.ttp import inspect_ttp_template


def _render(
    issues: list[Any],
    schema: dict[str, Any] | None = None,
) -> tuple[str, dict[str, Any]]:
    session = GenerationSession(
        command_outputs=["synthetic"] * 5,
        schema_validator=lambda _: None,
        template_validator=lambda _: None,
    )
    session.frozen_schema = schema
    block = submission_feedback(
        session,
        accepted=False,
        candidate_updated=False,
        returned_record_count=0,
        issues=issues,
    )
    serialized = block.split("\n")[1]
    return serialized, json.loads(serialized)


def _object(properties: dict[str, Any], required: list[str]) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": properties,
        "required": required,
        "additionalProperties": False,
    }


def test_schema_feedback_preserves_actionable_paths_without_values() -> None:
    schema = _object(
        {
            "items": {
                "type": "array",
                "items": _object(
                    {"name": {"type": "string"}, "rating": {"type": "integer"}},
                    ["name", "rating"],
                ),
            }
        },
        ["items"],
    )
    diagnostics = validate_records_against_schema(
        [
            {"items": [{"rating": "PRIVATE_VALUE", "PRIVATE_KEY": 1}]},
            {"items": [{}]},
        ],
        schema,
    )
    serialized, feedback = _render(diagnostics, schema)
    assert "PRIVATE" not in serialized
    issues = feedback["issues"]
    assert [issue["input_index"] for issue in issues][:2] == [0, 1]
    type_issue = next(issue for issue in issues if issue["keyword"] == "type")
    assert type_issue["path"] == "/items/*/rating"
    assert type_issue["details"] == {
        "expected_type": "integer",
        "actual_type": "string",
    }
    required = next(issue for issue in issues if issue["keyword"] == "required")
    assert required["path"] == "/items/*"
    assert "name" in required["details"]["missing_required"]
    unexpected = next(
        issue for issue in issues if issue["keyword"] == "additionalProperties"
    )
    assert unexpected["details"] == {"unexpected_property_count": 1}
    assert feedback["issues_total"] == len(diagnostics)
    assert feedback["issues_omitted"] == 0


def test_untrusted_diagnostics_are_projected_by_code_and_keyword() -> None:
    schema = _object({"value": {"type": "string"}}, ["value"])
    raw = {
        "code": "schema.record_mismatch",
        "output_index": True,
        "path": "/",
        "message": "PRIVATE_MESSAGE",
        "details": {
            "keyword": "required",
            "missing_required": ["value", "PRIVATE_FIELD"],
            "exception_type": "SystemExit",
            "actual_type": "PRIVATE_TYPE",
            "actual_value": "PRIVATE_VALUE",
            "required_action": "PRIVATE_ACTION",
        },
    }
    serialized, feedback = _render(
        [
            raw,
            {**raw, "code": "private.secret"},
            {**raw, "path": "/PRIVATE_PATH", "details": {"keyword": "type"}},
            {"code": ["invalid"], "details": []},
            {
                "code": "ttp.worker_error",
                "details": {"exception_type": "PRIVATE_ERROR"},
            },
        ],
        schema,
    )
    assert "PRIVATE" not in serialized
    assert "secret" not in serialized
    assert feedback["issues"][0]["input_index"] is None
    assert feedback["issues"][0]["details"] == {
        "missing_required": ["value"],
        "missing_required_omitted": 0,
    }
    assert feedback["issues"][1] == {
        "code": "validation.unknown_issue",
        "input_index": None,
        "path": None,
        "keyword": None,
        "details": {},
    }
    assert feedback["issues"][2]["path"] is None
    assert feedback["issues"][4]["details"] == {"exception_type": "OtherError"}


def test_required_name_declared_outside_properties_remains_actionable() -> None:
    schema = _object({"value": {"type": "string"}}, ["missing"])
    diagnostics = validate_records_against_schema([{}], schema)
    _, feedback = _render(diagnostics, schema)
    assert feedback["issues"][0]["details"] == {
        "missing_required": ["missing"],
        "missing_required_omitted": 0,
    }


@pytest.mark.parametrize("invalid", [True, -1, 2**100, "PRIVATE", None, [], {}])
def test_invalid_detail_counts_and_positions_are_omitted(invalid: Any) -> None:
    _, feedback = _render(
        [
            {
                "code": "ttp.invalid_xml",
                "details": {"line": invalid, "column": invalid},
            },
            {
                "code": "schema.record_mismatch",
                "details": {
                    "keyword": "additionalProperties",
                    "unexpected_property_count": invalid,
                },
            },
        ]
    )
    assert all(not issue["details"] for issue in feedback["issues"])


def test_static_syntax_and_worker_details_are_bounded() -> None:
    diagnostics: list[Any] = list(inspect_ttp_template("<group>{{ value }}"))
    diagnostics.extend(
        [
            {
                "code": "ttp.worker_error",
                "details": {
                    "exception_type": "SystemExit",
                    "message": "PRIVATE",
                },
            },
            {
                "code": "ttp.forbidden_group_attribute",
                "path": "/template/group[0]/@PRIVATE_ATTRIBUTE",
            },
            {
                "code": "ttp.invalid_ignore_syntax",
                "details": {
                    "required_action": "replace_with_ignore_call",
                    "expression": "PRIVATE",
                },
            },
        ]
    )
    serialized, feedback = _render(diagnostics)
    assert "PRIVATE" not in serialized
    xml = feedback["issues"][0]
    assert xml["code"] == "ttp.invalid_xml"
    assert xml["details"]["line"] >= 0
    assert xml["details"]["column"] >= 0
    assert xml["details"]["required_action"] == "escape_xml_metacharacters"
    assert feedback["issues"][1]["details"] == {"exception_type": "SystemExit"}
    assert feedback["issues"][2]["path"] == "/template/group[0]/@*"
    assert feedback["issues"][3]["details"] == {
        "required_action": "replace_with_ignore_call",
    }


def test_issue_limit_preserves_input_interleaving_and_reports_omissions() -> None:
    schema = _object({"value": {"type": "string"}}, ["value"])
    diagnostics = [
        {
            "code": "schema.record_mismatch",
            "path": "/value",
            "output_index": index % 5,
            "details": {"keyword": "type"},
        }
        for index in range(100)
    ]
    _, feedback = _render(diagnostics, schema)
    assert len(feedback["issues"]) == 24
    assert [i["input_index"] for i in feedback["issues"]] == [i % 5 for i in range(24)]
    assert feedback["issues_total"] == 100
    assert feedback["issues_omitted"] == 76


def test_byte_limit_keeps_valid_json_and_separate_missing_field_omissions() -> None:
    fields = ["field" + "x" * 110 + str(index) for index in range(60)]
    schema = _object({name: {"type": "string"} for name in fields}, fields)
    diagnostics = [
        {
            "code": "schema.record_mismatch",
            "path": "/",
            "output_index": index,
            "details": {"keyword": "required", "missing_required": fields},
        }
        for index in range(5)
    ]
    serialized, feedback = _render(diagnostics, schema)
    assert len(serialized.encode("utf-8")) <= 8192
    assert 0 < len(feedback["issues"]) < 5
    assert feedback["issues_omitted"] == 5 - len(feedback["issues"])
    assert feedback["retained_candidate_submission_index"] is None
    for issue in feedback["issues"]:
        assert len(issue["details"]["missing_required"]) == 24
        assert issue["details"]["missing_required_omitted"] == 36


def test_long_and_non_schema_paths_are_not_echoed() -> None:
    schema = _object({"value": {"type": "string"}}, ["value"])
    _, feedback = _render(
        [
            {
                "code": "schema.record_mismatch",
                "path": path,
                "details": {"keyword": "required", "missing_required": ["value"]},
            }
            for path in ["/" + "x" * 3000, "/items/0", "/value/secret", "/private"]
        ],
        schema,
    )
    assert all(issue["path"] is None for issue in feedback["issues"])
    assert all(issue["details"] == {} for issue in feedback["issues"])


def test_pipe_compatibility_feedback_projects_only_fixed_repair_facts() -> None:
    serialized, feedback = _render(
        [
            {
                "code": "ttp.incompatible_argument_pipe",
                "path": "/template/group[0]",
                "details": {
                    "required_action": "split_pipe_argument",
                    "line": 3,
                    "column": 18,
                    "argument": "PRIVATE_TEXT",
                    "function": "PRIVATE_NAME",
                },
            }
        ]
    )
    assert "PRIVATE" not in serialized
    assert feedback["issues"][0]["path"] == "/template/group[0]"
    assert feedback["issues"][0]["details"] == {
        "required_action": "split_pipe_argument",
        "line": 3,
        "column": 18,
    }
