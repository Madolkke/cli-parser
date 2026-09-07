"""Private, value-free field presence facts for a frozen record Schema."""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from ..validation.json_schema import (
    MAX_FIELD_NAME_CHARS,
    MAX_ISSUE_PATH_CHARS,
    MAX_SCHEMA_DEPTH,
    MAX_SCHEMA_PROPERTIES,
)

MAX_COVERAGE_PATHS = 24
_FIELD_NAME = re.compile(r"[a-z][a-z0-9]*(?:_[a-z0-9]+)*")
_SCALAR_TYPES = {"string", "integer", "number", "boolean", "null"}


@dataclass(frozen=True, slots=True)
class _PresenceNode:
    kind: str
    path: str
    required: frozenset[str] = frozenset()
    properties: tuple[tuple[str, _PresenceNode], ...] = ()
    items: _PresenceNode | None = None


def _safe_name(value: Any) -> bool:
    return (
        isinstance(value, str)
        and len(value) <= MAX_FIELD_NAME_CHARS
        and _FIELD_NAME.fullmatch(value) is not None
    )


def _presence_tree(schema: Mapping[str, Any]) -> _PresenceNode | None:
    property_count = 0

    def build(
        node: Any, path: str, depth: int, ancestors: frozenset[int]
    ) -> _PresenceNode:
        nonlocal property_count
        if (
            not isinstance(node, Mapping)
            or id(node) in ancestors
            or depth > MAX_SCHEMA_DEPTH
            or len(path) > MAX_ISSUE_PATH_CHARS
        ):
            raise ValueError("invalid presence schema")
        ancestors = ancestors | {id(node)}
        kind = node.get("type")
        if kind == "object":
            properties = node.get("properties")
            required = node.get("required", [])
            if not isinstance(properties, Mapping) or not isinstance(required, list):
                raise ValueError("invalid presence object")
            property_count += len(properties)
            if property_count > MAX_SCHEMA_PROPERTIES or not all(
                isinstance(name, str) for name in required
            ):
                raise ValueError("invalid presence properties")
            children = []
            for name, child in properties.items():
                if not _safe_name(name):
                    raise ValueError("invalid presence property")
                children.append(
                    (name, build(child, path + "/" + name, depth + 1, ancestors))
                )
            return _PresenceNode(kind, path, frozenset(required), tuple(children))
        if kind == "array":
            return _PresenceNode(
                kind,
                path,
                items=build(node.get("items"), path + "/*", depth + 1, ancestors),
            )
        if isinstance(kind, str) and kind in _SCALAR_TYPES:
            return _PresenceNode(kind, path)
        raise ValueError("invalid presence type")

    # Production input is frozen and restricted; this guard also bounds test doubles.
    try:
        return build(schema, "", 0, frozenset())
    except ValueError:
        return None


def _input_facts(
    tree: _PresenceNode, record: Any, input_index: int
) -> tuple[bool, list[tuple[str, dict[str, Any]]]]:
    complete = isinstance(record, dict)
    counts: dict[str, list[int]] = {}

    def visit(node: _PresenceNode, value: Any) -> None:
        nonlocal complete
        if node.kind == "array" and isinstance(value, list) and node.items is not None:
            for item in value:
                visit(node.items, item)
        elif node.kind == "object" and isinstance(value, dict):
            # Required names need not be declared in properties under JSON Schema.
            complete &= node.required.issubset(value)
            for name, child in node.properties:
                present = name in value
                if name not in node.required:
                    count = counts.setdefault(child.path, [0, 0])
                    count[0] += 1
                    count[1] += present
                # An absent or wrongly typed parent does not imply missing children.
                if present:
                    visit(child, value[name])

    visit(tree, record)
    facts = [
        (
            "optional_paths_absent" if present == 0 else "optional_paths_partial",
            {
                "input_index": input_index,
                "path": path,
                "parent_occurrences": parents,
                "present_occurrences": present,
            },
        )
        for path, (parents, present) in counts.items()
        if present < parents
    ]
    return bool(complete), facts


def build_record_coverage(
    *,
    schema: Mapping[str, Any] | None,
    records: Sequence[Any] | None,
    input_count: int,
) -> tuple[dict[str, Any] | None, list[tuple[str, dict[str, Any]]]]:
    """Reserve a fixed envelope; feedback applies the shared item and byte limits."""
    if records is None or schema is None:
        return None, []
    tree = _presence_tree(schema)
    if tree is None or tree.kind != "object":
        return None, []
    complete = len(records) == input_count
    by_input = []
    for input_index, record in enumerate(records[:input_count]):
        input_complete, facts = _input_facts(tree, record, input_index)
        complete &= input_complete
        by_input.append(facts)
    # Interleave inputs before truncation so a large first record cannot dominate.
    facts = [
        items[index]
        for index in range(max(map(len, by_input), default=0))
        for items in by_input
        if index < len(items)
    ]
    return {
        "required_paths_complete": bool(complete),
        "optional_paths_absent": [],
        "optional_paths_partial": [],
        "optional_paths_total": len(facts),
        "optional_paths_omitted": len(facts),
    }, facts
