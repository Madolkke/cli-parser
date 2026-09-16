from __future__ import annotations

import json
from copy import deepcopy

import pytest

from cli_parser_agent.ttp_generation.schema_plan import (
    MAX_PLAN_REFERENCES,
    SchemaPlan,
    SourceFragment,
    compile_schema_plan,
)
from cli_parser_agent.ttp_generation.validation import validate_records_against_schema


def _ref(text: str, part: str, *, offset=0, input_index=0, fragment_index=0):
    start = text.index(part, offset)
    return {
        "input_index": input_index,
        "fragment_index": fragment_index,
        "start": start,
        "end": start + len(part),
    }


def _plan(text="Name: Atlas\n", *, label="Name", value="Atlas", **changes):
    node = {
        "id": "field",
        "kind": "value",
        "role": "name",
        "label_refs": [_ref(text, label)],
        "evidence_complete": True,
        "occurrences": [
            {"parent_instance_id": "input_0", "segments": [_ref(text, value)]}
        ],
    }
    node.update(changes)
    return {"version": 1, "nodes": [node]}


def _compile(plan, text="Name: Atlas\n", *, complete=True):
    return compile_schema_plan(plan, [SourceFragment(0, 0, text, complete)])


def _codes(result):
    return {issue.code for issue in result.issues}


def test_compiler_is_deterministic_without_mutating_model_or_sources():
    raw = _plan()
    before = deepcopy(raw)
    first = _compile(raw)
    second = _compile(SchemaPlan.model_validate(raw))
    assert first.accepted and second.accepted
    assert first.schema == second.schema
    assert raw == before
    assert first.schema["required"] == ["name"]
    assert first.schema["properties"]["name"]["type"] == "string"
    assert first.facts["enumeration_verified"] is False
    assert "title" not in json.dumps(first.schema)


@pytest.mark.parametrize("unknown", ["required", "description", "type", "enum"])
def test_plan_does_not_allow_direct_schema_control(unknown):
    plan = _plan()
    plan["nodes"][0][unknown] = "sensitive content"
    result = _compile(plan)
    assert _codes(result) == {"schema_plan.invalid_shape"}
    assert "sensitive" not in str(result)
    assert unknown not in result.issues[0].path


@pytest.mark.parametrize(
    "label,name",
    [
        ("BGP-Peer.ID:", "bgp_peer_id"),
        ("Rx  CRC Errors", "rx_crc_errors"),
        ("match", "match"),
        ("case", "case"),
        ("type", "type"),
    ],
)
def test_labels_preserve_order_abbreviations_and_soft_keywords(label, name):
    text = f"{label} value\n"
    result = _compile(_plan(text, label=label, value="value"), text)
    assert result.accepted
    assert list(result.schema["properties"]) == [name]


@pytest.mark.parametrize("label", ["class", "ignore", "123 name", "名称", "A" * 121])
def test_invalid_normalized_names_need_explicit_legal_fallback(label):
    text = f"{label}: item\n"
    raw = _plan(text, label=label, value="item")
    assert _codes(_compile(raw, text)) == {"schema_plan.invalid_name"}
    raw["nodes"][0].update(fallback_name="device_class", fallback_reason="invalid_name")
    result = _compile(raw, text)
    assert result.accepted
    assert result.facts["fallback_names"] == 1
    assert list(result.schema["properties"]) == ["device_class"]


def test_legal_label_cannot_be_overridden_with_synonym():
    result = _compile(
        _plan(fallback_name="device_name", fallback_reason="invalid_name")
    )
    assert _codes(result) == {"schema_plan.unnecessary_fallback"}


@pytest.mark.parametrize("name", ["class", "bad__name", "bad_", "9name", "ignore"])
def test_illegal_fallback_rejected(name):
    result = _compile(
        _plan(label_refs=[], fallback_name=name, fallback_reason="unlabeled")
    )
    assert _codes(result) == {"schema_plan.invalid_name"}


def test_multiline_header_keeps_qualifier_order_and_parent_does_not_remove_words():
    text = "BGP\nPeer ID\nGroup\nBGP Peer ID: a\n"
    plan = {
        "version": 1,
        "nodes": [
            {
                "id": "group",
                "kind": "object",
                "label_refs": [_ref(text, "Group")],
                "evidence_complete": True,
                "instances": [
                    {
                        "instance_id": "group_0",
                        "parent_instance_id": "input_0",
                        "spans": [_ref(text, "BGP Peer ID: a")],
                        "complete": True,
                    }
                ],
            },
            {
                "id": "value",
                "parent_id": "group",
                "kind": "value",
                "role": "identifier",
                "label_refs": [_ref(text, "BGP"), _ref(text, "Peer ID")],
                "evidence_complete": True,
                "occurrences": [
                    {"parent_instance_id": "group_0", "segments": [_ref(text, "a")]}
                ],
            },
        ],
    }
    result = _compile(plan, text)
    assert result.accepted
    assert "bgp_peer_id" in result.schema["properties"]["group"]["properties"]


def test_collision_requires_evidenced_qualifiers_or_explicit_fallback():
    text = "Input Count: 2\nOutput Count: 3\n"
    first = _plan(text, label="Count", value="2", role="count")["nodes"][0]
    second = deepcopy(first)
    second.update(
        id="second",
        label_refs=[_ref(text, "Count", offset=15)],
        occurrences=[{"parent_instance_id": "input_0", "segments": [_ref(text, "3")]}],
    )
    plan = {"version": 1, "nodes": [first, second]}
    assert "schema_plan.name_collision" in _codes(_compile(plan, text))
    first["qualifier_refs"] = [_ref(text, "Input")]
    second["qualifier_refs"] = [_ref(text, "Output")]
    result = _compile(plan, text)
    assert result.accepted
    assert set(result.schema["properties"]) == {"input_count", "output_count"}
    assert all(
        value["type"] == "integer" for value in result.schema["properties"].values()
    )


def test_unnecessary_qualifier_is_not_a_parent_prefix_escape():
    text = "Device Name: Atlas\n"
    plan = _plan(text, label="Name", qualifier_refs=[_ref(text, "Device")])
    assert _codes(_compile(plan, text)) == {"schema_plan.invalid_qualifier"}


@pytest.mark.parametrize("value", ["0", "14", "3000000000000000000000000"])
def test_fully_evidenced_counts_compile_to_integer(value):
    text = f"Count: {value}\n"
    result = _compile(_plan(text, label="Count", value=value, role="count"), text)
    assert result.schema["properties"]["count"]["type"] == "integer"
    assert "minimum" not in result.schema["properties"]["count"]


@pytest.mark.parametrize(
    "value", ["01", "00", "-1", "+2", "1.0", "2 ms", "N/A", "１２", "2 "]
)
def test_ambiguous_or_noncanonical_counts_remain_strings(value):
    text = f"Count: {value}\n"
    result = _compile(_plan(text, label="Count", value=value, role="count"), text)
    assert result.accepted
    assert result.schema["properties"]["count"]["type"] == "string"


@pytest.mark.parametrize(
    "role,value",
    [
        ("identifier", "42"),
        ("version", "2.6.1"),
        ("status", "!unavailable"),
        ("timestamp", "2031-08-14 10:40:00Z"),
        ("duration", "2 days 03:04:05"),
        ("name", "svc-a/b"),
        ("measurement", "20"),
    ],
)
def test_logical_roles_preserve_complete_values(role, value):
    text = f"Item: {value}\n"
    result = _compile(_plan(text, label="Item", value=value, role=role), text)
    assert result.accepted
    assert result.schema["properties"]["item"]["type"] == "string"
    assert validate_records_against_schema([{"item": value}], result.schema) == []


@pytest.mark.parametrize("complete,node_complete", [(False, True), (True, False)])
def test_incomplete_evidence_keeps_fields_optional_and_counts_as_strings(
    complete, node_complete
):
    text = "Count: 12\n"
    result = _compile(
        _plan(
            text,
            label="Count",
            value="12",
            role="count",
            evidence_complete=node_complete,
        ),
        text,
        complete=complete,
    )
    assert result.accepted
    assert "required" not in result.schema
    assert result.schema["properties"]["count"]["type"] == "string"


def _entities():
    text = "Asset a\nResult: ok,\nAsset b\nAsset c\nResult: ,\n"
    entities = ["Asset a\nResult: ok,\n", "Asset b\n", "Asset c\nResult: ,\n"]
    container = {
        "id": "assets",
        "kind": "collection",
        "fallback_name": "assets",
        "fallback_reason": "unlabeled",
        "evidence_complete": True,
        "instances": [
            {
                "instance_id": f"asset_{i}",
                "parent_instance_id": "input_0",
                "spans": [_ref(text, content)],
                "complete": True,
            }
            for i, content in enumerate(entities)
        ],
    }
    names = {
        "id": "name",
        "parent_id": "assets",
        "kind": "value",
        "role": "name",
        "fallback_name": "name",
        "fallback_reason": "unlabeled",
        "evidence_complete": True,
        "occurrences": [
            {
                "parent_instance_id": f"asset_{i}",
                "segments": [
                    _ref(text, f"Asset {name}")
                    | {"start": text.index(f"Asset {name}") + 6}
                ],
            }
            for i, name in enumerate("abc")
        ],
    }
    zero = _ref(text, "Result: ,")["end"] - 1
    results = {
        "id": "result",
        "parent_id": "assets",
        "kind": "value",
        "role": "status",
        "label_refs": [_ref(text, "Result")],
        "evidence_complete": True,
        "occurrences": [
            {"parent_instance_id": "asset_0", "segments": [_ref(text, "ok")]},
            {
                "parent_instance_id": "asset_2",
                "segments": [
                    {"input_index": 0, "fragment_index": 0, "start": zero, "end": zero}
                ],
                "empty_line": _ref(text, "Result: ,\n"),
            },
        ],
    }
    return text, {"version": 1, "nodes": [container, names, results]}


def test_instances_with_optional_missing_section_and_explicit_empty_slot():
    text, plan = _entities()
    result = _compile(plan, text)
    assert result.accepted, result.issues
    items = result.schema["properties"]["assets"]["items"]
    assert items["required"] == ["name"]
    assert result.schema["required"] == ["assets"]
    records = [
        {
            "assets": [
                {"name": "a", "result": "ok"},
                {"name": "b"},
                {"name": "c", "result": ""},
            ]
        }
    ]
    assert validate_records_against_schema(records, result.schema) == []


def test_incomplete_parent_entity_makes_child_required_conservative():
    text, plan = _entities()
    plan["nodes"][0]["instances"][1]["complete"] = False
    result = _compile(plan, text)
    assert result.accepted
    assert "required" not in result.schema["properties"]["assets"]["items"]


def test_empty_slot_needs_complete_nonempty_line_within_same_parent():
    text, plan = _entities()
    plan["nodes"][2]["occurrences"][1].pop("empty_line")
    assert "schema_plan.empty_slot_evidence" in _codes(_compile(plan, text))


def test_evidence_cannot_be_moved_between_entities():
    text, plan = _entities()
    plan["nodes"][2]["occurrences"][0]["parent_instance_id"] = "asset_1"
    assert "schema_plan.outside_parent" in _codes(_compile(plan, text))


def test_duplicate_occurrence_is_not_an_implicit_multivalue_field():
    plan = _plan()
    plan["nodes"][0]["occurrences"] *= 2
    assert "schema_plan.duplicate_occurrence" in _codes(_compile(plan))


def test_multiple_inputs_are_root_records_not_entity_wrappers():
    texts = ["Name: Atlas\n", "Name: Echo\n"]
    plan = _plan()
    plan["nodes"][0]["occurrences"].append(
        {
            "parent_instance_id": "input_1",
            "segments": [_ref(texts[1], "Echo", input_index=1)],
        }
    )
    result = compile_schema_plan(
        plan, [SourceFragment(i, 0, text) for i, text in enumerate(texts)]
    )
    assert result.accepted
    assert result.schema["required"] == ["name"]
    assert set(result.schema["properties"]) == {"name"}
    plan["nodes"][0]["occurrences"].pop()
    optional = compile_schema_plan(
        plan, [SourceFragment(i, 0, text) for i, text in enumerate(texts)]
    )
    assert "required" not in optional.schema


def test_unicode_positions_are_code_points_and_not_bytes():
    text = "Name: 星 Atlas\n"
    result = _compile(_plan(text, value="星 Atlas"), text)
    assert result.accepted
    bad = _plan(text, value="星 Atlas")
    bad["nodes"][0]["occurrences"][0]["segments"][0]["end"] = len(text.encode("utf-8"))
    assert "schema_plan.invalid_reference" in _codes(_compile(bad, text))


@pytest.mark.parametrize(
    "capture,separator",
    [
        ("joined_token", "空分隔符"),
        ("joined_values", "单个空格"),
        ("multiline", "换行"),
    ],
)
def test_multisegment_capture_has_fixed_preservation_description(capture, separator):
    text = "Name: svc\n-a/b\n"
    plan = _plan(text, value="svc", capture=capture)
    plan["nodes"][0]["occurrences"][0]["segments"].append(_ref(text, "-a/b"))
    result = _compile(plan, text)
    assert result.accepted
    assert separator in result.schema["properties"]["name"]["description"]
    plan["nodes"][0]["occurrences"][0]["segments"].reverse()
    assert "schema_plan.invalid_segments" in _codes(_compile(plan, text))


def test_sampling_gaps_cannot_be_joined_as_a_value_and_never_imply_required():
    plan = _plan(capture="multiline")
    sources = [
        SourceFragment(0, 0, "Name: Atlas\n", False),
        SourceFragment(0, 1, "Echo\n", False),
    ]
    result = compile_schema_plan(plan, sources)
    assert result.accepted and "required" not in result.schema
    plan["nodes"][0]["occurrences"][0]["segments"].append(
        _ref("Echo\n", "Echo", fragment_index=1)
    )
    assert "schema_plan.invalid_segments" in _codes(compile_schema_plan(plan, sources))


def test_shared_label_split_requires_disjoint_values_and_never_adds_raw_copy():
    text = "Release: 2.4 build7\n"
    version = _plan(
        text,
        label="Release",
        value="2.4",
        role="version",
        fallback_name="version",
        fallback_reason="split_component",
    )["nodes"][0]
    build = deepcopy(version)
    build.update(
        id="build",
        role="build",
        fallback_name="build",
        occurrences=[
            {"parent_instance_id": "input_0", "segments": [_ref(text, "build7")]}
        ],
    )
    plan = {"version": 1, "nodes": [version, build]}
    result = _compile(plan, text)
    assert result.accepted
    assert set(result.schema["properties"]) == {"version", "build"}
    build["occurrences"][0]["segments"] = [_ref(text, "2.4 build7")]
    assert "schema_plan.invalid_split" in _codes(_compile(plan, text))


@pytest.mark.parametrize(
    "change", ["parent_missing", "cycle", "duplicate", "root_reserved"]
)
def test_invalid_node_relationships_are_rejected(change):
    plan = _plan()
    if change == "parent_missing":
        plan["nodes"][0]["parent_id"] = "missing"
    elif change == "cycle":
        plan["nodes"][0]["parent_id"] = "field"
    elif change == "duplicate":
        plan["nodes"] *= 2
    else:
        plan["nodes"][0]["id"] = "root"
    assert not _compile(plan).accepted


def test_shared_schema_validator_catches_leafless_container():
    text, plan = _entities()
    plan["nodes"] = plan["nodes"][:1]
    result = _compile(plan, text)
    assert "schema.no_leaf_fields" in _codes(result)


def test_schema_depth_counts_collection_item_object_layer():
    text = "x"
    nodes = []
    for index in range(8):
        nodes.append(
            {
                "id": f"n{index}",
                "parent_id": "root" if not index else f"n{index - 1}",
                "kind": "collection",
                "fallback_name": f"node{index}",
                "fallback_reason": "unlabeled",
                "instances": [
                    {
                        "instance_id": f"i{index}",
                        "parent_instance_id": "input_0"
                        if not index
                        else f"i{index - 1}",
                        "spans": [_ref(text, "x")],
                    }
                ],
            }
        )
    result = _compile({"version": 1, "nodes": nodes}, text)
    assert "schema_plan.depth_exceeded" in _codes(result)


def test_plan_limit_and_errors_do_not_leak_unknown_content():
    plan = _plan()
    plan["secrets_must_not_appear"] = "秘密" * (64 * 1024)
    result = _compile(plan)
    assert _codes(result) == {"schema_plan.too_large"}
    assert "秘密" not in str(result) and "secrets" not in str(result)
    plan = _plan()
    plan["nodes"] *= 257
    assert not _compile(plan).accepted


def test_reference_limit_is_not_silent_truncation(monkeypatch):
    # A smaller guard exercises the same bound without generating an oversized plan.
    import cli_parser_agent.ttp_generation.schema_plan as module

    monkeypatch.setattr(module, "MAX_PLAN_REFERENCES", 1)
    assert MAX_PLAN_REFERENCES == 1024
    result = _compile(_plan())
    assert _codes(result) == {"schema_plan.reference_limit"}


def test_sources_must_have_unique_coordinates_and_cannot_be_model_supplied():
    duplicate = [SourceFragment(0, 0, "Name: Atlas\n")] * 2
    assert _codes(compile_schema_plan(_plan(), duplicate)) == {
        "schema_plan.invalid_sources"
    }
    assert _codes(compile_schema_plan(_plan(), [])) == {"schema_plan.invalid_sources"}
    bad = _plan()
    bad["nodes"][0]["occurrences"][0]["segments"][0]["fragment_index"] = 9
    assert "schema_plan.invalid_reference" in _codes(_compile(bad))


def test_same_but_wrong_plan_determinism_is_not_semantic_validation():
    raw = _plan()
    raw["nodes"][0]["role"] = "identifier"
    first = _compile(raw)
    second = _compile(raw)
    assert first.accepted and first.schema == second.schema
    assert first.facts["enumeration_verified"] is False
    assert "semantic_valid" not in first.facts


def test_explicit_empty_collection_has_no_fabricated_entity_and_retains_schema():
    text, plan = _entities()
    empty_text = "Assets: no rows\n"
    plan["nodes"][0]["empty_collections"] = [
        {
            "parent_instance_id": "input_1",
            "marker": _ref(empty_text, "Assets: no rows", input_index=1),
        }
    ]
    result = compile_schema_plan(
        plan, [SourceFragment(0, 0, text), SourceFragment(1, 0, empty_text)]
    )
    assert result.accepted, result.issues
    assert result.schema["required"] == ["assets"]
    assert validate_records_against_schema([{"assets": []}], result.schema) == []
    assert result.schema["properties"]["assets"]["items"]["required"] == ["name"]


def test_only_empty_collection_can_coexist_with_root_field_without_invented_items():
    text = "Name: Atlas\nAssets: no rows\n"
    plan = _plan(text)
    plan["nodes"].append(
        {
            "id": "assets",
            "kind": "collection",
            "label_refs": [_ref(text, "Assets")],
            "evidence_complete": True,
            "empty_collections": [
                {
                    "parent_instance_id": "input_0",
                    "marker": _ref(text, "Assets: no rows"),
                }
            ],
        }
    )
    result = _compile(plan, text)
    assert result.accepted
    assert result.schema["properties"]["assets"]["items"] == {
        "type": "object",
        "properties": {},
        "additionalProperties": False,
    }
    assert (
        validate_records_against_schema(
            [{"name": "Atlas", "assets": []}], result.schema
        )
        == []
    )
    assert validate_records_against_schema(
        [{"name": "Atlas", "assets": [{"made_up": "x"}]}], result.schema
    )


def test_empty_and_populated_collection_cannot_claim_same_parent():
    text, plan = _entities()
    plan["nodes"][0]["empty_collections"] = [
        {"parent_instance_id": "input_0", "marker": _ref(text, "Asset")}
    ]
    assert "schema_plan.empty_collection_evidence" in _codes(_compile(plan, text))


@pytest.mark.parametrize("version", [True, "1", 1.0])
def test_plan_version_does_not_coerce_bool_string_or_float(version):
    plan = _plan()
    plan["version"] = version
    assert _codes(_compile(plan)) == {"schema_plan.invalid_shape"}


def test_whitespace_label_is_not_a_semantic_name_source():
    text = "Name: Atlas\n"
    plan = _plan()
    plan["nodes"][0]["label_refs"] = [_ref(text, " ")]
    assert "schema_plan.empty_label" in _codes(_compile(plan, text))


def test_empty_slot_cannot_be_placed_after_line_ending():
    text, plan = _entities()
    occurrence = plan["nodes"][2]["occurrences"][1]
    end = occurrence["empty_line"]["end"]
    occurrence["segments"][0].update(start=end, end=end)
    assert "schema_plan.empty_slot_evidence" in _codes(_compile(plan, text))


def test_schema_plan_property_and_depth_boundaries():
    text = "x"
    nodes = []
    for index in range(256):
        nodes.append(
            {
                "id": f"n{index}",
                "kind": "value",
                "role": "text",
                "fallback_name": f"field{index}",
                "fallback_reason": "unlabeled",
                "occurrences": [
                    {"parent_instance_id": "input_0", "segments": [_ref(text, "x")]}
                ],
            }
        )
    result = _compile({"version": 1, "nodes": nodes}, text)
    assert result.accepted
    assert len(result.schema["properties"]) == 256
    nodes.append(deepcopy(nodes[0]))
    assert _codes(_compile({"version": 1, "nodes": nodes}, text)) == {
        "schema_plan.invalid_shape"
    }


def test_reaching_reference_cap_keeps_complete_claim_conservative(monkeypatch):
    import cli_parser_agent.ttp_generation.schema_plan as module

    monkeypatch.setattr(module, "MAX_PLAN_REFERENCES", 2)
    result = _compile(_plan())
    assert result.accepted
    assert "required" not in result.schema
    assert result.facts["evidence_incomplete_nodes"] == 1


def test_model_cannot_add_arbitrary_constraints_or_annotations():
    plan = _plan()
    plan["nodes"][0]["description"] = "Convert all unavailable values to empty"
    result = _compile(plan)
    assert not result.accepted
    assert "Convert all" not in str(result)


def test_equivalent_node_and_instance_order_has_identical_contract():
    text, plan = _entities()
    expected = _compile(plan, text).schema
    plan["nodes"][0]["instances"].reverse()
    plan["nodes"][1]["occurrences"].reverse()
    plan["nodes"].reverse()
    actual = _compile(plan, text)
    assert actual.accepted
    assert actual.schema == expected


def test_normalized_name_accepts_exact_character_limit():
    label = "A" * 120
    text = f"{label}: sample\n"
    result = _compile(_plan(text, label=label, value="sample"), text)
    assert result.accepted
    assert next(iter(result.schema["properties"])) == "a" * 120


def test_depth_sixteen_is_accepted_before_seventeen_is_rejected():
    text = "x"
    nodes = []
    for index in range(14):
        nodes.append(
            {
                "id": f"n{index}",
                "parent_id": "root" if not index else f"n{index - 1}",
                "kind": "object",
                "fallback_name": f"node{index}",
                "fallback_reason": "unlabeled",
                "instances": [
                    {
                        "instance_id": f"i{index}",
                        "parent_instance_id": "input_0"
                        if not index
                        else f"i{index - 1}",
                        "spans": [_ref(text, "x")],
                    }
                ],
            }
        )
    leaf = _plan(text, label="x", value="x")["nodes"][0]
    leaf["parent_id"] = "n13"
    leaf["occurrences"][0]["parent_instance_id"] = "i13"
    nodes.append(leaf)
    result = _compile({"version": 1, "nodes": nodes}, text)
    assert result.accepted, result.issues


def test_parent_instance_spans_cannot_overlap_or_duplicate_evidence():
    text, plan = _entities()
    plan["nodes"][0]["instances"][0]["spans"] *= 2
    result = _compile(plan, text)
    assert "schema_plan.invalid_instance" in _codes(result)


def test_overlapping_sibling_entities_rejected():
    text, plan = _entities()
    plan["nodes"][0]["instances"][1]["spans"] = plan["nodes"][0]["instances"][0][
        "spans"
    ]
    result = _compile(plan, text)
    assert "schema_plan.overlapping_instances" in _codes(result)


@pytest.mark.parametrize(
    "coordinate", ["start", "end", "input_index", "fragment_index"]
)
def test_reference_coordinates_do_not_coerce_booleans(coordinate):
    plan = _plan()
    plan["nodes"][0]["label_refs"][0][coordinate] = True
    assert _codes(_compile(plan)) == {"schema_plan.invalid_shape"}


def test_validation_issues_contain_no_field_names_or_input_text():
    text = "Sensitive-A: secret0\nSensitive-A: secret1\n"
    first = _plan(text, label="Sensitive-A", value="secret0")["nodes"][0]
    second = deepcopy(first)
    second["id"] = "second"
    second["occurrences"][0]["segments"] = [_ref(text, "secret1")]
    result = _compile({"version": 1, "nodes": [first, second]}, text)
    assert "schema_plan.name_collision" in _codes(result)
    issues = json.dumps([issue.model_dump() for issue in result.issues])
    assert "sensitive" not in issues.lower() and "secret" not in issues.lower()


def test_compiled_contract_remains_distinct_for_unlabeled_synonyms():
    from cli_parser_agent.evaluation import schema_pair_metrics

    first = _plan(label_refs=[], fallback_name="device", fallback_reason="unlabeled")
    second = deepcopy(first)
    second["nodes"][0]["fallback_name"] = "host"
    left = _compile(first).schema
    right = _compile(second).schema
    comparison = schema_pair_metrics(left, right)
    assert comparison["contract_equal"] is False


def test_label_parts_cannot_reorder_words_or_cross_sampling_gaps():
    text = "Rx Error Count: 2\n"
    plan = _plan(text, label="Rx Error Count", value="2", role="count")
    node = plan["nodes"][0]
    node["label_refs"] = [_ref(text, "Count"), _ref(text, "Rx Error")]
    outcome = _compile(plan, text)
    assert "schema_plan.label_order" in _codes(outcome)
    node["label_refs"].reverse()
    assert _compile(plan, text).accepted
    node["label_refs"][1]["fragment_index"] = 1
    outcome = compile_schema_plan(
        plan, [SourceFragment(0, 0, text, False), SourceFragment(0, 1, text, False)]
    )
    assert "schema_plan.label_order" in _codes(outcome)


def test_incomplete_container_instance_does_not_become_required():
    text, plan = _entities()
    plan["nodes"][0]["instances"][0]["complete"] = False
    outcome = _compile(plan, text)
    assert outcome.accepted
    assert "required" not in outcome.schema
    assert outcome.facts["evidence_incomplete_nodes"] == 3
