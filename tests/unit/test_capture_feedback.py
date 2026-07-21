from __future__ import annotations

import json

from cli_parser_agent.ttp_generation.validation import (
    MAX_CAPTURE_BYTES,
    MAX_SCALAR_VALUE_CHARS,
    build_parse_capture,
)


def _encoded_size(value: object) -> int:
    return len(
        json.dumps(
            value,
            ensure_ascii=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8"),
    )


def test_empty_records_mean_capture_is_unavailable() -> None:
    assert build_parse_capture([]) == {
        "available": False,
        "complete": False,
        "serialized_bytes": 0,
        "records": [],
        "previews": [],
    }


def test_small_capture_returns_complete_index_ordered_records() -> None:
    records = [{}, {"value": "two"}]

    capture = build_parse_capture(records)

    assert capture == {
        "available": True,
        "complete": True,
        "serialized_bytes": len(b'[{},{"value":"two"}]'),
        "records": records,
        "previews": [],
    }


def test_serialized_bytes_uses_the_actual_ascii_safe_tool_encoding() -> None:
    records = [{"value": "\u754c"}]

    capture = build_parse_capture(records)

    assert capture["serialized_bytes"] == _encoded_size(records)
    assert capture["complete"] is True


def test_oversized_capture_returns_bounded_structured_head_tail_previews() -> None:
    long_value = "A" * 600 + "Z" * 600
    records = [
        {
            "interfaces": [
                {
                    "name/alias~": f"eth{index}",
                    "description": long_value,
                }
                for index in range(2_000)
            ],
        },
        {},
    ]

    capture = build_parse_capture(records)

    assert capture["available"] is True
    assert capture["complete"] is False
    assert capture["serialized_bytes"] > MAX_CAPTURE_BYTES
    assert capture["records"] == []
    assert [preview["output_index"] for preview in capture["previews"]] == [0, 1]
    assert _encoded_size(capture) <= MAX_CAPTURE_BYTES

    first = capture["previews"][0]
    assert first["containers"][:2] == [
        {"path": "", "type": "object", "size": 1},
        {"path": "/interfaces", "type": "array", "size": 2_000},
    ]
    scalar_paths = [item["path"] for item in first["scalars"]]
    assert len(scalar_paths) == 64
    assert "/interfaces/0/name~1alias~0" in scalar_paths
    assert "/interfaces/1999/description" in scalar_paths
    assert scalar_paths[47] == "/interfaces/23/description"
    assert scalar_paths[48] == "/interfaces/1992/name~1alias~0"

    truncated = next(
        item
        for item in first["scalars"]
        if item["path"] == "/interfaces/0/description"
    )
    assert truncated["value_truncated"] is True
    assert len(truncated["value"]) == MAX_SCALAR_VALUE_CHARS
    assert truncated["value"] == "A" * 384 + "Z" * 128

    second = capture["previews"][1]
    assert second == {
        "output_index": 1,
        "containers": [{"path": "", "type": "object", "size": 0}],
        "scalars": [],
    }


def test_ascii_expansion_is_included_in_the_capture_hard_limit() -> None:
    records = [{"values": ["\u754c" * 500 for _ in range(100)]}]

    capture = build_parse_capture(records)

    assert capture["complete"] is False
    assert _encoded_size(capture) <= MAX_CAPTURE_BYTES


def test_complete_capture_switches_to_preview_at_the_exact_wrapper_boundary() -> None:
    low = 0
    high = MAX_CAPTURE_BYTES
    while low + 1 < high:
        middle = (low + high) // 2
        if build_parse_capture([{"value": "x" * middle}])["complete"]:
            low = middle
        else:
            high = middle

    complete = build_parse_capture([{"value": "x" * low}])
    preview = build_parse_capture([{"value": "x" * high}])

    assert complete["complete"] is True
    assert _encoded_size(complete) <= MAX_CAPTURE_BYTES
    assert preview["complete"] is False
    assert _encoded_size(preview) <= MAX_CAPTURE_BYTES
