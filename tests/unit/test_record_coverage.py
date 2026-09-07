from __future__ import annotations

import json

import pytest

from cli_parser_agent.ttp_generation.agent import feedback as feedback_module
from cli_parser_agent.ttp_generation.agent.feedback import submission_feedback
from cli_parser_agent.ttp_generation.agent.session import GenerationSession
from cli_parser_agent.ttp_generation.agent.tools import (
    SubmitTtpTemplateTool,
    ValidatorOutcome,
)
from cli_parser_agent.ttp_generation.validation.json_schema import (
    validate_result_schema,
)


def obj(properties, required=()):
    return {"type": "object", "properties": properties, "required": list(required)}


def render(schema, records, input_count=1, issues=()):
    session = GenerationSession(
        command_outputs=["synthetic"] * input_count,
        schema_validator=lambda _: None,
        template_validator=lambda _: None,
    )
    session.frozen_schema = schema
    block = submission_feedback(
        session,
        accepted=False,
        candidate_updated=False,
        returned_record_count=len(records) if records is not None else 0,
        issues=issues,
        records=records,
    )
    serialized = block.split("\n")[1]
    return json.loads(serialized)["record_coverage"], serialized


def test_no_parse_has_no_coverage():
    assert render(obj({}), None)[0] is None


def test_presence_does_not_claim_schema_valid_or_source_presence():
    result, serialized = render(
        obj({"name": {"type": "string"}, "note": {"type": "string"}}, ["name"]),
        [{"name": 42, "extra_private_key": "secret_value"}],
    )
    assert result["required_paths_complete"] is True
    assert result["optional_paths_absent"] == [
        {
            "input_index": 0,
            "path": "/note",
            "parent_occurrences": 1,
            "present_occurrences": 0,
        }
    ]
    assert (
        "42" not in serialized
        and "secret" not in serialized
        and "private" not in serialized
    )


def test_missing_optional_parent_and_empty_array_do_not_imply_child_missing():
    schema = obj(
        {
            "details": obj({"name": {"type": "string"}}, ["name"]),
            "items": {
                "type": "array",
                "items": obj(
                    {"note": {"type": "string"}, "name": {"type": "string"}}, ["name"]
                ),
            },
        }
    )
    result, _ = render(schema, [{"items": []}])
    assert result["required_paths_complete"] is True
    assert [entry["path"] for entry in result["optional_paths_absent"]] == ["/details"]


def test_nested_required_fields_checked_only_for_existing_object_instances():
    schema = obj({"details": obj({"name": {"type": "string"}}, ["name"])})
    assert render(schema, [{}])[0]["required_paths_complete"] is True
    assert render(schema, [{"details": {}}])[0]["required_paths_complete"] is False
    assert (
        render(schema, [{"details": "wrong_type"}])[0]["required_paths_complete"]
        is True
    )


def test_partial_array_presence_uses_wildcards_and_actual_parent_counts():
    schema = obj(
        {
            "items": {
                "type": "array",
                "items": obj(
                    {
                        "note": {"type": "string"},
                        "detail": obj({"label": {"type": "string"}}),
                    }
                ),
            }
        }
    )
    result, _ = render(
        schema,
        [
            {
                "items": [
                    {"note": None, "detail": {}},
                    {},
                    {"note": "v", "detail": {"label": "l"}},
                    "invalid",
                ]
            }
        ],
    )
    assert result["optional_paths_absent"] == []
    assert result["optional_paths_partial"] == [
        {
            "input_index": 0,
            "path": "/items/*/note",
            "parent_occurrences": 3,
            "present_occurrences": 2,
        },
        {
            "input_index": 0,
            "path": "/items/*/detail",
            "parent_occurrences": 3,
            "present_occurrences": 2,
        },
        {
            "input_index": 0,
            "path": "/items/*/detail/label",
            "parent_occurrences": 2,
            "present_occurrences": 1,
        },
    ]


def test_invalid_root_mapping_cannot_claim_required_complete():
    schema = obj({})
    for records in ([], [{}], [{}, {}], [None, {}]):
        assert (
            render(schema, records, input_count=3)[0]["required_paths_complete"]
            is False
        )


def test_multiple_inputs_are_interleaved_before_global_limit():
    schema = obj({f"field_{i}": {"type": "string"} for i in range(30)})
    result, _ = render(schema, [{}, {}], input_count=2)
    assert len(result["optional_paths_absent"]) == 24
    assert result["optional_paths_total"] == 60
    assert result["optional_paths_omitted"] == 36
    assert [entry["input_index"] for entry in result["optional_paths_absent"]] == [
        0,
        1,
    ] * 12


def test_byte_limit_preserves_whole_facts_and_omission_counts(monkeypatch):
    monkeypatch.setattr(feedback_module, "MAX_FEEDBACK_BYTES", 1500)
    schema = obj({"x" * 110 + str(i): {"type": "string"} for i in range(60)})
    result, serialized = render(schema, [{}])
    assert len(serialized.encode("utf-8")) <= 1500
    assert 0 < len(result["optional_paths_absent"]) < 24
    assert result["optional_paths_omitted"] == 60 - len(result["optional_paths_absent"])


def test_non_schema_names_are_never_exported():
    schema = obj(
        {
            "safe": {"type": "string"},
            "PRIVATE": {"type": "string"},
            "x" * 121: {"type": "string"},
            "bad/path": {"type": "string"},
        }
    )
    result, serialized = render(schema, [{}])
    assert result is None
    assert "PRIVATE" not in serialized and "bad/path" not in serialized


def test_required_names_outside_properties_still_need_to_be_present():
    schema = obj({"label": {"type": "string"}}, ["absent"])
    assert render(schema, [{}])[0]["required_paths_complete"] is False
    assert render(schema, [{"absent": 3}])[0]["required_paths_complete"] is True


@pytest.mark.parametrize("required_name", ["UNDECLARED", "private/path", "x" * 121])
def test_frozen_schema_undeclared_required_names_are_checked_but_never_projected(
    required_name,
):
    schema = {
        **obj({"note": {"type": "string"}}, [required_name]),
        "additionalProperties": False,
    }
    assert validate_result_schema(schema) == []
    for record, complete in [({}, False), ({required_name: "private_value"}, True)]:
        coverage, serialized = render(schema, [record])
        assert coverage["required_paths_complete"] is complete
        assert [item["path"] for item in coverage["optional_paths_absent"]] == ["/note"]
        assert required_name not in serialized
        assert "private_value" not in serialized


@pytest.mark.parametrize("mode", ["cycle", "depth", "properties", "invalid_type"])
def test_invalid_schema_cannot_expand_presence_recursively(mode):
    schema = obj({})
    if mode == "cycle":
        schema["properties"]["child"] = schema
    elif mode == "depth":
        for _ in range(25):
            schema = obj({"child": schema})
    elif mode == "properties":
        schema = obj({f"field{i}": {"type": "string"} for i in range(257)})
    else:
        schema["type"] = {"untrusted": "private"}
    result, serialized = render(schema, [{}])
    assert result is None
    assert "private" not in serialized


def test_issues_take_priority_within_the_shared_byte_budget():
    fields = ["x" * 110 + str(i) for i in range(60)]
    schema = obj({name: {"type": "string"} for name in fields}, fields[:30])
    diagnostics = [
        {
            "code": "schema.record_mismatch",
            "path": "/",
            "output_index": index,
            "details": {"keyword": "required", "missing_required": fields[:30]},
        }
        for index in range(5)
    ]
    coverage, serialized = render(schema, [{}] * 5, input_count=5, issues=diagnostics)
    payload = json.loads(serialized)
    assert len(serialized.encode("utf-8")) <= 8192
    assert 0 < len(payload["issues"]) < 5
    assert payload["issues_omitted"] == 5 - len(payload["issues"])
    assert coverage["required_paths_complete"] is False
    assert coverage["optional_paths_total"] == 150
    selected = len(coverage["optional_paths_absent"])
    assert coverage["optional_paths_omitted"] == 150 - selected
    assert selected < 24


def test_global_limit_combines_absent_and_partial_optional_paths():
    fields = {f"field{i}": {"type": "string"} for i in range(20)}
    schema = obj({"items": {"type": "array", "items": obj(fields)}})
    record = {"items": [{f"field{i}": 1 for i in range(10)}, {}]}
    coverage, _ = render(schema, [record, record], input_count=2)
    assert (
        len(coverage["optional_paths_absent"]) + len(coverage["optional_paths_partial"])
        == 24
    )
    assert coverage["optional_paths_total"] == 40
    assert coverage["optional_paths_omitted"] == 16


def _chunk_feedback(chunk):
    return json.loads(chunk.content[0].text.split("\n")[1])


@pytest.mark.parametrize(
    ("issues", "expected"),
    [
        ([{"code": "ttp.worker_error"}], None),
        ([{"code": "ttp.incompatible_argument_pipe"}], None),
        ([{"code": "ttp.invalid_record"}], None),
        ([], False),
    ],
)
async def test_unavailable_parse_and_empty_returned_mapping_are_distinct(
    issues, expected
):
    session = GenerationSession(
        command_outputs=["synthetic"],
        schema_validator=lambda _: None,
        template_validator=lambda _: ValidatorOutcome(
            valid=not issues, records=(), issues=tuple(issues)
        ),
    )
    session.frozen_schema = obj({"optional": {"type": "string"}})
    chunk = await SubmitTtpTemplateTool(session).call("{{ optional }}")
    coverage = _chunk_feedback(chunk)["record_coverage"]
    if expected is None:
        assert coverage is None
    else:
        assert coverage["required_paths_complete"] is expected


async def test_failure_feedback_uses_current_records_and_preserves_candidate():
    outcomes = iter(
        [
            ValidatorOutcome(valid=True, records=({"name": "ok", "note": "kept"},)),
            ValidatorOutcome(
                valid=False,
                records=({},),
                issues=({"code": "schema.record_mismatch"},),
            ),
        ]
    )
    session = GenerationSession(
        command_outputs=["synthetic"],
        schema_validator=lambda _: None,
        template_validator=lambda _: next(outcomes),
    )
    session.frozen_schema = obj(
        {"name": {"type": "string"}, "note": {"type": "string"}}, ["name"]
    )
    tool = SubmitTtpTemplateTool(session)
    first = _chunk_feedback(await tool.call("{{ name }} {{ note }}"))
    failed = _chunk_feedback(await tool.call("{{ name }}"))
    duplicate = _chunk_feedback(await tool.call("{{ name }}"))
    assert first["record_coverage"]["optional_paths_absent"] == []
    assert failed["record_coverage"]["required_paths_complete"] is False
    assert failed["record_coverage"]["optional_paths_absent"][0]["path"] == "/note"
    assert failed["retained_candidate_submission_index"] == 1
    assert duplicate["record_coverage"] is None
    assert duplicate["retained_candidate_submission_index"] == 1
    assert session.records == ({"name": "ok", "note": "kept"},)
