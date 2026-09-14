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
    blocks = re.findall(r"```json\n(.*?)\n```", SCHEMA_SYSTEM_PROMPT, re.DOTALL)
    return next(json.loads(b) for b in blocks if "cache_ttl_ms" in b)


def test_displayed_label_example_is_legal_and_keeps_qualifiers():
    schema = example()
    before = deepcopy(schema)
    assert not validate_result_schema(schema)
    assert schema == before
    assert set(schema["properties"]) == {
        "cache_ttl_ms",
        "origin",
        "service_class",
        "ingress_lane_ref",
        "egress_lane_ref",
    }
    assert set(schema["properties"]["origin"]["properties"]) == {"origin_ref"}
    table = re.search(r"Queue Snapshot\n(.*?)\n```", SCHEMA_SYSTEM_PROMPT, re.DOTALL)[
        1
    ].splitlines()
    # Both lower headers share the exact column positions of their own qualifiers.
    assert table[0].index("Ingress") == table[1].index("Lane Ref")
    assert table[0].index("Egress") == table[1].rindex("Lane Ref")
    assert "不能把 Ingress 拼到右列" in SCHEMA_SYSTEM_PROMPT


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
