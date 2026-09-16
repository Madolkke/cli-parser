"""Validate bounded source-coordinate facts without exporting source content."""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, field

from .evaluation import HarnessError

_COORDINATES = "one_based_line_zero_based_unicode_half_open_column"
_SOURCE_KEYS = frozenset({"line_start", "column_start", "line_end", "column_end"})
_KINDS = frozenset({"root", "section", "repeated_entity", "nested_entity"})
_ROLES = frozenset({"label", "qualifier", "value", "empty_slot"})
_CATEGORIES = frozenset(
    {
        "identifier",
        "text",
        "status",
        "version",
        "build",
        "timestamp",
        "duration",
        "measurement",
        "count",
        "category_count",
        "entity_discriminator",
        "ordered_values",
    }
)
_REPRESENTATIONS = frozenset(
    {
        "preserve_string",
        "preserve_string_with_empty_slots",
        "preserve_sequence",
        "integer_or_string",
        "numeric_or_string",
        "independent_value",
        "preserve_whole_or_semantic_parts",
        "preserve_unit_or_explicit_unit",
        "semantic_identity",
    }
)
_ENTITY_ID = re.compile(r"e[0-9]{3}")
_FACT_ID = re.compile(r"f[0-9]{3}")
_CASE_ID = re.compile(r"[a-z][a-z0-9_-]*(?:\.[a-z][a-z0-9_-]*)+")
_INPUT_PATH = re.compile(r"evals/test_sets/[a-z0-9_.-]+/inputs/[0-9]{3}\.txt")


@dataclass(frozen=True)
class FactsInput:
    """Trusted registry identity and input already loaded by the caller.

    The validator never opens a path supplied by an untrusted facts document.
    Source text is deliberately excluded from this object's representation.
    """

    dataset_id: int
    input_path: str
    text: str = field(repr=False)


def validate_evaluation_facts(
    document: object, inputs: Mapping[str, FactsInput]
) -> dict[str, int]:
    """Validate facts, returning counts only; this does not judge their semantics.

    Inputs must come from the registry, independently of the facts document.
    Every trusted case must occur exactly once. Coordinates count Unicode code
    points, never UTF-8 bytes. Newline characters are excluded from columns.
    """

    def require(condition: object) -> None:
        if not condition:
            raise HarnessError("invalid evaluation facts structure or reference")

    def keys(node: object, expected: set[str] | frozenset[str]) -> None:
        require(type(node) is dict and set(node) == expected)

    def enum(value: object, choices: frozenset[str]) -> None:
        require(type(value) is str and value in choices)

    keys(document, {"facts_version", "coordinate_system", "cases"})
    require(type(document["facts_version"]) is int and document["facts_version"] == 1)
    require(document["coordinate_system"] == _COORDINATES)
    cases = document["cases"]
    require(type(cases) is list and 1 <= len(cases) <= 64)
    require(len(cases) == len(inputs))
    seen_cases: set[str] = set()
    seen_datasets: set[int] = set()
    totals = {
        "facts_version": 1,
        "cases": 0,
        "entities": 0,
        "facts": 0,
        "references": 0,
    }
    for case in cases:
        keys(case, {"dataset_id", "case_id", "input_path", "entities", "facts"})
        cid = case["case_id"]
        require(type(cid) is str and len(cid) <= 240 and _CASE_ID.fullmatch(cid))
        require(cid in inputs and cid not in seen_cases)
        trusted = inputs[cid]
        require(isinstance(trusted, FactsInput) and type(trusted.text) is str)
        require(type(case["dataset_id"]) is int and case["dataset_id"] > 0)
        require(case["dataset_id"] == trusted.dataset_id)
        require(case["dataset_id"] not in seen_datasets)
        require(
            type(case["input_path"]) is str
            and _INPUT_PATH.fullmatch(case["input_path"])
            and case["input_path"] == trusted.input_path
            and case["input_path"].split("/")[2] == cid
        )
        seen_cases.add(cid)
        seen_datasets.add(case["dataset_id"])
        lines = trusted.text.splitlines()
        require(lines and trusted.text.strip())

        def source(
            node: dict, lines: list[str] = lines
        ) -> tuple[tuple[int, int], tuple[int, int]]:
            for key in _SOURCE_KEYS:
                require(type(node[key]) is int)
            start = (node["line_start"], node["column_start"])
            end = (node["line_end"], node["column_end"])
            require(1 <= start[0] <= end[0] <= len(lines))
            require(0 <= start[1] <= len(lines[start[0] - 1]))
            require(0 <= end[1] <= len(lines[end[0] - 1]))
            require(start <= end)
            return start, end

        def content(
            bounds: tuple[tuple[int, int], tuple[int, int]], lines: list[str] = lines
        ) -> str:
            start, end = bounds
            if start[0] == end[0]:
                return lines[start[0] - 1][start[1] : end[1]]
            parts = [lines[start[0] - 1][start[1] :]]
            parts.extend(lines[start[0] : end[0] - 1])
            parts.append(lines[end[0] - 1][: end[1]])
            return "\n".join(parts)

        entities = case["entities"]
        require(type(entities) is list and 1 <= len(entities) <= 1000)
        by_entity = {}
        bounds_by_entity = {}
        for entity in entities:
            keys(entity, {"entity_id", "parent_id", "kind", "source"})
            eid = entity["entity_id"]
            require(type(eid) is str and _ENTITY_ID.fullmatch(eid))
            require(eid not in by_entity)
            enum(entity["kind"], _KINDS)
            keys(entity["source"], _SOURCE_KEYS)
            bounds = source(entity["source"])
            require(bounds[0] < bounds[1] and content(bounds).strip())
            parent = entity["parent_id"]
            if entity["kind"] == "root":
                require(not by_entity and eid == "e000" and parent is None)
                require(bounds == ((1, 0), (len(lines), len(lines[-1]))))
            else:
                # Parents must precede children: this also excludes all cycles.
                require(type(parent) is str and parent in by_entity)
                outer = bounds_by_entity[parent]
                require(outer[0] <= bounds[0] and bounds[1] <= outer[1])
            by_entity[eid] = entity
            bounds_by_entity[eid] = bounds
        require(entities[0]["kind"] == "root")
        facts = case["facts"]
        require(type(facts) is list and 1 <= len(facts) <= 999)
        seen_facts: set[str] = set()
        case_references = 0
        for fact in facts:
            keys(
                fact,
                {
                    "fact_id",
                    "category",
                    "presence",
                    "representation",
                    "mandatory",
                    "sources",
                },
            )
            fid = fact["fact_id"]
            require(type(fid) is str and _FACT_ID.fullmatch(fid) and fid != "f000")
            require(fid not in seen_facts)
            seen_facts.add(fid)
            enum(fact["category"], _CATEGORIES)
            enum(fact["representation"], _REPRESENTATIONS)
            enum(
                fact["presence"],
                frozenset({"present", "empty_slot", "present_with_empty_slots"}),
            )
            require(type(fact["mandatory"]) is bool)
            refs = fact["sources"]
            require(type(refs) is list and 1 <= len(refs) <= 2048)
            case_references += len(refs)
            require(case_references <= 16384)
            roles: set[str] = set()
            seen_refs = set()
            for ref in refs:
                keys(ref, _SOURCE_KEYS | {"entity_id", "role"})
                require(type(ref["entity_id"]) is str and ref["entity_id"] in by_entity)
                enum(ref["role"], _ROLES)
                bounds = source(ref)
                outer = bounds_by_entity[ref["entity_id"]]
                require(outer[0] <= bounds[0] and bounds[1] <= outer[1])
                identity = (ref["entity_id"], ref["role"], bounds)
                require(identity not in seen_refs)
                seen_refs.add(identity)
                roles.add(ref["role"])
                excerpt = content(bounds)
                if ref["role"] == "empty_slot":
                    require(bounds[0][0] == bounds[1][0] and not excerpt.strip())
                    require(lines[bounds[0][0] - 1].strip())
                else:
                    require(excerpt.strip())
            require("value" in roles or "empty_slot" in roles)
            if fact["presence"] == "present":
                require("value" in roles and "empty_slot" not in roles)
            elif fact["presence"] == "empty_slot":
                require("empty_slot" in roles and "value" not in roles)
            else:
                require("empty_slot" in roles and "value" in roles)
            if "empty_slot" in roles:
                require("label" in roles or "qualifier" in roles)
        totals["cases"] += 1
        totals["entities"] += len(entities)
        totals["facts"] += len(facts)
        totals["references"] += case_references
    return totals
