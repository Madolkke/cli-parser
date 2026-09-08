"""Synthetic boundaries for parent fields following repeated nested groups."""

from __future__ import annotations

from copy import deepcopy

import pytest

from cli_parser_agent.ttp_generation.validation import validate_ttp_template

_SOURCE = """Inventory
Item: alpha
Features:
  Feature: red
  Feature: blue
Tail: alpha done
Item: beta
Tail: beta done
Item: gamma
Features:
  Feature: green
Tail: gamma done
Total: 3
"""

_EXPECTED = {
    "items": [
        {
            "name": "alpha",
            "features": {
                "capabilities": [{"capability": "red"}, {"capability": "blue"}],
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


def _object(properties: dict, required: list[str]) -> dict:
    return {
        "type": "object",
        "properties": properties,
        "required": required,
        "additionalProperties": False,
    }


def _schema() -> dict:
    feature = _object({"capability": {"type": "string"}}, ["capability"])
    features = _object(
        {"capabilities": {"type": "array", "items": feature}}, ["capabilities"]
    )
    item = _object(
        {
            "name": {"type": "string"},
            "tail": {"type": "string"},
            "features": features,
        },
        ["name", "tail"],
    )
    return _object(
        {"items": {"type": "array", "items": item}, "total": {"type": "string"}},
        ["items", "total"],
    )


def _template(
    *, tail_before_child: bool, matched_heading: bool, tail_pattern: str = "ORPHRASE"
) -> str:
    tail = "Tail: {{ tail | " + tail_pattern + " }}\n"
    heading = '{{ ignore("Features:") }}' if matched_heading else "Features:"
    return (
        '<group>\n{{ ignore("Inventory") }}\nTotal: {{ total | DIGIT }}\n'
        '<group name="items*">\nItem: {{ name | WORD }}\n'
        + (tail if tail_before_child else "")
        + '<group name="features">\n'
        + heading
        + '\n<group name="capabilities*">\n'
        + "  Feature: {{ capability | WORD }}\n</group>\n</group>\n"
        + ("" if tail_before_child else tail)
        + "</group>\n</group>"
    )


@pytest.mark.parametrize("tail_before_child", [False, True])
@pytest.mark.parametrize("matched_heading", [False, True])
def test_nested_heading_controls_parent_tail_capture_independently_of_xml_order(
    tail_before_child: bool, matched_heading: bool
) -> None:
    """Moving a declaration cannot replace a matchable container heading."""
    outcome = validate_ttp_template(
        _template(tail_before_child=tail_before_child, matched_heading=matched_heading),
        [_SOURCE],
        _schema(),
    )

    expected = deepcopy(_EXPECTED)
    if not matched_heading:
        # A bare text heading provides no TTP match. Its repeated child still
        # captures, but the containing features scope consumes later parents.
        del expected["items"][0]["tail"]
        del expected["items"][2]["tail"]
        del expected["total"]
    assert outcome.records == [expected]
    assert outcome.valid is matched_heading
    if matched_heading:
        assert outcome.issues == []
    else:
        # Identical wildcard required diagnostics are deduplicated upstream.
        assert len(outcome.issues) == 2
        assert {
            (issue.code, issue.path, issue.details["keyword"])
            for issue in outcome.issues
        } == {
            ("schema.record_mismatch", "/", "required"),
            ("schema.record_mismatch", "/items/*", "required"),
        }
        assert sorted(
            tuple(issue.details["missing_required"]) for issue in outcome.issues
        ) == [("tail",), ("total",)]


@pytest.mark.parametrize("multiword_tail", [False, True])
def test_word_pattern_mismatch_is_distinct_from_nested_group_scope(
    multiword_tail: bool,
) -> None:
    """A correctly started child does not make WORD capture multiword tails."""
    source = _SOURCE if multiword_tail else _SOURCE.replace(" done", "")
    outcome = validate_ttp_template(
        _template(tail_before_child=False, matched_heading=True, tail_pattern="WORD"),
        [source],
        _schema(),
    )

    expected = deepcopy(_EXPECTED)
    for item in expected["items"]:
        if multiword_tail:
            del item["tail"]
        else:
            item["tail"] = item["tail"].removesuffix(" done")
    assert outcome.records == [expected]
    assert outcome.valid is not multiword_tail
    if multiword_tail:
        assert len(outcome.issues) == 1
        assert all(
            issue.code == "schema.record_mismatch"
            and issue.path == "/items/*"
            and issue.details == {"keyword": "required", "missing_required": ["tail"]}
            for issue in outcome.issues
        )
        # On the identical multiword input, widen only the target pattern.
        control = validate_ttp_template(
            _template(
                tail_before_child=False,
                matched_heading=True,
                tail_pattern="ORPHRASE",
            ),
            [source],
            _schema(),
        )
        assert control.valid
        assert control.issues == []
        assert control.records == [_EXPECTED]
    else:
        assert outcome.issues == []


@pytest.mark.parametrize("branch_layout", ["status_first", "code_first", "separate"])
def test_optional_child_alternatives_need_independent_start_matches(
    branch_layout: str,
) -> None:
    """A later child match line is not another implicit start alternative."""
    source = """Catalog
Entry: alpha
Details absent - pending
Entry: beta
Details:
  Code: blue
Entry: gamma
Total: 3
"""
    status_line = "Details absent - {{ status | WORD }}\n"
    code_lines = "Details:\n  Code: {{ code | WORD }}\n"
    if branch_layout == "separate":
        child = (
            '<group name="details">\n'
            + status_line
            + '</group>\n<group name="details">\n'
            + code_lines
            + "</group>\n"
        )
    else:
        child = (
            '<group name="details">\n'
            + (
                status_line + code_lines
                if branch_layout == "status_first"
                else code_lines + status_line
            )
            + "</group>\n"
        )
    template = (
        '<group>\n{{ ignore("Catalog") }}\nTotal: {{ total | DIGIT }}\n'
        '<group name="entries*">\nEntry: {{ label | WORD }}\n'
        + child
        + "</group>\n</group>"
    )
    details = _object({"status": {"type": "string"}, "code": {"type": "string"}}, [])
    entry = _object({"label": {"type": "string"}, "details": details}, ["label"])
    schema = _object(
        {
            "entries": {"type": "array", "items": entry},
            "total": {"type": "string"},
        },
        ["entries", "total"],
    )

    outcome = validate_ttp_template(template, [source], schema)

    expected = {
        "entries": [
            {"label": "alpha", "details": {"status": "pending"}},
            {"label": "beta", "details": {"code": "blue"}},
            {"label": "gamma"},
        ],
        "total": "3",
    }
    if branch_layout == "status_first":
        del expected["entries"][1]["details"]
    elif branch_layout == "code_first":
        del expected["entries"][0]["details"]
    # Optional container loss can satisfy Schema, so pin the actual records.
    assert outcome.valid
    assert outcome.issues == []
    assert outcome.records == [expected]
