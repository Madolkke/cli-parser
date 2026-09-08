"""Synthetic regressions for TTP text capture and regex composition."""

from __future__ import annotations

from typing import Any

import pytest

from cli_parser_agent.ttp_generation.validation import (
    inspect_ttp_template,
    parse_ttp_template,
    validate_ttp_template,
)

_ENTRY_TEXT = (
    "Entry alpha\nNarrative:\n  first line\n  second line\n"
    "Status: ready\nExtra:\n  Flag: present\n"
    "Entry beta\nNarrative:\n  third line\nStatus: idle\n"
)
_BROAD_CAPTURE = r'{{ notes | re(".+") | joinmatches("\n") }}'


def _entry_template(capture: str) -> str:
    return (
        '<group name="entries*">\n'
        "Entry {{ name }}\n"
        "Narrative:\n" + capture + "\nStatus: {{ status }}\n"
        '<group name="optional">\nExtra:\n  Flag: {{ flag }}\n</group>\n'
        "</group>"
    )


def _entry_schema() -> dict[str, Any]:
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "type": "object",
        "properties": {
            "entries": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "name": {"type": "string"},
                        "notes": {"type": "string"},
                        "status": {"type": "string"},
                        "optional": {
                            "type": "object",
                            "properties": {"flag": {"type": "string"}},
                            "required": ["flag"],
                            "additionalProperties": False,
                        },
                    },
                    "required": ["name", "notes", "status"],
                    "additionalProperties": False,
                },
            },
        },
        "required": ["entries"],
        "additionalProperties": False,
    }


def _parse(template: str, text: str) -> Any:
    assert inspect_ttp_template(template) == []
    outcome = parse_ttp_template(template, text, timeout_seconds=5)
    assert outcome.valid
    assert outcome.issues == []
    return outcome.result


def test_broad_joinmatches_claims_later_status_lines_before_specific_pattern() -> None:
    outcome = validate_ttp_template(
        _entry_template(_BROAD_CAPTURE),
        [_ENTRY_TEXT],
        _entry_schema(),
        timeout_seconds=5,
    )

    assert outcome.records == [
        {
            "entries": [
                {
                    "name": "alpha",
                    "notes": (
                        "Narrative:\n  first line\n  second line\nStatus: ready\nExtra:"
                    ),
                    "optional": {"flag": "present"},
                },
                {
                    "name": "beta",
                    "notes": "Narrative:\n  third line\nStatus: idle",
                },
            ],
        },
    ]
    assert not outcome.valid
    assert {issue.code for issue in outcome.issues} == {"schema.record_mismatch"}
    assert all(issue.details["keyword"] == "required" for issue in outcome.issues)
    assert all(
        issue.details["missing_required"] == ["status"] for issue in outcome.issues
    )


def test_indentation_excludes_labels_and_preserves_repeated_multiline_notes() -> None:
    # The only template change is the two-space indentation of the capture.
    outcome = validate_ttp_template(
        _entry_template("  " + _BROAD_CAPTURE),
        [_ENTRY_TEXT],
        _entry_schema(),
        timeout_seconds=5,
    )

    assert outcome.valid
    assert outcome.issues == []
    assert outcome.records == [
        {
            "entries": [
                {
                    "name": "alpha",
                    "notes": "first line\nsecond line",
                    "status": "ready",
                    "optional": {"flag": "present"},
                },
                {"name": "beta", "notes": "third line", "status": "idle"},
            ],
        },
    ]


def test_status_match_does_not_end_later_same_indent_notes_capture() -> None:
    text = (
        "Entry alpha\nNarrative:\n  first line\n  second line\n"
        "Status: ready\n  Footer: unrelated\n"
    )
    outcome = validate_ttp_template(
        _entry_template("  " + _BROAD_CAPTURE),
        [text],
        _entry_schema(),
        timeout_seconds=5,
    )

    # Presence/type acceptance cannot detect this semantic overcapture.
    assert outcome.valid
    assert outcome.records == [
        {
            "entries": [
                {
                    "name": "alpha",
                    "notes": "first line\nsecond line\nFooter: unrelated",
                    "status": "ready",
                },
            ],
        },
    ]


@pytest.mark.parametrize(
    ("filters", "expected_values"),
    [
        (
            're("(?!Status:).+") | re("(?!Optional:).+")',
            ["Status: ready", "Optional: present", "free text"],
        ),
        ('re("(?!Status:)(?!Optional:).+")', ["free text"]),
        (
            're(".+") | exclude_re("^Status:") | exclude_re("^Optional:")',
            ["free text"],
        ),
    ],
    ids=["re-alternatives", "single-re-lookaheads", "sequential-predicates"],
)
def test_re_patterns_are_alternatives_while_exclude_filters_are_sequential(
    filters: str,
    expected_values: list[str],
) -> None:
    # TTP joins variable regexes as (?:first)|(?:second); negative patterns
    # therefore do not intersect. Predicate filters operate on each capture.
    template = '<group name="lines*">\n{{ value | ' + filters + " }}\n</group>"
    text = "Status: ready\nOptional: present\nfree text\n"

    assert _parse(template, text) == [
        {"lines": [{"value": value} for value in expected_values]},
    ]


@pytest.mark.parametrize("prefix", ["", "Value: "], ids=["bare", "after-label"])
def test_unflagged_caret_is_not_the_start_of_the_ttp_capture(prefix: str) -> None:
    # TTP's line regex consumes a newline (and any literal label) before
    # inserting re(...); an unflagged ^ at that position cannot match.
    def template(pattern: str) -> str:
        return (
            '<group name="lines*">\n'
            + prefix
            + '{{ value | re("'
            + pattern
            + '") }}\n</group>'
        )

    text = prefix + "alpha\n" + prefix + "beta\n"
    assert _parse(template("^.+"), text) == [{}]
    assert _parse(template(".+"), text) == [
        {"lines": [{"value": "alpha"}, {"value": "beta"}]},
    ]


def _report_template(*, exclude_section_label: bool) -> str:
    extra = ' | exclude_re("^  Details:")' if exclude_section_label else ""
    return (
        '<group name="reports*">\n'
        "Report {{ name }}\n"
        '{{ notes | re(".+") | exclude_re("^Report ") | '
        'exclude_re("^Narrative:") | '
        'exclude_re("^State:")' + extra + r' | joinmatches("\n") }}'
        "\nState: {{ state }}\n"
        '<group name="metadata">\n'
        "Metadata: {{ category }}\n"
        "  Level: {{ level }}\n"
        "</group>\n"
        "</group>"
    )


def _report_schema() -> dict[str, Any]:
    schema = _entry_schema()
    item = schema["properties"].pop("entries")
    schema["properties"]["reports"] = item
    schema["required"] = ["reports"]
    fields = item["items"]["properties"]
    fields["state"] = fields.pop("status")
    fields["metadata"] = {
        "type": "object",
        "properties": {
            "category": {"type": "string"},
            "level": {"type": "string"},
        },
        "required": ["category", "level"],
        "additionalProperties": False,
    }
    fields.pop("optional")
    item["items"]["required"] = ["name", "notes", "state"]
    return schema


@pytest.mark.parametrize("has_section_label", [False, True], ids=["plain", "label"])
@pytest.mark.parametrize(
    "exclude_section_label", [False, True], ids=["broad", "narrow"]
)
def test_parent_match_between_child_fields_changes_later_match_preference(
    has_section_label: bool,
    exclude_section_label: bool,
) -> None:
    section_label = "  Details:\n" if has_section_label else ""
    text = (
        "Report alpha\nNarrative:\n  first detail\n  second detail\n"
        "Metadata: primary\n" + section_label + "  Level: high\nState: ready\n"
        "Report beta\nNarrative:\n  third detail\nState: idle\n"
    )
    outcome = validate_ttp_template(
        _report_template(exclude_section_label=exclude_section_label),
        [text],
        _report_schema(),
        timeout_seconds=5,
    )
    stolen = has_section_label and not exclude_section_label

    # An unmodeled section label can switch the active result back to the
    # parent. At the next line both regexes match, and TTP prefers that parent.
    # Without the intervening label, the same broad pattern leaves child
    # scalars intact. Excluding only that label is the single-factor fix.
    assert outcome.records == [
        {
            "reports": [
                {
                    "name": "alpha",
                    "notes": (
                        "  Details:\n  Level: high"
                        if stolen
                        else "  first detail\n  second detail"
                    ),
                    "metadata": (
                        {"category": "primary"}
                        if stolen
                        else {"category": "primary", "level": "high"}
                    ),
                    "state": "ready",
                },
                {"name": "beta", "notes": "  third detail", "state": "idle"},
            ],
        },
    ]
    assert outcome.valid is not stolen
    if stolen:
        assert len(outcome.issues) == 1
        assert outcome.issues[0].code == "schema.record_mismatch"
        assert outcome.issues[0].details["keyword"] == "required"
        assert outcome.issues[0].details["missing_required"] == ["level"]
    else:
        assert outcome.issues == []
