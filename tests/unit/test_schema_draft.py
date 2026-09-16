"""Compiler checks structure and source validity, not business correctness."""

from copy import deepcopy
from dataclasses import replace

import pytest

from cli_parser_agent.evaluation import schema_pair_metrics
from cli_parser_agent.ttp_generation.schema_draft import compile_schema_draft
from cli_parser_agent.ttp_generation.schema_draft_sources import prepare_draft_sources
from cli_parser_agent.ttp_generation.validation.json_schema import (
    schema_draft_attribute_keywords,
    validate_records_against_schema,
    validate_result_schema,
)


def _source(text="Name: Cedar\nResult: !pending\nCount: 3\n"):
    return prepare_draft_sources([text], originals=[text])


def _ref(quote, line="i0f0l1"):
    return {"line_id": line, "quote": quote}


def _field(name="Name", node=None, *, required=True):
    return {
        "name": {"source": [_ref(name)]},
        "required": required,
        "node": node or {"type": "string"},
    }


def _fallback(name, node=None, reason="unlabeled", source=None, required=True):
    result = _field(node=node, required=required)
    result["name"] = {"fallback": name, "reason": reason}
    if source is not None:
        result["name"]["source"] = source
    return result


def _draft(*fields, attributes=None):
    result = {"version": 1, "fields": list(fields)}
    if attributes is not None:
        result["attributes"] = attributes
    return result


def _code(result):
    assert not result.accepted and result.schema is None
    return result.issues[0].code


def test_simple_compilation_exact_deterministic_and_input_unchanged():
    raw = _draft(_field(), attributes={"description": "One output"})
    before = deepcopy(raw)
    first = compile_schema_draft(raw, _source())
    second = compile_schema_draft(raw, _source())
    assert first.accepted and first.schema == second.schema
    assert raw == before
    assert first.schema == {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "type": "object",
        "description": "One output",
        "additionalProperties": False,
        "properties": {"name": {"type": "string"}},
        "required": ["name"],
    }
    assert first.facts["source_name_count"] == first.facts["reference_count"] == 1
    assert "Cedar" not in repr(first)


def test_arrays_scalar_nested_object_and_required_flags_remain_at_parent():
    node = {
        "type": "object",
        "fields": [
            _fallback("name", required=False),
            _fallback(
                "values",
                {
                    "type": "array",
                    "items": {
                        "type": "array",
                        "items": {"type": "integer", "attributes": {"minimum": 0}},
                    },
                },
            ),
        ],
    }
    result = compile_schema_draft(
        _draft(_fallback("assets", {"type": "array", "items": node})), _source()
    )
    assert result.accepted
    assert result.schema["required"] == ["assets"]
    item = result.schema["properties"]["assets"]["items"]
    assert item["required"] == ["values"]
    assert item["additionalProperties"] is False
    records = [{"assets": [{"values": [[1, 2], [3]]}]}]
    assert validate_records_against_schema(records, result.schema) == []
    assert result.facts["property_count"] == 3


@pytest.mark.parametrize(
    ("node_type", "attributes"),
    [
        (
            "string",
            {
                "title": "Label",
                "description": "Keep !pending",
                "enum": ["x", "!pending"],
                "minLength": 1,
                "maxLength": 30,
            },
        ),
        (
            "integer",
            {
                "enum": [0, 1],
                "minimum": 0,
                "maximum": 8,
                "exclusiveMinimum": -1,
                "exclusiveMaximum": 9,
                "multipleOf": 1,
            },
        ),
        (
            "number",
            {
                "enum": [1.5, 2.0],
                "minimum": 0.2,
                "maximum": 4,
                "exclusiveMinimum": 0,
                "exclusiveMaximum": 5,
                "multipleOf": 0.5,
            },
        ),
        ("boolean", {"enum": [True, False]}),
        ("array", {"title": "Values", "minItems": 0, "maxItems": 5}),
        ("object", {"description": "Container"}),
    ],
)
def test_model_types_and_all_supported_attributes_preserved(node_type, attributes):
    node = {"type": node_type, "attributes": attributes}
    if node_type == "array":
        node["items"] = {"type": "string"}
    if node_type == "object":
        node["fields"] = [_fallback("child")]
    result = compile_schema_draft(_draft(_field(node=node)), _source())
    assert result.accepted
    compiled = result.schema["properties"]["name"]
    assert compiled["type"] == node_type
    assert {key: compiled[key] for key in attributes} == attributes


def test_invalid_attribute_values_reach_shared_validator_without_echo():
    result = compile_schema_draft(
        _draft(_field(node={"type": "integer", "attributes": {"enum": [True]}})),
        _source(),
    )
    assert _code(result) == "schema.invalid_enum"
    assert result.issues[0].path == "/fields/0/node/attributes/enum"
    assert "Name" not in result.issues[0].model_dump_json()


@pytest.mark.parametrize(
    "attribute",
    [
        "type",
        "properties",
        "required",
        "items",
        "additionalProperties",
        "$schema",
        "pattern",
        "format",
        "arbitrary_secret",
    ],
)
def test_structural_or_unsupported_attributes_rejected_without_echo(attribute):
    result = compile_schema_draft(
        _draft(_field(node={"type": "string", "attributes": {attribute: "secret"}})),
        _source(),
    )
    assert _code(result) == "schema_draft.invalid_attribute"
    assert "secret" not in result.issues[0].model_dump_json()


def test_keyword_helper_is_read_only_and_matches_supported_scalar_constraints():
    assert schema_draft_attribute_keywords("boolean") == frozenset(
        {"title", "description", "enum"}
    )
    assert "items" not in schema_draft_attribute_keywords("array")
    assert schema_draft_attribute_keywords("unknown") == frozenset()


@pytest.mark.parametrize(
    ("label", "expected"),
    [
        ("CLI-ID:", "cli_id"),
        ("  Port..RX Count --", "port_rx_count"),
        ("OS Rev2", "os_rev2"),
    ],
)
def test_label_normalization_preserves_order_abbreviation_and_numbers(label, expected):
    result = compile_schema_draft(_draft(_field(label)), _source(label + " value\n"))
    assert result.accepted and list(result.schema["properties"]) == [expected]


def test_multiline_parts_are_ordered_and_parent_does_not_remove_qualifier():
    text = "Port RX     System\nError Count Rate\n"
    name = {"source": [_ref("Port RX"), _ref("Error Count", "i0f0l2")]}
    field = _field()
    field["name"] = name
    result = compile_schema_draft(
        _draft(_fallback("port", {"type": "object", "fields": [field]})), _source(text)
    )
    assert result.accepted
    assert "port_rx_error_count" in result.schema["properties"]["port"]["properties"]


def test_valid_references_do_not_prove_multiline_column_ownership():
    text = "Port RX     System\nError Count Rate\n"
    field = _field()
    field["name"] = {"source": [_ref("System"), _ref("Error Count", "i0f0l2")]}
    result = compile_schema_draft(_draft(field), _source(text))
    assert result.accepted
    assert list(result.schema["properties"]) == ["system_error_count"]
    # A valid reference to a neighboring header needs semantic review.
    assert "port_rx_error_count" not in result.schema["properties"]


@pytest.mark.parametrize("quote", ["Informatio", "nformation", "Last Reboot Reaso"])
def test_ascii_partial_word_quotes_rejected(quote):
    text = "Information Last Reboot Reason\n"
    result = compile_schema_draft(_draft(_field(quote)), _source(text))
    assert _code(result) == "schema_draft.partial_word"


def test_omitting_a_whole_label_word_cannot_be_rejected_by_source_validation():
    result = compile_schema_draft(
        _draft(_field("Reboot Reason")), _source("Last Reboot Reason: normal\n")
    )
    assert result.accepted and list(result.schema["properties"]) == ["reboot_reason"]
    assert "last_reboot_reason" not in result.schema["properties"]


@pytest.mark.parametrize(
    ("text", "quote", "code"),
    [
        ("Name Name\n", "Name", "ambiguous_reference"),
        ("Name\n", "Missing", "invalid_reference"),
        ("Name\n", "", "invalid_reference"),
    ],
)
def test_quote_requires_unique_nonempty_exact_match(text, quote, code):
    assert (
        _code(compile_schema_draft(_draft(_field(quote)), _source(text)))
        == "schema_draft." + code
    )


def test_repeated_labels_use_one_reference_and_are_legal_in_different_parents():
    field = _field()
    raw = _draft(
        _fallback("left", {"type": "object", "fields": [field]}),
        _fallback("right", {"type": "object", "fields": [deepcopy(field)]}),
    )
    result = compile_schema_draft(raw, _source("Name: Cedar\nName: Birch\n"))
    assert result.accepted and result.facts["reference_count"] == 2
    assert "name" in result.schema["properties"]["left"]["properties"]
    assert "name" in result.schema["properties"]["right"]["properties"]


@pytest.mark.parametrize(
    "sources",
    [
        [replace(_source()[0], complete=False)],
        [],
        [replace(_source()[0], line_id="wrong")],
        [_source()[0], _source()[0]],
    ],
)
def test_incomplete_absent_and_invalid_sources_rejected(sources):
    assert not compile_schema_draft(_draft(_field()), sources).accepted


@pytest.mark.parametrize(
    "refs",
    [
        [_ref("Result", "i0f0l2"), _ref("Name")],
        [_ref("Name"), _ref("Name")],
        [_ref("Name"), _ref("Result", "i0f1l1")],
        [_ref("Name"), _ref("Result", "i1f0l1")],
    ],
)
def test_label_parts_cannot_reverse_overlap_or_cross_fragment_or_input(refs):
    sources = list(_source()) + [
        replace(_source()[1], line_id="i0f1l1", fragment_index=1, line_index=1),
        replace(_source()[1], line_id="i1f0l1", input_index=1, line_index=1),
    ]
    field = _field()
    field["name"] = {"source": refs}
    assert (
        _code(compile_schema_draft(_draft(field), sources))
        == "schema_draft.source_order"
    )


@pytest.mark.parametrize("label", ["class", "ignore", "123 Name", "名称", "X" * 121])
def test_invalid_label_requires_verified_legal_fallback(label):
    sources = _source(label + ": value\n")
    assert (
        _code(compile_schema_draft(_draft(_field(label)), sources))
        == "schema_draft.invalid_name"
    )
    result = compile_schema_draft(
        _draft(_fallback("device_value", reason="invalid_name", source=[_ref(label)])),
        sources,
    )
    assert result.accepted
    assert result.facts["fallback_invalid_name_count"] == 1


@pytest.mark.parametrize(
    "name", ["class", "name_", "bad__name", "Name", "ignore", "x" * 121]
)
def test_fallback_must_obey_existing_name_gate(name):
    assert (
        _code(compile_schema_draft(_draft(_fallback(name)), _source()))
        == "schema_draft.invalid_name"
    )


def test_scalar_ignore_vs_container_ignore_and_soft_keywords():
    raw = _draft(
        _fallback("ignore", {"type": "array", "items": {"type": "string"}}),
        *[_fallback(name) for name in ["match", "case", "type", "id"]],
    )
    assert compile_schema_draft(raw, []).accepted


@pytest.mark.parametrize("reason", ["invalid_name", "conflict"])
def test_verifiable_fallback_reasons_cannot_replace_a_valid_unique_label(reason):
    raw = _draft(_fallback("device_name", reason=reason, source=[_ref("Name")]))
    assert (
        _code(compile_schema_draft(raw, _source()))
        == "schema_draft.unnecessary_fallback"
    )


def test_collision_requires_explicit_resolution_and_never_overwrites():
    sources = _source("CLI-ID: first\nCLI ID: second\n")
    first, second = _field("CLI-ID"), _field("CLI ID")
    second["name"]["source"][0]["line_id"] = "i0f0l2"
    assert (
        _code(compile_schema_draft(_draft(first, second), sources))
        == "schema_draft.name_collision"
    )
    second["name"] = {
        "fallback": "secondary_cli_id",
        "reason": "conflict",
        "source": [_ref("CLI ID", "i0f0l2")],
    }
    result = compile_schema_draft(_draft(first, second), sources)
    assert result.accepted
    assert list(result.schema["properties"]) == ["cli_id", "secondary_cli_id"]
    assert result.schema["required"] == ["cli_id", "secondary_cli_id"]
    assert result.facts["fallback_conflict_count"] == 1


@pytest.mark.parametrize("reason", ["unlabeled", "ambiguous_source", "split_component"])
def test_semantic_fallback_reasons_remain_model_judgments(reason):
    result = compile_schema_draft(
        _draft(_fallback("business_value", reason=reason)), []
    )
    assert result.accepted and result.facts[f"fallback_{reason}_count"] == 1


def test_required_must_be_bool_and_only_property_nodes_have_names():
    raw = _draft(_field())
    raw["fields"][0]["required"] = 1
    assert _code(compile_schema_draft(raw, _source())) == "schema_draft.invalid_shape"
    raw = _draft(
        _fallback(
            "items", {"type": "array", "items": {"type": "string", "required": True}}
        )
    )
    assert _code(compile_schema_draft(raw, [])) == "schema_draft.invalid_shape"


@pytest.mark.parametrize("version", [True, 1.0, 2, "1"])
def test_version_is_exact_integer_one(version):
    raw = _draft(_field())
    raw["version"] = version
    assert _code(compile_schema_draft(raw, _source())) == "schema_draft.invalid_shape"


def test_property_depth_byte_and_reference_limits():
    raw = _draft(_field(), _fallback("second"))
    assert (
        _code(compile_schema_draft(raw, _source(), max_schema_properties=1))
        == "schema_draft.property_limit"
    )
    assert (
        _code(compile_schema_draft(_draft(_field()), _source(), max_schema_depth=1))
        == "schema_draft.depth_exceeded"
    )
    assert (
        _code(compile_schema_draft(_draft(_field()), _source(), max_schema_bytes=8))
        == "schema_draft.too_large"
    )
    field = _field()
    field["name"]["source"] = [_ref("x", f"i0f0l{index}") for index in range(1, 1026)]
    sources = prepare_draft_sources(["x\n" * 1025], originals=["x\n" * 1025])
    assert (
        _code(compile_schema_draft(_draft(field), sources))
        == "schema_draft.reference_limit"
    )


def test_256_properties_and_configured_limits_remain_compatible():
    raw = _draft(*[_fallback(f"value_{index}") for index in range(256)])
    result = compile_schema_draft(raw, [])
    assert result.accepted and result.facts["property_count"] == 256
    raw["fields"].append(_fallback("extra"))
    assert _code(compile_schema_draft(raw, [])) == "schema_draft.property_limit"
    with pytest.raises(ValueError, match="positive"):
        compile_schema_draft(_draft(_field()), _source(), max_schema_depth=True)


def test_non_json_and_nonfinite_values_rejected_without_mutating_or_echo():
    raw = _draft(_field(), attributes={"description": float("nan")})
    assert _code(compile_schema_draft(raw, _source())) == "schema_draft.invalid_shape"
    raw["attributes"]["description"] = object()
    assert _code(compile_schema_draft(raw, _source())) == "schema_draft.invalid_shape"
    raw["attributes"]["description"] = "secret\ud800"
    result = compile_schema_draft(raw, _source())
    assert _code(result) == "schema_draft.invalid_shape"
    assert "secret" not in result.issues[0].model_dump_json()


def test_fixed_root_does_not_prove_complete_multi_entity_modeling():
    text = "Name: Cedar\nName: Birch\n"
    result = compile_schema_draft(_draft(_field()), _source(text))
    assert result.accepted and result.schema["type"] == "object"
    assert validate_result_schema(result.schema) == []
    assert validate_records_against_schema([{"name": "Cedar"}], result.schema) == []
    # Both rows need representation. Compilation cannot establish that fact.
    assert validate_records_against_schema(
        [{"assets": [{"name": "Cedar"}, {"name": "Birch"}]}], result.schema
    )


def test_business_properties_named_title_description_not_draft_attributes():
    result = compile_schema_draft(
        _draft(_fallback("title"), _fallback("description")), []
    )
    assert result.accepted
    assert set(result.schema["properties"]) == {"title", "description"}


def test_same_plan_compiles_identically_but_different_valid_modeling_still_drifts():
    direct = compile_schema_draft(_draft(_field()), _source()).schema
    alternate = compile_schema_draft(_draft(_fallback("device_name")), []).schema
    assert schema_pair_metrics(direct, direct)["contract_equal"]
    pair = schema_pair_metrics(direct, alternate)
    assert not pair["contract_equal"] and pair["path_difference_count"] == 2


def test_model_choices_of_optional_type_and_constraints_are_not_canonicalized():
    first = compile_schema_draft(
        _draft(_fallback("value", {"type": "string"}, required=False)), []
    ).schema
    second = compile_schema_draft(
        _draft(_fallback("value", {"type": "integer", "attributes": {"minimum": 0}})),
        [],
    ).schema
    pair = schema_pair_metrics(first, second)
    assert not pair["contract_equal"]
    assert pair["scalar_type_difference_count"] == 1
    assert pair["required_difference_count"] == 1


def test_multiple_input_sources_allow_separate_names_but_not_one_cross_input_label():
    texts = ["Name: Cedar\n", "State: pending\n"]
    sources = prepare_draft_sources(texts, originals=texts)
    second = _field("State", required=False)
    second["name"]["source"][0]["line_id"] = "i1f0l1"
    result = compile_schema_draft(_draft(_field(), second), sources)
    assert result.accepted and set(result.schema["properties"]) == {"name", "state"}
    assert result.schema["required"] == ["name"]


def test_draft_telemetry_is_bounded_and_present_for_rejections():
    large = compile_schema_draft(_draft(_field()), _source(), max_schema_bytes=8)
    assert large.facts["draft_bytes"] > 8
    missing = compile_schema_draft(_draft(_field("secret missing")), _source())
    assert missing.facts["source_rejection_count"] == 1
    conflict = compile_schema_draft(_draft(_field(), _field()), _source())
    assert conflict.facts["name_conflict_count"] == 1
    assert conflict.facts["source_rejection_count"] == 0
    assert all(type(value) is int for value in conflict.facts.values())
    assert "secret" not in repr(missing.facts)


@pytest.mark.parametrize("value", [{1: "value"}, ("x",), {"x"}, object()])
def test_non_json_constraint_values_are_not_coerced_by_serialization(value):
    raw = _draft(_field(node={"type": "string", "attributes": {"enum": value}}))
    assert _code(compile_schema_draft(raw, _source())) == "schema_draft.invalid_shape"


def test_raw_extra_fields_and_invalid_node_shapes_are_not_silently_dropped():
    for node in [
        {"type": "string", "fields": []},
        {"type": "object", "items": {"type": "string"}},
        {"type": "array", "fields": []},
        {"type": "string", "description": "should be in attributes"},
    ]:
        assert (
            _code(compile_schema_draft(_draft(_field(node=node)), _source()))
            == "schema_draft.invalid_shape"
        )
    raw = _draft(_field())
    raw["secret_extra"] = "secret body"
    result = compile_schema_draft(raw, _source())
    assert _code(result) == "schema_draft.invalid_shape"
    assert "secret" not in result.issues[0].model_dump_json()


def test_empty_draft_does_not_bypass_existing_leaf_requirement():
    result = compile_schema_draft(_draft(), [])
    assert _code(result) == "schema.no_leaf_fields"
    assert result.issues[0].path == "/"
