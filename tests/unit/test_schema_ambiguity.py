"""Independent ambiguity and envelope diagnostics; no evaluation answers."""

import json
import re
from copy import deepcopy

import pytest
from pydantic import ValidationError

from cli_parser_agent.evaluation import schema_pair_metrics
from cli_parser_agent.ttp_generation.agent.prompt import SCHEMA_SYSTEM_PROMPT
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


def test_actual_prompt_envelope_preserves_columns_empty_slots_and_missing_lines():
    blocks = [
        json.loads(s)
        for s in re.findall(r"```json\n(.*?)\n```", SCHEMA_SYSTEM_PROMPT, re.S)
    ]
    envelopes = [s for s in blocks if "result_schema" in s]
    assert len(envelopes) == 1
    envelope = envelopes[0]
    before = deepcopy(envelope)
    schema = SchemaSubmissionInput.model_validate(envelope).result_schema
    assert validate_result_schema(schema) == []
    assert validate_records_against_schema(expected_records(), schema) == []
    assert envelope == before
    assert schema_pair_metrics(schema, synthetic_envelope()["result_schema"])[
        "contract_equal"
    ]
    fields = schema["properties"]["dispatch"]["items"]["properties"]
    assert set(fields) == {"source_ref", "target_ref", "result", "tag"}
    assert "note" not in schema["required"]
    source = re.search(r"示例 D：.*?```text\n(.*?)\n```", SCHEMA_SYSTEM_PROMPT, re.S)[1]
    lines = source.splitlines()
    assert lines[2].index("Source") == lines[3].index("Ref")
    assert lines[2].index("Target") == lines[3].rindex("Ref")
    assert lines[1].index("Auxiliary") not in {
        lines[3].index("Result"),
        lines[3].index("Tag"),
    }
    missing = deepcopy(expected_records())
    del missing[0]["dispatch"][0]["tag"]
    assert validate_records_against_schema(missing, schema)


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
