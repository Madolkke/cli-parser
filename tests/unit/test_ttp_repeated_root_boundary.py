"""Independent synthetic root-start evidence; no Trace or evaluation content."""

from copy import deepcopy

import pytest

from cli_parser_agent.ttp_generation.validation import validate_ttp_template

SOURCE = """=========
Item: cedar
Features:
  Feature: amber
Tail: first done
=========
Item: birch
Tail: second done
=========
Item: maple
Features:
  Feature: violet
Tail: third done
Total: 3
"""


def obj(props, req):
    return {
        "type": "object",
        "properties": props,
        "required": req,
        "additionalProperties": False,
    }


STRING = {"type": "string"}
SCHEMA = obj(
    {
        "items": {
            "type": "array",
            "items": obj(
                {
                    "name": STRING,
                    "features": obj(
                        {
                            "capabilities": {
                                "type": "array",
                                "items": obj({"capability": STRING}, ["capability"]),
                            }
                        },
                        ["capabilities"],
                    ),
                    "tail": STRING,
                },
                ["name"],
            ),
        },
        "total": STRING,
    },
    ["items"],
)
TEMPLATE = """<group>
ANCHOR
<group name="items*">
Item: {{ name | WORD }}
<group name="features">
{{ ignore("Features:") }}
<group name="capabilities*">
  Feature: {{ capability | WORD }}
</group>
</group>
Tail: {{ tail | ORPHRASE }}
</group>
Total: {{ total | DIGIT }}
</group>"""

EXPECTED = {
    "items": [
        {
            "name": "cedar",
            "features": {"capabilities": [{"capability": "amber"}]},
            "tail": "first done",
        },
        {"name": "birch", "tail": "second done"},
        {
            "name": "maple",
            "features": {"capabilities": [{"capability": "violet"}]},
            "tail": "third done",
        },
    ],
    "total": "3",
}


@pytest.mark.parametrize("omit", [None, "Tail: second done\n", "Total: 3\n"])
def test_repeated_root_separator_keeps_optional_sections_and_tails(omit):
    source = SOURCE if omit is None else SOURCE.replace(omit, "")
    expected = deepcopy(EXPECTED)
    if omit == "Tail: second done\n":
        del expected["items"][1]["tail"]
    elif omit == "Total: 3\n":
        del expected["total"]
    result = validate_ttp_template(
        TEMPLATE.replace("ANCHOR", '{{ ignore("=========") }}'), [source], SCHEMA
    )
    assert result.valid, result.issues
    assert result.records == [expected]


def test_missing_root_anchor_loses_optional_total_but_passes_schema():
    result = validate_ttp_template(TEMPLATE.replace("ANCHOR", ""), [SOURCE], SCHEMA)
    assert result.valid, result.issues
    expected = deepcopy(EXPECTED)
    del expected["total"]
    assert result.records == [expected]
    assert result.records != [EXPECTED]


def test_partial_root_separator_rejects_required_array():
    result = validate_ttp_template(
        TEMPLATE.replace("ANCHOR", '{{ ignore("===") }}'), [SOURCE], SCHEMA
    )
    assert not result.valid
    assert result.records == [{}]
    assert len(result.issues) == 1
    issue = result.issues[0]
    assert (issue.code, issue.path, issue.details) == (
        "schema.record_mismatch",
        "/",
        {"keyword": "required", "missing_required": ["items"]},
    )


def test_separator_inside_nested_section_is_not_a_safe_root_anchor():
    source = SOURCE.replace("  Feature: amber", "=========\n  Feature: amber")
    result = validate_ttp_template(
        TEMPLATE.replace("ANCHOR", '{{ ignore("=========") }}'), [source], SCHEMA
    )
    assert result.valid, result.issues
    expected = deepcopy(EXPECTED)
    del expected["items"][0]["tail"]
    assert result.records == [expected]
    assert result.records != [EXPECTED]
