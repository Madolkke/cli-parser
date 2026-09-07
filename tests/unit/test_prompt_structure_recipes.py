"""Synthetic recipes only; no evaluation inputs or reference templates."""

import re

from cli_parser_agent.ttp_generation.agent.prompt import TTP_SYSTEM_PROMPT
from cli_parser_agent.ttp_generation.validation.ttp import validate_ttp_template


def obj(properties, required=()):
    return {
        "type": "object",
        "properties": properties,
        "required": list(required),
        "additionalProperties": False,
    }


def array(items):
    return {"type": "array", "items": items}


STRING = {"type": "string"}


def _prompt_template(marker: str) -> str:
    blocks = re.findall(r"```xml\n(.*?)\n```", TTP_SYSTEM_PROMPT, re.DOTALL)
    matches = [block for block in blocks if marker in block]
    assert len(matches) == 1
    return matches[0] + "\n"


def test_root_anchor_keeps_children_and_trailing_scalar_with_value_boundary():
    schema = obj(
        {
            "modules": array(obj({"name": STRING, "state": STRING}, ["name", "state"])),
            "advisory": STRING,
        },
        ["modules"],
    )
    inputs = [
        "Inventory overview\nModule m1: ready\nModule m2: idle\n"
        "*** Advisory: Inspect spare module ***\n",
        "Inventory overview\nModule m3: ready\n",
    ]
    result = validate_ttp_template(
        _prompt_template("Inventory overview"), inputs, schema
    )
    assert result.valid, result.issues
    assert result.records == [
        {
            "modules": [
                {"name": "m1", "state": "ready"},
                {"name": "m2", "state": "idle"},
            ],
            "advisory": "Inspect spare module",
        },
        {"modules": [{"name": "m3", "state": "ready"}]},
    ]


def test_multiline_note_excludes_unindented_fields_and_preserves_newline():
    schema = obj(
        {
            "entries": array(
                obj(
                    {"name": STRING, "notes": STRING, "state": STRING},
                    ["name", "state"],
                )
            )
        },
        ["entries"],
    )
    inputs = [
        "Entry: e1\n  first line\n  second line\nStatus: up\n"
        "Entry: e2\nStatus: down\nEntry: e3\n  final note\nStatus: up\n"
    ]
    result = validate_ttp_template(_prompt_template("Entry: {{ name"), inputs, schema)
    assert result.valid, result.issues
    assert result.records == [
        {
            "entries": [
                {"name": "e1", "notes": "first line\nsecond line", "state": "up"},
                {"name": "e2", "state": "down"},
                {"name": "e3", "notes": "final note", "state": "up"},
            ]
        }
    ]


def test_multiline_recipe_does_not_end_notes_at_later_status_pattern():
    schema = obj(
        {
            "entries": array(
                obj(
                    {"name": STRING, "notes": STRING, "state": STRING},
                    ["name", "state"],
                )
            )
        },
        ["entries"],
    )
    source = "Entry: e1\n  first line\nStatus: up\n  Counter detail: 7\n"
    result = validate_ttp_template(_prompt_template("Entry: {{ name"), [source], schema)

    assert result.valid, result.issues
    assert result.records == [
        {
            "entries": [
                {
                    "name": "e1",
                    "state": "up",
                    "notes": "first line\nCounter detail: 7",
                }
            ]
        }
    ]
    # Matching a later XML line does not bound earlier continuation patterns.
    assert "Status 行本身不会终止 notes 捕获" in TTP_SYSTEM_PROMPT
    assert "若后面还有同缩进的非 notes 内容，不能直接使用本例" in TTP_SYSTEM_PROMPT


def test_nested_repeated_section_titles_reset_optional_children():
    schema = obj(
        {
            "units": array(
                obj(
                    {
                        "name": STRING,
                        "counters": array(
                            obj(
                                {
                                    "profile": STRING,
                                    "packets": STRING,
                                    "errors": STRING,
                                },
                                ["profile", "errors"],
                            )
                        ),
                    },
                    ["name", "counters"],
                )
            )
        },
        ["units"],
    )
    inputs = [
        "Unit: u1\nCounters: primary\nErrors: 0\n"
        "Counters: backup\nPackets: 7\nErrors: 1\n"
        "Unit: u2\nCounters: primary\nPackets: 9\nErrors: 2\n"
    ]
    result = validate_ttp_template(_prompt_template("Unit: {{ name"), inputs, schema)
    assert result.valid, result.issues
    assert result.records == [
        {
            "units": [
                {
                    "name": "u1",
                    "counters": [
                        {"profile": "primary", "errors": "0"},
                        {"profile": "backup", "packets": "7", "errors": "1"},
                    ],
                },
                {
                    "name": "u2",
                    "counters": [{"profile": "primary", "packets": "9", "errors": "2"}],
                },
            ]
        }
    ]
