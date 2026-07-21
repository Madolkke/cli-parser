"""Bounded structured feedback for records captured by a TTP candidate."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from typing import Any, Literal, TypedDict

MAX_CAPTURE_BYTES = 32 * 1024
MAX_SCALAR_VALUE_CHARS = 512
_MAX_PREVIEW_ITEMS_PER_KIND = 256


class ContainerPreview(TypedDict):
    """Size summary for one JSON container at a JSON Pointer path."""

    path: str
    type: Literal["object", "array"]
    size: int


class ScalarPreview(TypedDict):
    """One bounded scalar captured at a JSON Pointer path."""

    path: str
    value: Any
    value_truncated: bool


class OutputPreview(TypedDict):
    """Structured head/tail sample for one command output."""

    output_index: int
    containers: list[ContainerPreview]
    scalars: list[ScalarPreview]


class ParseCapture(TypedDict):
    """JSON-ready parsed-result feedback returned to the generation agent."""

    available: bool
    complete: bool
    serialized_bytes: int
    records: list[Any]
    previews: list[OutputPreview]


def _compact_json_bytes(value: Any, *, ensure_ascii: bool) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=ensure_ascii,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _pointer_segment(value: str | int) -> str:
    return str(value).replace("~", "~0").replace("/", "~1")


def _child_pointer(path: str, segment: str | int) -> str:
    return f"{path}/{_pointer_segment(segment)}"


def _sample_text(value: str, max_chars: int) -> tuple[str, bool]:
    if len(value) <= max_chars:
        return value, False
    head_chars = max_chars * 3 // 4
    return value[:head_chars] + value[-(max_chars - head_chars) :], True


def _preview_scalar(value: Any) -> tuple[Any, bool]:
    if isinstance(value, str):
        return _sample_text(value, MAX_SCALAR_VALUE_CHARS)

    encoded = json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        allow_nan=False,
    )
    sampled, truncated = _sample_text(encoded, MAX_SCALAR_VALUE_CHARS)
    return (sampled if truncated else value), truncated


def _collect_preview(value: Any) -> tuple[list[ContainerPreview], list[ScalarPreview]]:
    containers: list[ContainerPreview] = []
    scalars: list[ScalarPreview] = []
    stack: list[tuple[str, Any]] = [("", value)]

    while stack:
        path, current = stack.pop()
        if isinstance(current, Mapping):
            containers.append(
                {"path": path, "type": "object", "size": len(current)},
            )
            children = list(current.items())
            for key, child in reversed(children):
                stack.append((_child_pointer(path, str(key)), child))
            continue
        if isinstance(current, list):
            containers.append(
                {"path": path, "type": "array", "size": len(current)},
            )
            for index in range(len(current) - 1, -1, -1):
                stack.append((_child_pointer(path, index), current[index]))
            continue

        preview_value, truncated = _preview_scalar(current)
        scalars.append(
            {
                "path": path,
                "value": preview_value,
                "value_truncated": truncated,
            },
        )

    return containers, scalars


def _head_tail(items: Sequence[Any], limit: int) -> list[Any]:
    if limit <= 0:
        return []
    if len(items) <= limit:
        return list(items)

    head_count = max(1, limit * 3 // 4)
    tail_count = limit - head_count
    if tail_count == 0:
        return list(items[:head_count])
    return [*items[:head_count], *items[-tail_count:]]


def _capture_size(capture: ParseCapture) -> int:
    return len(_compact_json_bytes(capture, ensure_ascii=True))


def build_parse_capture(records: Sequence[Any]) -> ParseCapture:
    """Build complete or bounded structured feedback for parsed records.

    An empty sequence means parsing did not produce an index-mapped result. A
    non-empty sequence remains available even when individual records are empty
    objects or fail later acceptance checks.
    """

    if not records:
        return {
            "available": False,
            "complete": False,
            "serialized_bytes": 0,
            "records": [],
            "previews": [],
        }

    record_list = list(records)
    serialized_bytes = len(_compact_json_bytes(record_list, ensure_ascii=True))
    complete: ParseCapture = {
        "available": True,
        "complete": True,
        "serialized_bytes": serialized_bytes,
        "records": record_list,
        "previews": [],
    }
    if _capture_size(complete) <= MAX_CAPTURE_BYTES:
        return complete

    collected = [_collect_preview(record) for record in record_list]
    max_items = max(
        (
            max(len(containers), len(scalars))
            for containers, scalars in collected
        ),
        default=0,
    )
    item_limit = min(max_items, _MAX_PREVIEW_ITEMS_PER_KIND)

    while True:
        previews: list[OutputPreview] = []
        for output_index, (containers, scalars) in enumerate(collected):
            previews.append(
                {
                    "output_index": output_index,
                    "containers": _head_tail(containers, item_limit),
                    "scalars": _head_tail(scalars, item_limit),
                },
            )
        capture: ParseCapture = {
            "available": True,
            "complete": False,
            "serialized_bytes": serialized_bytes,
            "records": [],
            "previews": previews,
        }
        if _capture_size(capture) <= MAX_CAPTURE_BYTES:
            return capture
        if item_limit == 0:
            raise AssertionError("minimal parse capture exceeds its hard limit")
        item_limit //= 2


__all__ = [
    "MAX_CAPTURE_BYTES",
    "MAX_SCALAR_VALUE_CHARS",
    "ParseCapture",
    "build_parse_capture",
]
