"""Independent synthetic contracts; no evaluation or trace-derived material."""

import json
import re
from copy import deepcopy

import pytest

from cli_parser_agent.evaluation import schema_pair_metrics
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


def _prompt_examples():
    blocks = re.findall(r"```json\n(.*?)\n```", SCHEMA_SYSTEM_PROMPT, re.DOTALL)
    assert len(blocks) == 4
    schemas = [json.loads(block) for block in blocks]
    return [schema for schema in schemas if "result_schema" not in schema]


def _queue_records():
    return [
        {
            "survey": "depot",
            "queue": [
                {
                    "queue": "amber",
                    "note": "ready",
                    "check": [
                        {"check_id": "latch", "result": "shut"},
                        {"check_id": "lamp", "result": "~pending~"},
                    ],
                    "queue_total": 2,
                },
                {"queue": "birch", "note": "", "queue_total": 0},
                {
                    "queue": "cedar",
                    "check": [{"check_id": "relay", "result": "idle"}],
                    "queue_total": 1,
                },
            ],
            "total": 3,
        },
        {
            "survey": "annex",
            "queue": [{"queue": "elm", "queue_total": 0}],
            "total": 1,
        },
    ]


def test_actual_prompt_examples_pass_common_validator_without_mutation():
    examples = _prompt_examples()
    before = deepcopy(examples)
    for schema in examples:
        assert validate_result_schema(schema) == []
    assert examples == before


def test_prompt_root_entities_optional_sections_and_summary_match_records():
    schema = _prompt_examples()[0]
    records = _queue_records()
    before = deepcopy((schema, records))
    assert validate_records_against_schema(records, schema) == []
    assert (schema, records) == before
    assert schema["properties"]["queue"]["type"] == "array"
    entity = schema["properties"]["queue"]["items"]
    assert set(entity["required"]) == {"queue", "queue_total"}
    assert "name" not in entity["properties"]
    assert "check" not in entity["required"]
    assert entity["properties"]["check"]["type"] == "array"
    assert set(entity["properties"]["check"]["items"]["required"]) == {
        "check_id",
        "result",
    }
    assert len(records) == 2
    assert [len(record["queue"]) for record in records] == [3, 1]
    assert [item["queue"] for item in records[0]["queue"]] == [
        "amber",
        "birch",
        "cedar",
    ]
    assert records[0]["queue"][1]["note"] == ""
    assert "note" not in records[0]["queue"][2]
    assert "check" not in records[0]["queue"][1]
    assert "queue_total" not in entity["properties"]["check"]["items"]["properties"]


def test_prompt_fixed_roles_are_objects_and_do_not_copy_parent_prefix():
    schema = _prompt_examples()[1]
    expected = {
        "primary_counters": {"requests": 3},
        "backup_counters": {"requests": 5},
    }
    assert validate_records_against_schema([expected], schema) == []
    for role in expected:
        assert schema["properties"][role]["type"] == "object"
        assert set(schema["properties"][role]["properties"]) == {"requests"}


def test_prompt_naming_example_preserves_labels_and_separates_business_values():
    schema = _prompt_examples()[2]
    expected = {
        "cache_ttl_ms": "30 ms",
        "origin": {"origin_ref": "rack-8"},
        "service": {"service_class": "batch"},
        "transfer": [{"local_ref": "bay-2", "remote_ref": "bay-3"}],
        "worker_name": "cedar+east",
        "worker_status": "active",
    }
    assert validate_records_against_schema([expected], schema) == []
    changed = deepcopy(expected)
    changed["worker_name"] = "cedar+east (active)"
    del changed["worker_status"]
    assert validate_records_against_schema([changed], schema)


def test_legal_single_entity_shape_still_violates_whole_output_policy():
    full_schema = _prompt_examples()[0]
    wrong_schema = deepcopy(full_schema)
    wrong_schema["properties"]["queue"] = full_schema["properties"]["queue"]["items"]
    assert validate_result_schema(wrong_schema) == []
    expected = _queue_records()[1]
    wrong_record = deepcopy(expected)
    wrong_record["queue"] = wrong_record["queue"][0]
    assert validate_records_against_schema([wrong_record], wrong_schema) == []
    assert validate_records_against_schema([expected], wrong_schema)
    pair = schema_pair_metrics(full_schema, wrong_schema)
    assert not pair["contract_equal"]
    assert not pair["structure_equal"]


def test_valid_roles_array_does_not_prove_fixed_role_structure_is_followed():
    original = _prompt_examples()[1]
    alternate = _object(
        {
            "counters": {
                "type": "array",
                "items": _object(
                    {"role": {"type": "string"}, "requests": {"type": "integer"}},
                    ["role", "requests"],
                ),
            }
        },
        ["counters"],
    )
    assert validate_result_schema(alternate) == []
    assert (
        validate_records_against_schema(
            [
                {
                    "counters": [
                        {"role": "primary", "requests": 3},
                        {"role": "backup", "requests": 5},
                    ]
                }
            ],
            alternate,
        )
        == []
    )
    assert not schema_pair_metrics(original, alternate)["contract_equal"]
