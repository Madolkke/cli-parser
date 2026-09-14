"""Synthetic naming examples; no evaluation labels or Trace content."""

from copy import deepcopy

from cli_parser_agent.evaluation import schema_pair_metrics
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
