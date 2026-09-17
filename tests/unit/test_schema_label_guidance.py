"""Synthetic naming examples; no evaluation labels or Trace content."""

import json
import re
from copy import deepcopy

from cli_parser_agent.evaluation import schema_pair_metrics
from cli_parser_agent.ttp_generation.agent.prompt import SCHEMA_SYSTEM_PROMPT
from cli_parser_agent.ttp_generation.validation.json_schema import (
    validate_result_schema,
)


def example():
    return {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "cache_ttl_ms": {"type": "string"},
            "service_class": {"type": "string"},
            "ingress_lane_ref": {"type": "string"},
            "egress_lane_ref": {"type": "string"},
            "origin": {
                "type": "object",
                "additionalProperties": False,
                "properties": {"origin_ref": {"type": "string"}},
            },
        },
    }


def test_legal_synonym_does_not_imply_equal_contract():
    first = example()
    second = deepcopy(first)
    second["properties"]["cache_time_to_live_ms"] = second["properties"].pop(
        "cache_ttl_ms"
    )
    assert not validate_result_schema(first)
    assert not validate_result_schema(second)
    comparison = schema_pair_metrics(first, second)
    assert comparison["path_difference_count"] == 2
    assert not comparison["contract_equal"]


def test_keyword_exception_is_necessary_not_automatic_renaming():
    schema = example()
    schema["properties"]["class"] = schema["properties"].pop("service_class")
    issues = validate_result_schema(schema)
    assert any(i.code == "schema.python_keyword_property_name" for i in issues)
    assert "class" in schema["properties"]


def test_actual_label_example_preserves_abbreviations_and_column_ownership():
    schema = json.loads(
        re.findall(r"```json\n(.*?)\n```", SCHEMA_SYSTEM_PROMPT, re.S)[2]
    )
    fields = schema["properties"]
    assert "cache_ttl_ms" in fields
    assert "cache_time_to_live_ms" not in fields
    assert set(fields["origin"]["properties"]) == {"origin_ref"}
    assert set(fields["service"]["properties"]) == {"service_class"}
    assert set(fields["transfer"]["items"]["properties"]) == {"local_ref", "remote_ref"}
    assert "worker_name" in fields and "worker_status" in fields
    assert validate_result_schema(schema) == []


def test_valid_synonym_and_partial_qualifier_are_contract_differences():
    original = json.loads(
        re.findall(r"```json\n(.*?)\n```", SCHEMA_SYSTEM_PROMPT, re.S)[2]
    )
    changed = deepcopy(original)
    fields = changed["properties"]
    fields["cache_time_to_live_ms"] = fields.pop("cache_ttl_ms")
    changed["required"][0] = "cache_time_to_live_ms"
    child = fields["origin"]
    child["properties"]["ref"] = child["properties"].pop("origin_ref")
    child["required"] = ["ref"]
    assert validate_result_schema(changed) == []
    pair = schema_pair_metrics(original, changed)
    assert not pair["contract_equal"]
    assert pair["path_difference_count"] == 4


def test_naming_policy_follows_business_boundaries_then_structure():
    positions = [
        SCHEMA_SYSTEM_PROMPT.index(text)
        for text in (
            "识别独立业务值",
            "确定实体与归属",
            "object／array",
            "确定名称",
            "检查完整性并提交",
        )
    ]
    assert positions == sorted(positions)
    for instruction in (
        "不主动做单复数转换",
        "不能因父容器",
        "不能带入",
        "无可靠标签、拆分子项、名称非法",
        "不强制各自套用整条标签",
    ):
        assert instruction in SCHEMA_SYSTEM_PROMPT
