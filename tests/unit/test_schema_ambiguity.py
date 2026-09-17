"""Independent ambiguity and envelope diagnostics; no evaluation answers."""

from copy import deepcopy

import pytest
from pydantic import ValidationError

from cli_parser_agent.evaluation import schema_pair_metrics
from cli_parser_agent.ttp_generation.agent.tools import SchemaSubmissionInput
from cli_parser_agent.ttp_generation.validation.json_schema import (
    validate_records_against_schema,
    validate_result_schema,
)


def synthetic_envelope():
    fields = {
        name: {"type": "string"}
        for name in ("source_ref", "target_ref", "result", "tag")
    }
    return {
        "result_schema": {
            "$schema": "https://json-schema.org/draft/2020-12/schema",
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "dispatch": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "additionalProperties": False,
                        "properties": fields,
                        "required": list(fields),
                    },
                },
                "note": {"type": "string"},
            },
            "required": ["dispatch"],
        }
    }


def expected_records():
    return [
        {
            "dispatch": [
                {
                    "source_ref": "oak+1",
                    "target_ref": "ash-2",
                    "result": "~queued~",
                    "tag": "",
                },
                {
                    "source_ref": "elm-3",
                    "target_ref": "fir+4",
                    "result": "ready",
                    "tag": "",
                },
            ],
            "note": "scheduled",
        },
        {
            "dispatch": [
                {
                    "source_ref": "pine-5",
                    "target_ref": "yew+6",
                    "result": "~held~",
                    "tag": "",
                }
            ]
        },
    ]


def test_root_required_at_tool_top_level_is_rejected_without_mutation():
    wrong = synthetic_envelope()
    wrong["required"] = wrong["result_schema"].pop("required")
    before = deepcopy(wrong)
    with pytest.raises(ValidationError):
        SchemaSubmissionInput.model_validate(wrong)
    assert wrong == before
    assert validate_result_schema(wrong["result_schema"]) == []


def test_wrong_qualifier_can_pass_schema_gate_but_changes_business_contract():
    good = synthetic_envelope()["result_schema"]
    wrong = deepcopy(good)
    item = wrong["properties"]["dispatch"]["items"]
    item["properties"]["auxiliary_result"] = item["properties"].pop("result")
    item["required"] = [
        "auxiliary_result" if key == "result" else key for key in item["required"]
    ]
    assert validate_result_schema(wrong) == []
    assert validate_records_against_schema(expected_records(), wrong)
    altered = deepcopy(expected_records())
    for record in altered:
        for row in record["dispatch"]:
            row["auxiliary_result"] = row.pop("result")
    assert validate_records_against_schema(altered, wrong) == []
    assert not schema_pair_metrics(good, wrong)["contract_equal"]


def test_string_gate_does_not_prove_placeholder_fidelity():
    schema = synthetic_envelope()["result_schema"]
    original = expected_records()
    cleaned = deepcopy(original)
    cleaned[0]["dispatch"][0]["result"] = "queued"
    assert validate_records_against_schema(original, schema) == []
    assert validate_records_against_schema(cleaned, schema) == []
    assert original != cleaned
