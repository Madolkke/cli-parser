"""Independent coordinate facts remain bounded and contain no source bodies."""

from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from cli_parser_agent.evaluation import HarnessError, load_dataset_registry
from cli_parser_agent.evaluation_facts import FactsInput, validate_evaluation_facts


def _range(line, start, end):
    return {
        "line_start": line,
        "column_start": start,
        "line_end": line,
        "column_end": end,
    }


@pytest.fixture
def specimen():
    text = "Report\nItem α\nValue: 甲🙂\nEmpty: \nTail\n"
    source = {"line_start": 1, "column_start": 0, "line_end": 5, "column_end": 4}
    document = {
        "facts_version": 1,
        "coordinate_system": "one_based_line_zero_based_unicode_half_open_column",
        "cases": [
            {
                "dataset_id": 1,
                "case_id": "synthetic.report",
                "input_path": "evals/test_sets/synthetic.report/inputs/001.txt",
                "entities": [
                    {
                        "entity_id": "e000",
                        "parent_id": None,
                        "kind": "root",
                        "source": source,
                    },
                    {
                        "entity_id": "e001",
                        "parent_id": "e000",
                        "kind": "repeated_entity",
                        "source": {
                            "line_start": 2,
                            "column_start": 0,
                            "line_end": 4,
                            "column_end": 7,
                        },
                    },
                ],
                "facts": [
                    {
                        "fact_id": "f001",
                        "category": "text",
                        "presence": "present",
                        "representation": "preserve_string",
                        "mandatory": True,
                        "sources": [
                            {"entity_id": "e001", "role": "label", **_range(3, 0, 5)},
                            {"entity_id": "e001", "role": "value", **_range(3, 7, 9)},
                        ],
                    },
                    {
                        "fact_id": "f002",
                        "category": "text",
                        "presence": "empty_slot",
                        "representation": "preserve_string_with_empty_slots",
                        "mandatory": True,
                        "sources": [
                            {"entity_id": "e001", "role": "label", **_range(4, 0, 5)},
                            {
                                "entity_id": "e001",
                                "role": "empty_slot",
                                **_range(4, 6, 7),
                            },
                        ],
                    },
                ],
            }
        ],
    }
    inputs = {
        "synthetic.report": FactsInput(1, document["cases"][0]["input_path"], text)
    }
    return document, inputs


def test_valid_facts_preserve_document_and_return_counts_only(specimen):
    document, inputs = specimen
    original = copy.deepcopy(document)
    result = validate_evaluation_facts(document, inputs)
    assert result == {
        "facts_version": 1,
        "cases": 1,
        "entities": 2,
        "facts": 2,
        "references": 4,
    }
    assert document == original
    assert "甲" not in repr(inputs)
    assert "Report" not in repr(inputs)


def test_zero_width_empty_and_multiline_value(specimen):
    document, inputs = specimen
    facts = document["cases"][0]["facts"]
    facts[1]["sources"][1]["column_start"] = 7
    value = facts[0]["sources"][1]
    value.update(line_start=2, column_start=0, line_end=3, column_end=9)
    assert validate_evaluation_facts(document, inputs)["references"] == 4


@pytest.mark.parametrize(
    ("location", "key", "value"),
    [
        ("root", "source_text", "SENSITIVE_BODY"),
        ("root", "facts_version", True),
        ("case", "input_path", "../../SENSITIVE_BODY"),
        ("case", "case_id", "unknown.report"),
        ("case", "dataset_id", True),
        ("case", "dataset_id", 2),
        ("entity", "kind", "SENSITIVE_BODY"),
        ("entity", "parent_id", "e001"),
        ("entity", "entity_id", "e000"),
        ("fact", "description", "SENSITIVE_BODY"),
        ("fact", "category", "SENSITIVE_BODY"),
        ("fact", "presence", "empty_slot"),
        ("fact", "representation", "SENSITIVE_BODY"),
        ("fact", "mandatory", "SENSITIVE_BODY"),
        ("ref", "role", "SENSITIVE_BODY"),
        ("ref", "entity_id", "e999"),
        ("ref", "column_end", 999),
        ("ref", "column_start", -1),
        ("ref", "line_start", True),
        ("ref", "line_end", 0),
        ("ref", "column_start", 9),
    ],
)
def test_mutation_rejected_without_echoing_candidate(specimen, location, key, value):
    document, inputs = specimen
    case = document["cases"][0]
    target = {
        "root": document,
        "case": case,
        "entity": case["entities"][1],
        "fact": case["facts"][0],
        "ref": case["facts"][0]["sources"][1],
    }[location]
    target[key] = value
    with pytest.raises(HarnessError) as error:
        validate_evaluation_facts(document, inputs)
    assert str(error.value) == "invalid evaluation facts structure or reference"


@pytest.mark.parametrize("kind", ["case", "fact", "ref"])
def test_duplicate_identity_rejected(specimen, kind):
    document, inputs = specimen
    case = document["cases"][0]
    collection = {
        "case": document["cases"],
        "fact": case["facts"],
        "ref": case["facts"][0]["sources"],
    }[kind]
    collection.append(copy.deepcopy(collection[0]))
    with pytest.raises(HarnessError):
        validate_evaluation_facts(document, inputs)


@pytest.mark.parametrize(
    "mutation",
    [
        "root_partial",
        "outside_parent",
        "wrong_empty",
        "empty_without_label",
        "absent_value",
        "empty_case",
        "cycle",
    ],
)
def test_entity_presence_and_bounds_invariants(specimen, mutation):
    document, inputs = specimen
    case = document["cases"][0]
    if mutation == "root_partial":
        case["entities"][0]["source"]["line_start"] = 2
    elif mutation == "outside_parent":
        case["facts"][0]["sources"][1].update(_range(5, 0, 4))
    elif mutation == "wrong_empty":
        case["facts"][1]["sources"][1].update(_range(4, 0, 2))
    elif mutation == "empty_without_label":
        case["facts"][1]["sources"].pop(0)
    elif mutation == "absent_value":
        case["facts"][0]["sources"].pop()
    elif mutation == "empty_case":
        case["facts"] = []
    else:
        case["entities"][0]["parent_id"] = "e001"
    with pytest.raises(HarnessError):
        validate_evaluation_facts(document, inputs)


def test_input_path_is_never_opened(specimen, monkeypatch):
    document, inputs = specimen

    def unexpected(*args, **kwargs):
        pytest.fail("facts validator must not open document-controlled paths")

    monkeypatch.setattr(Path, "open", unexpected)
    assert validate_evaluation_facts(document, inputs)["cases"] == 1


def test_registered_independent_facts_all_valid():
    root = Path(__file__).resolve().parents[2]
    document = json.loads(
        (root / "docs/schema-plan-evaluation-facts.json").read_text(encoding="utf-8")
    )
    registry = load_dataset_registry(root / "evals/datasets.toml")
    ids = {1, 3, 5, 6, 7, 9, 10, 11, 13, 14, 16, 17, 18, 19}
    inputs = {}
    for case in registry.datasets:
        if case.id in ids:
            relative = f"evals/test_sets/{case.name}/inputs/001.txt"
            inputs[case.name] = FactsInput(
                case.id, relative, (root / relative).read_text(encoding="utf-8")
            )
    assert validate_evaluation_facts(document, inputs) == {
        "facts_version": 1,
        "cases": 14,
        "entities": 233,
        "facts": 192,
        "references": 1575,
    }
