"""Synthetic value boundaries, independent of evaluation assets and Trace text."""

import json
import re
from copy import deepcopy

from cli_parser_agent.evaluation import schema_pair_metrics
from cli_parser_agent.ttp_generation.agent.prompt import SCHEMA_SYSTEM_PROMPT
from cli_parser_agent.ttp_generation.validation.json_schema import (
    validate_records_against_schema,
    validate_result_schema,
)


def _example():
    blocks = re.findall(r"```json\n(.*?)\n```", SCHEMA_SYSTEM_PROMPT, re.DOTALL)
    return next(json.loads(block) for block in blocks if '"ready_count"' in block)


def _record():
    return {
        "revision": "7.4.2",
        "build": "k91",
        "published": "2032-04-08 16:35:20 UTC",
        "task_name": "cedar+west",
        "task_state": "~queued~",
        "elapsed": "3h:07m:02s",
        "ready_count": 6,
        "waiting_count": 2,
    }


def test_displayed_value_boundaries_support_whole_values_and_absent_component():
    schema = _example()
    first = _record()
    second = {
        **first,
        "revision": "8.0.5",
        "published": "2033-11-09 01:02:03 UTC",
        "task_name": "birch/east-2",
        "task_state": "!held!",
        "elapsed": "0h:00m:04s",
        "ready_count": 0,
        "waiting_count": 9,
    }
    del second["build"]
    before = deepcopy((schema, first, second))
    assert validate_result_schema(schema) == []
    assert validate_records_against_schema([first, second], schema) == []
    assert set(schema["properties"]) == set(first)
    assert (schema, first, second) == before
    assert "build" not in schema["required"]
    assert all(
        schema["properties"][key]["type"] == "string"
        for key in ("revision", "published", "task_name", "task_state", "elapsed")
    )


def test_merged_business_values_can_be_legal_but_lose_independent_contract():
    coarse = {
        "type": "object",
        "additionalProperties": False,
        "properties": {"package": {"type": "string"}},
        "required": ["package"],
    }
    merged = {"package": "revision=7.4.2; build=k91; published=2032-04-08 16:35:20 UTC"}
    assert validate_result_schema(coarse) == []
    assert validate_records_against_schema([merged], coarse) == []
    assert validate_records_against_schema([_record()], coarse)
    assert not schema_pair_metrics(coarse, _example())["contract_equal"]


def test_mechanical_version_split_changes_contract_even_when_both_are_legal():
    original = _example()
    split = deepcopy(original)
    split["properties"]["revision"] = {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            part: {"type": "integer"} for part in ("major", "minor", "patch")
        },
        "required": ["major", "minor", "patch"],
    }
    changed = {**_record(), "revision": {"major": 7, "minor": 4, "patch": 2}}
    assert validate_result_schema(original) == []
    assert validate_result_schema(split) == []
    assert validate_records_against_schema([_record()], original) == []
    assert validate_records_against_schema([changed], split) == []
    assert not schema_pair_metrics(original, split)["contract_equal"]
    assert changed != _record()
