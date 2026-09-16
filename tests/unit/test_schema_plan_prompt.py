"""Exercise the actual displayed plan examples and displayed source coordinates."""

import json
import re
from copy import deepcopy

import pytest

from cli_parser_agent.ttp_generation.agent.schema_plan_prompt import (
    SCHEMA_PLAN_SYSTEM_PROMPT,
    build_schema_plan_task,
    plan_sources,
    plan_system_prompt,
)
from cli_parser_agent.ttp_generation.sampling import (
    TRUNCATION_MARKER,
    sample_command_outputs,
)
from cli_parser_agent.ttp_generation.schema_plan import (
    SourceFragment,
    compile_schema_plan,
)
from cli_parser_agent.ttp_generation.validation import validate_records_against_schema


def _examples():
    sources = re.findall(
        r"<sources_json>(.*?)</sources_json>", SCHEMA_PLAN_SYSTEM_PROMPT, re.S
    )
    plans = re.findall(
        r"<plan_arguments_json>(.*?)</plan_arguments_json>",
        SCHEMA_PLAN_SYSTEM_PROMPT,
        re.S,
    )
    assert len(sources) == len(plans) == 2
    return [
        (
            [SourceFragment(**source) for source in json.loads(raw_sources)],
            json.loads(raw_arguments)["plan"],
        )
        for raw_sources, raw_arguments in zip(sources, plans, strict=True)
    ]


@pytest.mark.parametrize("example", [0, 1])
def test_every_displayed_plan_compiles_through_shared_schema_gate(example):
    sources, plan = _examples()[example]
    outcome = compile_schema_plan(plan, sources)
    assert outcome.accepted, outcome.issues
    assert outcome.facts["enumeration_verified"] is False
    assert outcome.facts["evidence_incomplete_nodes"] == 0


def test_displayed_flat_example_is_one_root_without_artificial_container():
    sources, plan = _examples()[0]
    schema = compile_schema_plan(plan, sources).schema
    assert schema["required"] == ["name"]
    assert set(schema["properties"]) == {"name"}
    assert validate_records_against_schema([{"name": "Atlas"}], schema) == []


def test_displayed_repeated_example_has_correct_unicode_empty_and_status_evidence():
    sources, plan = _examples()[1]
    schema = compile_schema_plan(plan, sources).schema
    assert schema["required"] == ["items"]
    item_schema = schema["properties"]["items"]["items"]
    assert item_schema["required"] == ["item", "result"]
    assert set(item_schema["properties"]) == {"item", "result"}
    assert (
        validate_records_against_schema(
            [
                {
                    "items": [
                        {"item": "星", "result": ""},
                        {"item": "Oak", "result": "!idle"},
                    ]
                },
                {"items": []},
            ],
            schema,
        )
        == []
    )
    # Check actual referenced values; schema-valid handwritten records alone do
    # not prove the displayed coordinates describe those records.
    indexed = {
        (source.input_index, source.fragment_index): source.text for source in sources
    }
    captured = {}
    for node in plan["nodes"]:
        for occurrence in node.get("occurrences", []):
            ref = occurrence["segments"][0]
            value = indexed[ref["input_index"], ref["fragment_index"]][
                ref["start"] : ref["end"]
            ]
            captured[node["id"], occurrence["parent_instance_id"]] = value
    assert captured == {
        ("item", "first"): "星",
        ("item", "second"): "Oak",
        ("result", "first"): "",
        ("result", "second"): "!idle",
    }
    for node in plan["nodes"][1:]:
        ref = node["label_refs"][0]
        assert sources[0].text[ref["start"] : ref["end"]].lower() == node["id"]
    spans = plan["nodes"][0]["instances"]
    assert [
        sources[0].text[entry["spans"][0]["start"] : entry["spans"][0]["end"]]
        for entry in spans
    ] == [
        "Item: 星\nResult: ,\n",
        "Item: Oak\nResult: !idle,\n",
    ]


def test_examples_do_not_silently_infer_completeness_when_omitted():
    sources, raw = _examples()[1]
    plan = deepcopy(raw)
    for node in plan["nodes"]:
        node.pop("evidence_complete")
        for instance in node.get("instances", []):
            instance.pop("complete")
    outcome = compile_schema_plan(plan, sources)
    assert outcome.accepted
    assert "required" not in outcome.schema
    assert "required" not in outcome.schema["properties"]["items"]["items"]


def test_variant_prompt_has_identical_modeling_guidance_except_confirmation():
    direct, confirm = plan_system_prompt(False), plan_system_prompt(True)
    assert direct.startswith(SCHEMA_PLAN_SYSTEM_PROMPT)
    assert confirm.startswith(SCHEMA_PLAN_SYSTEM_PROMPT)
    assert "confirm_schema_plan()" not in direct
    assert "confirm_schema_plan()" in confirm
    assert "新提交会撤销旧待确认结果" in confirm


def _task_sources(texts, originals=None):
    task = build_schema_plan_task(texts, originals=originals)
    return json.loads(
        task.split("<source_fragments_json>", 1)[1].rsplit(
            "</source_fragments_json>", 1
        )[0]
    )


def test_task_line_offsets_are_unicode_positions_preserving_original_whitespace():
    text = "Name: 星\r\n Note: e\u0301\t\nlast🙂"
    source = _task_sources([text])[0]
    assert source["complete"] is True
    assert "".join(line["text"] for line in source["lines"]) == text
    assert all(
        text[line["start"] : line["end"]] == line["text"] for line in source["lines"]
    )
    assert source["lines"][-1]["end"] == len(text)
    assert source["lines"][-1]["end"] != len(text.encode("utf-8"))


def test_actual_sampling_marker_is_removed_without_exposing_omitted_text():
    original = "Start: a\n" + "PRIVATE_OMITTED_LINE\n" * 20 + "End: b\n"
    sampled = sample_command_outputs([original], total_char_budget=70)[0]
    assert sampled.truncated
    fragments = plan_sources([sampled.text], originals=[original])
    assert len(fragments) == 2
    assert not any(fragment.complete for fragment in fragments)
    assert all(TRUNCATION_MARKER not in fragment.text for fragment in fragments)
    task = _task_sources([sampled.text], [original])
    assert "".join(
        line["text"] for source in task for line in source["lines"]
    ) == "".join(fragment.text for fragment in fragments)
    assert task[1]["lines"][0]["start"] == 0
    assert len(json.dumps(task)) < len(json.dumps(original))


def test_literal_marker_in_unmodified_input_is_real_text_not_sampling_signal():
    original = "Header\n" + TRUNCATION_MARKER + "Tail\n"
    for originals in [None, [original]]:
        fragments = plan_sources([original], originals=originals)
        assert len(fragments) == 1 and fragments[0].complete
        assert fragments[0].text == original


def test_marker_in_original_head_is_not_confused_with_inserted_sampling_marker():
    original = "Head\n" + TRUNCATION_MARKER + "prefix\n" + "middle\n" * 50 + "Tail\n"
    sampled = sample_command_outputs([original], total_char_budget=100)[0]
    assert sampled.text.count(TRUNCATION_MARKER) == 2
    fragments = plan_sources([sampled.text], originals=[original])
    assert len(fragments) == 2
    assert TRUNCATION_MARKER in fragments[0].text
    assert fragments[0].text.startswith("Head\n")
    assert fragments[1].text.endswith("Tail\n")


@pytest.mark.parametrize("budget", [1, 12, len(TRUNCATION_MARKER)])
def test_marker_only_sampling_has_no_invented_evidence(budget):
    original = "Name: Atlas\n" * 20
    sampled = sample_command_outputs([original], total_char_budget=budget)[0]
    assert plan_sources([sampled.text], originals=[original]) == (
        SourceFragment(0, 0, "", False),
    )
    assert _task_sources([sampled.text], [original]) == [
        {
            "input_index": 0,
            "fragment_index": 0,
            "complete": False,
            "lines": [],
        }
    ]


def test_arbitrary_changed_text_cannot_be_presented_as_original_evidence():
    assert plan_sources(["Name: fabricated\n"], originals=["Name: Atlas\n"]) == (
        SourceFragment(0, 0, "", False),
    )
    with pytest.raises(ValueError, match="counts"):
        plan_sources(["a"], originals=[])


@pytest.mark.parametrize("missing_index", [0, 1, 2])
@pytest.mark.parametrize(
    "unavailable", [TRUNCATION_MARKER, TRUNCATION_MARKER[:4], "changed"]
)
def test_no_evidence_input_cannot_disappear_from_root_count_or_required(
    missing_index, unavailable
):
    originals = ["Name: Atlas\n"] * 3
    texts = list(originals)
    texts[missing_index] = unavailable
    sources = plan_sources(texts, originals=originals)
    assert {source.input_index for source in sources} == {0, 1, 2}
    shown = _task_sources(texts, originals)
    assert len(shown) == 3
    assert shown[missing_index]["complete"] is False
    assert shown[missing_index]["lines"] == []
    _, plan = _examples()[0]
    field = plan["nodes"][0]
    available = [index for index in range(3) if index != missing_index]
    field["label_refs"][0]["input_index"] = available[0]
    field["occurrences"] = [
        {
            "parent_instance_id": f"input_{index}",
            "segments": [
                {"input_index": index, "fragment_index": 0, "start": 6, "end": 11}
            ],
        }
        for index in available
    ]
    outcome = compile_schema_plan(plan, sources)
    assert outcome.accepted, outcome.issues
    assert outcome.facts["input_count"] == 3
    assert outcome.facts["incomplete_inputs"] == 1
    assert "required" not in outcome.schema


def test_empty_incomplete_metadata_is_not_a_referenceable_empty_slot():
    _, plan = _examples()[0]
    node = plan["nodes"][0]
    node["label_refs"][0].update(start=0, end=0)
    node["occurrences"][0]["segments"][0].update(start=0, end=0)
    node["occurrences"][0]["empty_line"] = {
        "input_index": 0,
        "fragment_index": 0,
        "start": 0,
        "end": 0,
    }
    outcome = compile_schema_plan(plan, [SourceFragment(0, 0, "", False)])
    assert not outcome.accepted
    assert "schema_plan.invalid_reference" in {issue.code for issue in outcome.issues}


def test_ambiguous_sampling_marker_keeps_all_inputs_as_incomplete_metadata():
    # Both marker positions have matching original prefix/suffix. Neither is
    # selected because that would claim an unproved fragment boundary.
    sampled = TRUNCATION_MARKER * 2
    original = TRUNCATION_MARKER * 3
    sources = plan_sources(
        ["Name: Atlas\n", sampled], originals=["Name: Atlas\n", original]
    )
    assert len(sources) == 2
    assert sources[-1] == SourceFragment(1, 0, "", False)


def test_large_table_uses_representative_evidence_without_dropping_business_fields():
    header = "Item Name | State | Build | Version | Count | Duration | Identifier\n"
    row_values = ["alpha", "!ready", "b7", "2.6.1", "12", "2 days", "svc/a-b"]
    row = " | ".join(row_values) + "\n"
    text = header + row * 73
    _, example = _examples()[1]
    container = example["nodes"][0]
    container["evidence_complete"] = False
    container["instances"] = [
        {
            "instance_id": "representative",
            "parent_instance_id": "input_0",
            "complete": True,
            "spans": [
                {
                    "input_index": 0,
                    "fragment_index": 0,
                    "start": len(header),
                    "end": len(header) + len(row),
                }
            ],
        }
    ]
    container["empty_collections"] = []
    roles = ["name", "status", "build", "version", "count", "duration", "identifier"]
    labels = [
        "Item Name",
        "State",
        "Build",
        "Version",
        "Count",
        "Duration",
        "Identifier",
    ]
    nodes = [container]
    value_start = len(header)
    for index, (label, role, value) in enumerate(
        zip(labels, roles, row_values, strict=True)
    ):
        start = text.index(value, value_start)
        label_start = header.index(label)
        nodes.append(
            {
                "id": f"field{index}",
                "parent_id": "items",
                "kind": "value",
                "role": role,
                "evidence_complete": False,
                "label_refs": [
                    {
                        "input_index": 0,
                        "fragment_index": 0,
                        "start": label_start,
                        "end": label_start + len(label),
                    }
                ],
                "occurrences": [
                    {
                        "parent_instance_id": "representative",
                        "segments": [
                            {
                                "input_index": 0,
                                "fragment_index": 0,
                                "start": start,
                                "end": start + len(value),
                            }
                        ],
                    }
                ],
            }
        )
        value_start = start + len(value)
    plan = {"version": 1, "nodes": nodes}
    outcome = compile_schema_plan(plan, [SourceFragment(0, 0, text)])
    assert outcome.accepted, outcome.issues
    fields = outcome.schema["properties"]["items"]["items"]
    assert set(fields["properties"]) == {
        "item_name",
        "state",
        "build",
        "version",
        "count",
        "duration",
        "identifier",
    }
    assert "required" not in fields and "required" not in outcome.schema
    assert all(field["type"] == "string" for field in fields["properties"].values())
    assert outcome.facts["evidence_incomplete_nodes"] == 8
    assert outcome.facts["enumeration_verified"] is False
    assert len(json.dumps(plan, ensure_ascii=False).encode("utf-8")) < 5000
    expected_item = {
        "item_name": "alpha",
        "state": "!ready",
        "build": "b7",
        "version": "2.6.1",
        "count": "12",
        "duration": "2 days",
        "identifier": "svc/a-b",
    }
    assert (
        validate_records_against_schema(
            [{"items": [expected_item] * 73}], outcome.schema
        )
        == []
    )
