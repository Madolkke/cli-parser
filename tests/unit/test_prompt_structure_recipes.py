"""Synthetic recipes only; no evaluation inputs or reference templates."""

import re
from copy import deepcopy

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


def _entry_schema(required=("name", "state")):
    return obj(
        {
            "entries": array(
                obj({"name": STRING, "notes": STRING, "state": STRING}, required)
            )
        },
        ["entries"],
    )


def test_multiline_note_excludes_unindented_fields_and_preserves_newline():
    schema = _entry_schema()
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


_BUNDLE_SOURCE = (
    "Bundle: copper\nChoices: fast mode\n  ?standby\n  local cache\nState: ready\n"
    "Bundle: silver\nState: idle\n"
    "Bundle: gold\nChoices: read-only\n  wide area\nState: ready\n"
)
_BUNDLE_RECORD = {
    "bundles": [
        {
            "name": "copper",
            "choices": "fast mode ?standby local cache",
            "state": "ready",
        },
        {"name": "silver", "state": "idle"},
        {"name": "gold", "choices": "read-only wide area", "state": "ready"},
    ]
}


def _bundle_schema():
    return obj(
        {
            "bundles": array(
                obj(
                    {"name": STRING, "choices": STRING, "state": STRING},
                    ["name", "state"],
                )
            )
        },
        ["bundles"],
    )


def test_multivalue_recipe_joins_vertical_items_and_keeps_boundaries():
    result = validate_ttp_template(
        _prompt_template("Bundle: {{ name"), [_BUNDLE_SOURCE], _bundle_schema()
    )
    assert result.valid, result.issues
    assert result.records == [_BUNDLE_RECORD]


def test_multivalue_newline_separator_passes_schema_but_changes_representation():
    template = _prompt_template("Bundle: {{ name")
    assert template.count('joinmatches(" ")') == 2
    result = validate_ttp_template(
        template.replace('joinmatches(" ")', 'joinmatches("\\n")'),
        [_BUNDLE_SOURCE],
        _bundle_schema(),
    )
    expected = deepcopy(_BUNDLE_RECORD)
    expected["bundles"][0]["choices"] = "fast mode\n?standby\nlocal cache"
    expected["bundles"][2]["choices"] = "read-only\nwide area"
    assert result.valid, result.issues
    assert result.records == [expected]
    assert result.records != [_BUNDLE_RECORD]


def test_free_text_space_separator_passes_schema_but_loses_line_boundaries():
    template = _prompt_template("Entry: {{ name")
    assert template.count('joinmatches("\\n")') == 1
    schema = _entry_schema(required=())
    result = validate_ttp_template(
        template.replace('joinmatches("\\n")', 'joinmatches(" ")'),
        ["Entry: delta\n  first line\n  second line\nStatus: ready\n"],
        schema,
    )
    assert result.valid, result.issues
    assert result.records == [
        {
            "entries": [
                {"name": "delta", "notes": "first line second line", "state": "ready"}
            ]
        }
    ]
    assert result.records[0]["entries"][0]["notes"] != "first line\nsecond line"


def test_multiline_recipe_does_not_end_notes_at_later_status_pattern():
    schema = _entry_schema()
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


_CONTAINER_SOURCE = """=========
Item: alpha
Features:
  Feature: red
  Feature: blue
Tail: alpha done
=========
Item: beta
Tail: beta done
=========
Item: gamma
Features:
  Feature: green
Tail: gamma done
Total: 3
"""

_CONTAINER_RECORD = {
    "items": [
        {
            "name": "alpha",
            "features": {
                "capabilities": [{"capability": "red"}, {"capability": "blue"}]
            },
            "tail": "alpha done",
        },
        {"name": "beta", "tail": "beta done"},
        {
            "name": "gamma",
            "features": {"capabilities": [{"capability": "green"}]},
            "tail": "gamma done",
        },
    ],
    "total": "3",
}


def _container_schema():
    return obj(
        {
            "items": array(
                obj(
                    {
                        "name": STRING,
                        "tail": STRING,
                        "features": obj(
                            {
                                "capabilities": array(
                                    obj({"capability": STRING}, ["capability"])
                                )
                            },
                            ["capabilities"],
                        ),
                    },
                    ["name", "tail"],
                )
            ),
            "total": STRING,
        },
        ["items", "total"],
    )


def test_container_recipe_keeps_optional_sections_parent_tails_and_root_total():
    result = validate_ttp_template(
        _prompt_template('<group name="features">'),
        [_CONTAINER_SOURCE],
        _container_schema(),
    )

    assert result.valid, result.issues
    assert result.issues == []
    assert result.records == [_CONTAINER_RECORD]


def test_container_recipe_bare_heading_loses_tails_despite_child_results():
    template = _prompt_template('<group name="features">')
    heading = '{{ ignore("Features:") }}'
    assert template.count(heading) == 1
    result = validate_ttp_template(
        template.replace(heading, "Features:"),
        [_CONTAINER_SOURCE],
        _container_schema(),
    )

    expected = deepcopy(_CONTAINER_RECORD)
    del expected["items"][0]["tail"]
    del expected["items"][2]["tail"]
    del expected["total"]
    assert not result.valid
    assert result.records == [expected]
    # Upstream deduplicates identical required issues at wildcard paths.
    assert len(result.issues) == 2
    assert {
        (
            issue.code,
            issue.path,
            issue.details["keyword"],
            tuple(issue.details["missing_required"]),
        )
        for issue in result.issues
    } == {
        ("schema.record_mismatch", "/items/*", "required", ("tail",)),
        ("schema.record_mismatch", "/", "required", ("total",)),
    }


_ASSET_SOURCE = """Asset: cedar
Result: ready ,
Asset: birch
Result: ?pending review ,
Asset: elm
Asset: fir
Result:  ,
Asset: ash
Result: complete ,
"""

_ASSET_RECORD = {
    "assets": [
        {"name": "cedar", "result": "ready"},
        {"name": "birch", "result": "?pending review"},
        {"name": "elm"},
        {"name": "fir", "result": ""},
        {"name": "ash", "result": "complete"},
    ]
}


def _asset_schema():
    return obj(
        {"assets": array(obj({"name": STRING, "result": STRING}, ["name"]))},
        ["assets"],
    )


def test_asset_recipe_distinguishes_status_empty_slot_and_absent_field():
    result = validate_ttp_template(
        _prompt_template('<group name="assets*">'),
        [_ASSET_SOURCE],
        _asset_schema(),
    )

    assert result.valid, result.issues
    assert result.issues == []
    assert result.records == [_ASSET_RECORD]


def test_asset_recipe_excluding_status_passes_schema_but_loses_source_value():
    template = _prompt_template('<group name="assets*">')
    ending = " }} ,"
    assert template.count(ending) == 1
    excluded_template = template.replace(ending, ' | exclude("?pending review") }} ,')
    result = validate_ttp_template(excluded_template, [_ASSET_SOURCE], _asset_schema())

    expected = deepcopy(_ASSET_RECORD)
    del expected["assets"][1]["result"]
    assert result.valid, result.issues
    assert result.issues == []
    assert result.records == [expected]
    assert result.records != [_ASSET_RECORD]


def test_asset_recipe_literal_value_prefix_passes_schema_but_cleans_source_value():
    template = _prompt_template('<group name="assets*">')
    prefix = "Result: {{ result"
    assert template.count(prefix) == 1
    cleaned_template = template.replace(prefix, "Result: ?{{ result")
    source = "Asset: birch\nResult: ?pending review ,\n"
    result = validate_ttp_template(cleaned_template, [source], _asset_schema())

    assert result.valid, result.issues
    assert result.issues == []
    assert result.records == [
        {"assets": [{"name": "birch", "result": "pending review"}]}
    ]
    assert result.records != [
        {"assets": [{"name": "birch", "result": "?pending review"}]}
    ]


def test_container_recipe_full_root_line_cannot_be_shortened():
    template = _prompt_template('<group name="features">')
    result = validate_ttp_template(
        template.replace('{{ ignore("=========") }}', '{{ ignore("===") }}'),
        [_CONTAINER_SOURCE],
        _container_schema(),
    )
    assert not result.valid
    assert result.records == [{}]
    assert len(result.issues) == 1
    assert result.issues[0].path == "/"
    assert result.issues[0].details == {
        "keyword": "required",
        "missing_required": ["items", "total"],
    }


def test_container_recipe_optional_tail_absence_does_not_leak_from_next_item():
    schema = _container_schema()
    schema["required"] = ["items"]
    schema["properties"]["items"]["items"]["required"] = ["name"]
    source = _CONTAINER_SOURCE.replace("Tail: beta done\n", "").replace(
        "Total: 3\n", ""
    )
    expected = deepcopy(_CONTAINER_RECORD)
    del expected["items"][1]["tail"]
    del expected["total"]
    result = validate_ttp_template(
        _prompt_template('<group name="features">'), [source], schema
    )
    assert result.valid, result.issues
    assert result.records == [expected]
