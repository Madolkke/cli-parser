"""Independent synthetic contracts; no evaluation or trace-derived material."""

import json
import re
from copy import deepcopy

import pytest

from cli_parser_agent.ttp_generation.agent.prompt import SCHEMA_SYSTEM_PROMPT
from cli_parser_agent.ttp_generation.validation.json_schema import (
    validate_records_against_schema,
    validate_result_schema,
)


def _object(properties, required):
    return {
        "type": "object",
        "additionalProperties": False,
        "required": required,
        "properties": properties,
    }


def _contract():
    check = _object(
        {"name": {"type": "string"}, "state": {"type": "string"}},
        ["name", "state"],
    )
    asset = _object(
        {
            "name": {"type": "string"},
            "result": {
                "type": "string",
                "description": "保留原字符串及值内符号；空槽保留空字符串，缺行时省略。",
            },
            "checks": {"type": "array", "items": check},
        },
        ["name"],
    )
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        **_object(
            {
                "report_name": {"type": "string"},
                "assets": {"type": "array", "items": asset},
                "total": {"type": "integer"},
            },
            ["report_name", "assets", "total"],
        ),
    }


def _expected():
    return {
        "report_name": "workshop",
        "assets": [
            {
                "name": "cedar",
                "result": "ready",
                "checks": [
                    {"name": "lamp", "state": "lit"},
                    {"name": "latch", "state": "shut"},
                ],
            },
            {"name": "birch", "result": "~pending~"},
            {"name": "elm", "result": ""},
            {"name": "ash", "checks": [{"name": "relay", "state": "idle"}]},
        ],
        "total": 4,
    }


def test_schema_prompt_example_matches_independent_contract_and_input():
    blocks = re.findall(r"```json\n(.*?)\n```", SCHEMA_SYSTEM_PROMPT, re.DOTALL)
    assert len(blocks) == 1
    shown = json.loads(blocks[0])
    assert shown == _contract()
    assert validate_result_schema(shown) == []
    assert validate_records_against_schema([_expected()], shown) == []
    inputs = re.findall(r"```text\n(.*?)\n```", SCHEMA_SYSTEM_PROMPT, re.DOTALL)
    assert inputs == [
        "Report: workshop\nAsset: cedar\nResult: ready,\n"
        "Check: lamp, State: lit\nCheck: latch, State: shut\n"
        "Asset: birch\nResult: ~pending~,\nAsset: elm\nResult: ,\n"
        "Asset: ash\nCheck: relay, State: idle\nTotal: 4"
    ]


def test_whole_output_contract_supports_different_entity_counts_without_mutation():
    schema = _contract()
    records = [
        _expected(),
        {"report_name": "annex", "assets": [{"name": "oak"}], "total": 1},
        {"report_name": "closed", "assets": [], "total": 0},
    ]
    before = deepcopy((schema, records))
    assert validate_records_against_schema(records, schema) == []
    assert (schema, records) == before


def test_single_entity_root_is_legal_but_cannot_describe_whole_output():
    entity_schema = _contract()["properties"]["assets"]["items"]
    assert validate_result_schema(entity_schema) == []
    assert (
        validate_records_against_schema([_expected()["assets"][0]], entity_schema) == []
    )
    issues = validate_records_against_schema([_expected()], entity_schema)
    assert {issue.details["keyword"] for issue in issues} == {
        "required",
        "additionalProperties",
    }
    assert all(issue.path == "/" and issue.output_index == 0 for issue in issues)
    assert any(issue.details.get("missing_required") == ["name"] for issue in issues)


@pytest.mark.parametrize("change", ["empty", "omit", "strip_symbols"])
def test_schema_validation_cannot_detect_placeholder_rewriting(change):
    expected = _expected()
    changed = deepcopy(expected)
    entity = changed["assets"][1]
    if change == "empty":
        entity["result"] = ""
    elif change == "omit":
        del entity["result"]
    else:
        entity["result"] = "pending"
    assert validate_records_against_schema([expected], _contract()) == []
    assert validate_records_against_schema([changed], _contract()) == []
    assert changed != expected
    assert changed["assets"][:1] == expected["assets"][:1]
    assert changed["assets"][2:] == expected["assets"][2:]


def test_flat_device_contract_does_not_require_array_wrapper():
    schema = _object({"name": {"type": "string"}}, ["name"])
    assert validate_result_schema(schema) == []
    assert validate_records_against_schema([{"name": "standalone"}], schema) == []
