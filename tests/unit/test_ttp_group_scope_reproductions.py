"""Synthetic group-scope reproductions using the isolated TTP validator."""

from __future__ import annotations

from typing import Any

from cli_parser_agent.ttp_generation.validation import validate_ttp_template


def _object(properties: dict[str, Any], required: list[str]) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": properties,
        "required": required,
        "additionalProperties": False,
    }


def _schema() -> dict[str, Any]:
    text = {"type": "string"}
    return _object(
        {
            "crates": {
                "type": "array",
                "items": _object(
                    {
                        "label": text,
                        "weight": text,
                        "audit": _object({"code": text}, ["code"]),
                    },
                    ["label", "weight"],
                ),
            },
            "total": text,
        },
        ["crates"],
    )


_SOURCES = [
    """==== Storage register ====
Crate: cedar
Weight: 17
Audit: amber
Crate: birch
Weight: 29
Crate: maple
Weight: 43
Audit: violet
Trailer: 89
""",
    """==== Storage register ====
Crate: elm
Weight: 61
Crate: willow
Weight: 73
""",
]

_EXPECTED = [
    {
        "crates": [
            {"label": "cedar", "weight": "17", "audit": {"code": "amber"}},
            {"label": "birch", "weight": "29"},
            {"label": "maple", "weight": "43", "audit": {"code": "violet"}},
        ],
        "total": "89",
    },
    {
        "crates": [
            {"label": "elm", "weight": "61"},
            {"label": "willow", "weight": "73"},
        ]
    },
]

_FULL_ANCHOR = '{{ ignore("==== Storage register ====") }}\n'
_TRAILER = "Trailer: {{ total | DIGIT }}\n"


def _template(
    anchor: str, *, trailer_first: bool = False, fragmented: bool = False
) -> str:
    scalar_lines = ["Crate: {{ label | WORD }}", "Weight: {{ weight | DIGIT }}"]
    if fragmented:
        scalar_lines = [f"<group>\n{line}\n</group>" for line in scalar_lines]
    children = (
        '<group name="crates*">\n'
        + "\n".join(scalar_lines)
        + '\n<group name="audit">\nAudit: {{ code | WORD }}\n</group>\n'
        + "</group>\n"
    )
    body = _TRAILER + children if trailer_first else children + _TRAILER
    return "<group>\n" + anchor + body + "</group>"


def test_tail_first_root_pattern_loses_children_until_early_anchor_is_added() -> None:
    """A trailer cannot start the root before its children have already passed."""

    unanchored = validate_ttp_template(
        _template("", trailer_first=True), _SOURCES, _schema()
    )
    anchored = validate_ttp_template(
        _template(_FULL_ANCHOR, trailer_first=True), _SOURCES, _schema()
    )

    assert unanchored.records == [{"total": "89"}, {}]
    assert not unanchored.valid
    assert [issue.output_index for issue in unanchored.issues] == [0, 1]
    assert all(
        issue.code == "schema.record_mismatch"
        and issue.path == "/"
        and issue.details == {"keyword": "required", "missing_required": ["crates"]}
        for issue in unanchored.issues
    )
    assert anchored.valid, anchored.issues
    assert anchored.records == _EXPECTED


def test_child_first_unanchored_root_can_lose_only_the_optional_trailer() -> None:
    """No anchor does not always lose children; XML pattern order matters."""

    unanchored = validate_ttp_template(_template(""), _SOURCES, _schema())
    anchored = validate_ttp_template(_template(_FULL_ANCHOR), _SOURCES, _schema())

    assert unanchored.valid, unanchored.issues
    assert unanchored.records == [{"crates": record["crates"]} for record in _EXPECTED]
    assert "total" not in unanchored.records[0]
    # The optional Schema field cannot expose that the source trailer was lost.
    assert anchored.valid, anchored.issues
    assert anchored.records == _EXPECTED


def test_partial_decorated_title_does_not_start_the_root() -> None:
    """Only completing the title literal changes the failing template."""

    partial = validate_ttp_template(
        _template('{{ ignore("==== Storage register") }}\n'), _SOURCES, _schema()
    )
    complete = validate_ttp_template(_template(_FULL_ANCHOR), _SOURCES, _schema())

    assert not partial.valid
    assert partial.records == [{}, {}]
    assert [issue.output_index for issue in partial.issues] == [0, 1]
    assert all(
        issue.code == "schema.record_mismatch"
        and issue.path == "/"
        and issue.details == {"keyword": "required", "missing_required": ["crates"]}
        for issue in partial.issues
    )
    assert complete.valid, complete.issues
    assert complete.records == _EXPECTED


def test_unnamed_scalar_subgroups_fragment_repeated_entities() -> None:
    """Removing per-scalar wrappers restores the same repeated parent fields."""

    fragmented = validate_ttp_template(
        _template(_FULL_ANCHOR, fragmented=True), _SOURCES, _schema()
    )
    shared_parent = validate_ttp_template(_template(_FULL_ANCHOR), _SOURCES, _schema())

    assert not fragmented.valid
    assert fragmented.records == [
        {
            "crates": [
                {"label": "cedar"},
                {"weight": "17", "audit": {"code": "amber"}},
                {"label": "birch"},
                {"weight": "29"},
                {"label": "maple"},
                {"weight": "43", "audit": {"code": "violet"}},
            ]
        },
        {
            "crates": [
                {"label": "elm"},
                {"weight": "61"},
                {"label": "willow"},
                {"weight": "73"},
            ]
        },
    ]
    assert {
        (issue.output_index, issue.path, tuple(issue.details["missing_required"]))
        for issue in fragmented.issues
    } == {
        (0, "/crates/*", ("label",)),
        (0, "/crates/*", ("weight",)),
        (1, "/crates/*", ("label",)),
        (1, "/crates/*", ("weight",)),
    }
    assert all(issue.code == "schema.record_mismatch" for issue in fragmented.issues)
    assert shared_parent.valid, shared_parent.issues
    assert shared_parent.records == _EXPECTED
