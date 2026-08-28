from __future__ import annotations

import asyncio
import keyword
import time
from pathlib import Path

import pytest

from cli_parser_agent.ttp_generation.validation import (
    inspect_ttp_template,
    validate_records_against_schema,
    validate_ttp_template,
)

_PYTHON_SNAKE_CASE_KEYWORDS = tuple(
    field_name for field_name in keyword.kwlist if field_name.islower()
)


def _table_schema() -> dict:
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "type": "object",
        "properties": {
            "interfaces": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "name": {"type": "string"},
                        "status": {"type": "string"},
                    },
                    "required": ["name", "status"],
                    "additionalProperties": False,
                },
            },
        },
        "required": ["interfaces"],
        "additionalProperties": False,
    }


def _line_schema(field: str = "value", field_type: str = "string") -> dict:
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "type": "object",
        "properties": {field: {"type": field_type}},
        "required": [field],
        "additionalProperties": False,
    }


def _optional_line_schema(
    field: str = "value",
    field_type: str = "string",
) -> dict:
    schema = _line_schema(field, field_type)
    schema["required"] = []
    return schema


def _array_schema(container: str, field: str) -> dict:
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "type": "object",
        "properties": {
            container: {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {field: {"type": "string"}},
                    "required": [field],
                    "additionalProperties": False,
                },
            },
        },
        "required": [container],
        "additionalProperties": False,
    }


def _codes(issues) -> set[str]:
    return {issue.code for issue in issues}


@pytest.mark.parametrize(
    "tag",
    [
        "macro",
        "vars",
        "input",
        "output",
        "lookup",
        "extend",
    ],
)
def test_static_inspection_rejects_side_effect_capable_tags(tag: str) -> None:
    template = f"<template><{tag}>payload</{tag}></template>"

    issues = inspect_ttp_template(template)

    assert "ttp.forbidden_tag" in _codes(issues)
    assert issues[0].details == {"tag": tag}


@pytest.mark.parametrize(
    "attribute",
    [
        "re(__import__('os').system('whoami'))",
        "re(os.environ['SECRET'])",
        "re('x' + 'y')",
        "re([x for x in values])",
        "macro('danger')",
        "lookup('file')",
        "dns('example.com')",
        "geoip('127.0.0.1')",
        "set('constant')",
        "default('constant')",
    ],
)
def test_get_attributes_eval_attacks_and_unsafe_functions_are_rejected(
    attribute: str,
) -> None:
    template = f"{{{{ value | {attribute} }}}}"

    assert _codes(inspect_ttp_template(template)) == {
        "ttp.unsafe_variable_attribute",
    }


def test_unsafe_attribute_feedback_names_only_the_parsed_function() -> None:
    issues = inspect_ttp_template("{{ value | column(0) }}")

    assert len(issues) == 1
    assert issues[0].code == "ttp.unsafe_variable_attribute"
    assert issues[0].details == {"attribute": "column"}


@pytest.mark.parametrize("index", [-10_000, -1, 0, 10_000])
def test_item_allows_bounded_integer_indexes(index: int) -> None:
    assert inspect_ttp_template(f"{{{{ value | item({index}) }}}}") == []


def test_negative_item_index_is_applied_by_ttp() -> None:
    template = "Value: {{ value | ORPHRASE | item(-1) }}"

    result = validate_ttp_template(template, ["Value: alpha"], _line_schema())

    assert result.valid
    assert result.records == [{"value": "a"}]


@pytest.mark.parametrize(
    "argument",
    [
        "+1",
        "~1",
        "-(-1)",
        "-(1 + 1)",
        "1 - 2",
        "-1.0",
        "-True",
        "-value",
        "-10001",
        "10001",
    ],
)
def test_item_rejects_unsafe_or_out_of_range_indexes(argument: str) -> None:
    issues = inspect_ttp_template(f"{{{{ value | item({argument}) }}}}")

    assert _codes(issues) == {"ttp.unsafe_variable_attribute"}


@pytest.mark.parametrize("control", ["_exact_", "_exact_space_"])
def test_exact_line_controls_must_modify_a_schema_field(control: str) -> None:
    issues = inspect_ttp_template(f"{{{{ {control} }}}}")

    assert len(issues) == 1
    assert issues[0].code == "ttp.invalid_line_control"
    assert issues[0].details == {
        "control": control,
        "required_action": "attach_to_schema_field",
    }
    assert inspect_ttp_template(f"Value: {{{{ value | WORD | {control} }}}}") == []


def test_exact_line_control_attached_to_field_is_applied_by_ttp() -> None:
    result = validate_ttp_template(
        "Value: {{ value | WORD | _exact_ }}",
        ["Value: alpha"],
        _line_schema(),
    )

    assert result.valid
    assert result.records == [{"value": "alpha"}]


def test_group_attributes_are_closed_and_eval_safe() -> None:
    unsafe_filter = (
        '<group name="items*" '
        "contains=\"__import__('os').system('whoami')\">"
        "{{ value | WORD }}</group>"
    )
    external_macro = '<group name="items*" macro="danger">{{ value | WORD }}</group>'

    assert "ttp.unsafe_group_attribute" in _codes(
        inspect_ttp_template(unsafe_filter),
    )
    assert "ttp.forbidden_group_attribute" in _codes(
        inspect_ttp_template(external_macro),
    )

    long_attribute = "a" * 2_500
    issue = inspect_ttp_template(
        f'<group name="items*" {long_attribute}="x">{{{{ value | WORD }}}}</group>',
    )[0]
    assert issue.code == "ttp.forbidden_group_attribute"
    assert issue.path is not None and len(issue.path) <= 2_048


def test_xml_entities_and_external_declarations_are_rejected() -> None:
    template = (
        '<!DOCTYPE template [<!ENTITY xxe SYSTEM "file:///etc/passwd">]>'
        "<template>{{ value | WORD }}</template>"
    )

    assert _codes(inspect_ttp_template(template)) == {
        "ttp.forbidden_xml_declaration",
    }


def test_invalid_xml_reports_position_and_escape_action() -> None:
    issues = inspect_ttp_template(
        '<group name="items*">Flags: <{{ flags | PHRASE }}></group>',
    )

    assert len(issues) == 1
    assert issues[0].code == "ttp.invalid_xml"
    assert issues[0].details["required_action"] == "escape_xml_metacharacters"
    assert isinstance(issues[0].details["line"], int)
    assert isinstance(issues[0].details["column"], int)


def test_duplicate_variable_on_one_line_is_rejected_before_ttp() -> None:
    issues = inspect_ttp_template("{{ _line_ }} text {{ _line_ }}")

    duplicate = next(
        issue for issue in issues if issue.code == "ttp.duplicate_line_variable"
    )
    assert duplicate.details == {"variable": "_line_"}


@pytest.mark.parametrize("field_name", _PYTHON_SNAKE_CASE_KEYWORDS)
def test_python_keywords_are_valid_ttp_result_fields(field_name: str) -> None:
    assert inspect_ttp_template(f"{{{{ {field_name} | WORD }}}}") == []


def test_python_keyword_field_is_parsed_without_renaming() -> None:
    result = validate_ttp_template(
        "Value: {{ as | WORD }}",
        ["Value: alpha"],
        _line_schema("as"),
    )

    assert result.valid
    assert result.records == [{"as": "alpha"}]


def test_python_keyword_field_is_preserved_in_nested_group() -> None:
    result = validate_ttp_template(
        '<group name="details">Value: {{ class | WORD }}</group>',
        ["Value: router"],
        {
            "type": "object",
            "properties": {
                "details": {
                    "type": "object",
                    "properties": {"class": {"type": "string"}},
                    "required": ["class"],
                    "additionalProperties": False,
                },
            },
            "required": ["details"],
            "additionalProperties": False,
        },
    )

    assert result.valid
    assert result.records == [{"details": {"class": "router"}}]


@pytest.mark.parametrize(
    "expression",
    [
        "value.attribute",
        "value[0]",
        "value + other",
        "value()",
        "__import__('os')",
    ],
)
def test_non_bare_variable_heads_remain_rejected(expression: str) -> None:
    issues = inspect_ttp_template(f"{{{{ {expression} | WORD }}}}")

    assert _codes(issues) == {"ttp.invalid_field_name"}


def test_supported_ignore_forms_may_repeat_without_creating_result_fields() -> None:
    template = (
        r'{{ ignore }} {{ ignore(WORD) }} {{ value | WORD }} {{ ignore(r"\S+") }}'
    )

    result = validate_ttp_template(template, ["a b value c"], _line_schema())

    assert result.valid
    assert result.records == [{"value": "value"}]


@pytest.mark.parametrize(
    "expression",
    [
        "ignore()",
        "ignore(WORD, DIGIT)",
        "ignore(pattern=WORD)",
        "ignore(UNKNOWN)",
        'ignore(re(".*"))',
        'ignore.__call__(".*")',
        'ignore["pattern"]',
        'ignore("a" + "b")',
        'ignore(f"{value}")',
        "ignore(x for x in values)",
        'ignore("[")',
        "ignore | ORPHRASE",
        'ignore | re(".*")',
        'ignore(".*") | WORD',
        "value | ignore",
        "value | ignore(WORD)",
    ],
)
def test_invalid_ignore_forms_are_rejected_with_fixed_feedback(
    expression: str,
) -> None:
    issues = inspect_ttp_template(f"{{{{ {expression} }}}}")

    assert len(issues) == 1
    assert issues[0].code == "ttp.invalid_ignore_syntax"
    assert issues[0].details == {
        "required_action": "replace_with_ignore_call",
    }
    assert expression not in issues[0].message
    assert expression not in str(issues[0].details)


def test_ignore_regex_uses_the_existing_regex_limit() -> None:
    issues = inspect_ttp_template(
        '{{ ignore("abcd") }} {{ value }}',
        max_ttp_regex_chars=3,
    )

    assert _codes(issues) == {"ttp.invalid_ignore_syntax"}


def test_deep_xml_and_repeated_static_issues_remain_bounded() -> None:
    deeply_nested = (
        '<group name="nested">' * 1_200 + "{{ value | WORD }}" + "</group>" * 1_200
    )
    issues = inspect_ttp_template(deeply_nested)
    assert _codes(issues) == {"ttp.group_depth_exceeded"}

    repeated = "<template>" + "<forbidden />" * 500 + "</template>"
    issues = inspect_ttp_template(repeated)
    assert len(issues) == 100
    assert _codes(issues) == {"ttp.forbidden_tag"}


def test_callers_can_tighten_but_not_loosen_static_ttp_limits() -> None:
    template = '<group name="items*">{{ value | re("abcd") }}</group>'
    nested = (
        '<group name="outer"><group name="inner">{{ value | WORD }}</group></group>'
    )

    assert "ttp.template_too_large" in _codes(
        inspect_ttp_template(template, max_ttp_template_bytes=10),
    )
    assert "ttp.group_depth_exceeded" in _codes(
        inspect_ttp_template(nested, max_ttp_group_depth=1),
    )
    assert "ttp.unsafe_variable_attribute" in _codes(
        inspect_ttp_template(template, max_ttp_regex_chars=3),
    )
    assert "ttp.unsafe_variable_attribute" in _codes(
        inspect_ttp_template(
            "{{ value | contains('abcd') }}",
            max_ttp_argument_chars=3,
        ),
    )

    oversized = "{{ value | WORD }}" + "x" * (64 * 1024)
    assert "ttp.template_too_large" in _codes(
        inspect_ttp_template(oversized, max_ttp_template_bytes=10**9),
    )


def test_regex_alternation_is_not_mistaken_for_a_pipeline_separator() -> None:
    template = 'State: {{ state | re("up|down") }}'

    result = validate_ttp_template(template, ["State: up"], _line_schema("state"))

    assert result.valid
    assert result.records == [{"state": "up"}]


def test_real_linux_outputs_are_fully_parsed_with_ignore_calls() -> None:
    directory = (
        Path(__file__).resolve().parents[2]
        / "testdata"
        / "real_command_outputs"
        / "ttp_templates"
        / "linux"
        / "ip_address_show"
    )
    outputs = [
        (directory / "ip_address_show_multiple_addresses.txt").read_text(
            encoding="utf-8",
        ),
        (directory / "ip_address_show.txt").read_text(encoding="utf-8"),
        (directory / "ip_address_show_short_header.txt").read_text(
            encoding="utf-8",
        ),
    ]
    template = (
        '<group name="interfaces*">\n'
        "{{ ignore(DIGIT) }}: {{ name | WORD }}: "
        "&lt;{{ ignore(ORPHRASE) }}&gt; mtu {{ ignore(DIGIT) }} "
        'qdisc {{ ignore(".*?") }}state {{ ignore(WORD) }}'
        "{{ ignore(ORPHRASE) }}\n"
        "</group>\n"
    )

    result = validate_ttp_template(
        template,
        outputs,
        _array_schema("interfaces", "name"),
    )

    assert result.valid, result.issues
    assert [len(record["interfaces"]) for record in result.records] == [5, 6, 2]


def test_real_inventory_outputs_capture_every_serial_number() -> None:
    directory = (
        Path(__file__).resolve().parents[2]
        / "testdata"
        / "real_command_outputs"
        / "ttp_templates"
        / "cisco_ios"
        / "show_inventory"
    )
    outputs = [
        (directory / "show_inventory.txt").read_text(encoding="utf-8"),
        (directory / "show_inventory_modular_router.txt").read_text(
            encoding="utf-8",
        ),
    ]
    template = """\
<group name="inventory*">
PID: {{ ignore(".*?") }}, VID: {{ ignore(".*?") }}, SN: {{ sn | WORD }}
</group>
"""

    result = validate_ttp_template(
        template,
        outputs,
        _array_schema("inventory", "sn"),
    )

    assert result.valid, result.issues
    assert [len(record["inventory"]) for record in result.records] == [4, 6]
    assert all(item["sn"] for record in result.records for item in record["inventory"])


def test_delimiter_bounded_regex_captures_empty_inventory_pid() -> None:
    source = (
        Path(__file__).resolve().parents[2]
        / "testdata"
        / "real_command_outputs"
        / "ttp_templates"
        / "cisco_ios"
        / "show_inventory"
        / "show_inventory.txt"
    ).read_text(encoding="utf-8")
    item_properties = {
        "name": {"type": "string"},
        "descr": {"type": "string"},
        "pid": {"type": "string"},
        "vid": {"type": "string"},
        "sn": {"type": "string"},
    }
    schema = {
        "type": "object",
        "properties": {
            "components": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": item_properties,
                    "required": list(item_properties),
                    "additionalProperties": False,
                },
            },
        },
        "required": ["components"],
        "additionalProperties": False,
    }
    template = (
        '<group name="components*">\n'
        'NAME: "{{ name | PHRASE }}", DESCR: "{{ descr | PHRASE }}"\n'
        'PID: {{ pid | re("(?:[^ \\t,](?:[^,]*[^ \\t,])?)?") }} , '
        "VID: {{ vid | ORPHRASE }}, SN: {{ sn | ORPHRASE }}\n"
        "</group>\n"
    )

    result = validate_ttp_template(template, [source], schema)

    assert result.valid, result.issues
    components = result.records[0]["components"]
    assert len(components) == 4
    assert components[0] == {
        "name": "3640 chassis",
        "descr": "3640 chassis",
        "pid": "",
        "vid": "0xFF",
        "sn": "FF1045C5",
    }
    assert [component["pid"] for component in components[1:]] == [
        "NM-1FE-TX=",
        "NM-1FE-TX=",
        "NM-1FE-TX=",
    ]


def test_empty_root_record_is_accepted_when_schema_allows_it() -> None:
    result = validate_ttp_template(
        "Value: {{ value | WORD }}",
        ["Different: text"],
        _optional_line_schema(),
    )

    assert result.valid
    assert result.records == [{}]


def test_empty_root_record_is_rejected_only_by_required_schema() -> None:
    result = validate_ttp_template(
        "Value: {{ value | WORD }}",
        ["Different: text"],
        _line_schema(),
    )

    assert _codes(result.issues) == {"schema.record_mismatch"}
    assert result.issues[0].output_index == 0


def test_empty_root_and_other_schema_errors_are_reported_uniformly() -> None:
    result = validate_ttp_template(
        "Value: {{ value | WORD }}",
        ["Different: text", "Value: text"],
        _line_schema(field_type="integer"),
    )

    assert result.records == [{}, {"value": "text"}]
    assert [issue.code for issue in result.issues] == [
        "schema.record_mismatch",
        "schema.record_mismatch",
    ]
    assert [issue.output_index for issue in result.issues] == [0, 1]


@pytest.mark.parametrize(
    "keyword",
    [
        "max_ttp_template_bytes",
        "max_ttp_group_depth",
        "max_ttp_regex_chars",
        "max_ttp_argument_chars",
    ],
)
def test_static_ttp_limits_must_be_positive(keyword: str) -> None:
    with pytest.raises(ValueError, match="positive integer"):
        inspect_ttp_template("{{ value | WORD }}", **{keyword: 0})


def test_full_parse_preserves_input_order_and_nested_records() -> None:
    template = (
        '<group name="interfaces*" method="table">\n'
        "{{ name | WORD }} {{ status | WORD }}\n"
        "</group>"
    )
    outputs = ["eth0 up\neth1 down", "lo0 up"]

    result = validate_ttp_template(template, outputs, _table_schema())

    assert result.valid
    assert result.issues == []
    assert result.records == [
        {
            "interfaces": [
                {"name": "eth0", "status": "up"},
                {"name": "eth1", "status": "down"},
            ],
        },
        {"interfaces": [{"name": "lo0", "status": "up"}]},
    ]


def test_full_parse_omits_unmatched_optional_properties() -> None:
    template = """\
<group name="inventory*">
NAME: {{ name | ORPHRASE }}
PID: {{ pid | ORPHRASE }}
</group>"""
    source = """\
NAME: first
PID: one
NAME: second
NAME: third
PID: three"""
    item_schema = {
        "type": "object",
        "properties": {
            "name": {"type": "string"},
            "pid": {"type": "string"},
        },
        "required": ["name"],
        "additionalProperties": False,
    }
    schema = {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "type": "object",
        "properties": {"inventory": {"type": "array", "items": item_schema}},
        "required": ["inventory"],
        "additionalProperties": False,
    }

    result = validate_ttp_template(template, [source], schema)

    assert result.valid, result.issues
    assert result.records == [
        {
            "inventory": [
                {"name": "first", "pid": "one"},
                {"name": "second"},
                {"name": "third", "pid": "three"},
            ],
        },
    ]


@pytest.mark.asyncio
async def test_full_parse_can_spawn_from_asyncio_worker_thread() -> None:
    template = (
        '<group name="interfaces*" method="table">\n'
        "{{ name | WORD }} {{ status | WORD }}\n"
        "</group>"
    )

    result = await asyncio.to_thread(
        validate_ttp_template,
        template,
        ["eth0 up"],
        _table_schema(),
    )

    assert result.valid
    assert result.records == [
        {"interfaces": [{"name": "eth0", "status": "up"}]},
    ]


def test_nested_groups_produce_nested_schema_conformant_records() -> None:
    template = """\
<group name="systems*">
System: {{ system_name | WORD }}
<group name="interfaces*">
 Interface: {{ name | WORD }} {{ status | WORD }}
</group>
</group>"""
    source = """\
System: r1
 Interface: eth0 up
 Interface: eth1 down
System: r2
 Interface: eth2 up"""
    interface = {
        "type": "object",
        "properties": {
            "name": {"type": "string"},
            "status": {"type": "string"},
        },
        "required": ["name", "status"],
        "additionalProperties": False,
    }
    system = {
        "type": "object",
        "properties": {
            "system_name": {"type": "string"},
            "interfaces": {"type": "array", "items": interface},
        },
        "required": ["system_name", "interfaces"],
        "additionalProperties": False,
    }
    schema = {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "type": "object",
        "properties": {"systems": {"type": "array", "items": system}},
        "required": ["systems"],
        "additionalProperties": False,
    }

    result = validate_ttp_template(template, [source], schema)

    assert result.valid
    assert result.records[0]["systems"][1] == {
        "system_name": "r2",
        "interfaces": [{"name": "eth2", "status": "up"}],
    }


def _root_scalar_and_array_schema() -> dict:
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "type": "object",
        "properties": {
            "routing_table_type": {"type": "string"},
            "routes": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "destination_mask": {"type": "string"},
                        "protocol": {"type": "string"},
                    },
                    "required": ["destination_mask", "protocol"],
                    "additionalProperties": False,
                },
            },
        },
        "required": ["routing_table_type", "routes"],
        "additionalProperties": False,
    }


def test_anonymous_root_group_yields_one_root_object() -> None:
    """A root object holding scalars plus an array needs an unnamed root group.

    Naming that group would nest the root scalars under the name, so TTP's extra
    single-element list around unnamed top-level groups must be unwrapped rather
    than rejected as "not a root object".
    """

    template = """\
<group>
Routing Tables: {{ routing_table_type | WORD }}
<group name="routes*">
{{ ignore("\\s*") }}{{ destination_mask | WORD }} {{ protocol | WORD }}
</group>
</group>"""
    source = """\
Routing Tables: Public
        10.0.0.0/8  Static
        10.1.0.0/16  Direct"""

    result = validate_ttp_template(
        template,
        [source],
        _root_scalar_and_array_schema(),
    )

    assert result.valid
    assert result.records == [
        {
            "routing_table_type": "Public",
            "routes": [
                {"destination_mask": "10.0.0.0/8", "protocol": "Static"},
                {"destination_mask": "10.1.0.0/16", "protocol": "Direct"},
            ],
        },
    ]


def test_anonymous_root_unwrapping_preserves_input_order() -> None:
    template = """\
<group>
Routing Tables: {{ routing_table_type | WORD }}
<group name="routes*">
{{ ignore("\\s*") }}{{ destination_mask | WORD }} {{ protocol | WORD }}
</group>
</group>"""
    first = "Routing Tables: Public\n        10.0.0.0/8  Static"
    second = "Routing Tables: Private\n        192.168.0.0/16  Direct"

    result = validate_ttp_template(
        template,
        [first, second],
        _root_scalar_and_array_schema(),
    )

    assert result.valid
    assert [record["routing_table_type"] for record in result.records] == [
        "Public",
        "Private",
    ]


def test_repeating_anonymous_root_group_is_still_rejected() -> None:
    """Unwrapping must only collapse a single-element wrapper.

    An unnamed root group that matches twice yields two root objects for one
    input, which the one-root-object contract still has to reject.
    """

    template = """\
<group>
Host: {{ host | WORD }}
</group>"""

    result = validate_ttp_template(
        template,
        ["Host: r1\nHost: r2"],
        _line_schema(field="host"),
    )

    assert not result.valid
    assert any("root object" in issue.message for issue in result.issues)


def test_full_parse_requires_frozen_schema_match() -> None:
    template = "Value: {{ value | WORD }}"
    schema = _line_schema(field_type="integer")

    result = validate_ttp_template(template, ["Value: text"], schema)

    assert not result.valid
    assert result.records == [{"value": "text"}]
    assert "schema.record_mismatch" in _codes(result.issues)


def test_leading_zero_conversion_no_longer_requires_source_match() -> None:
    template = "Value: {{ value | DIGIT | to_int }}"

    result = validate_ttp_template(
        template,
        ["Value: 001"],
        _line_schema(field_type="integer"),
    )

    assert result.valid
    assert result.records == [{"value": 1}]


def test_cidr_conversion_no_longer_requires_final_value_in_source() -> None:
    result = validate_ttp_template(
        "Mask: {{ value | ORPHRASE | to_cidr }}",
        ["Mask: 255.255.255.0"],
        _line_schema(field_type="integer"),
    )

    assert result.valid
    assert result.records == [{"value": 24}]


def test_ip_normalization_no_longer_requires_normalized_source_text() -> None:
    result = validate_ttp_template(
        "Address: {{ value | IPV6 | to_ip }}",
        ["Address: 2001:0db8::1"],
        _line_schema(),
    )

    assert result.valid
    assert result.records == [{"value": "2001:db8::1"}]


def test_input_that_is_an_existing_path_is_parsed_as_text(tmp_path: Path) -> None:
    source_file = tmp_path / "output.txt"
    source_file.write_text("content-that-must-not-be-read", encoding="utf-8")
    source = str(source_file)

    result = validate_ttp_template(
        "{{ value | _line_ }}",
        [source],
        _line_schema(),
    )

    assert result.valid
    assert result.records == [{"value": source}]


def test_isolated_worker_can_be_terminated_on_timeout() -> None:
    result = validate_ttp_template(
        "{{ value | _line_ }}",
        ["x" * 100_000],
        _line_schema(),
        timeout_seconds=0,
    )

    assert result.records == []
    assert _codes(result.issues) == {"ttp.timeout"}


def test_timed_out_regex_worker_is_terminated_without_join_delay() -> None:
    started = time.monotonic()
    result = validate_ttp_template(
        '{{ value | re("(a+)+$") }}',
        ["a" * 30_000 + "!"],
        _line_schema(),
        timeout_seconds=0.05,
    )

    assert _codes(result.issues) == {"ttp.timeout"}
    assert time.monotonic() - started < 1.5


@pytest.mark.parametrize("timeout", [float("nan"), float("inf"), -float("inf")])
def test_timeout_must_be_finite(timeout: float) -> None:
    result = validate_ttp_template(
        "{{ value | _line_ }}",
        ["x"],
        _line_schema(),
        timeout_seconds=timeout,
    )

    assert _codes(result.issues) == {"ttp.invalid_timeout"}


def test_empty_required_string_is_allowed_by_frozen_schema() -> None:
    issues = validate_records_against_schema(
        [{"value": ""}],
        _line_schema(),
    )

    assert issues == []


def test_full_parse_preserves_literal_empty_string_when_schema_allows() -> None:
    result = validate_ttp_template(
        'PID: {{ value | re("[^ ]*") }},',
        ["PID: ,"],
        _line_schema(),
    )

    assert result.valid, result.issues
    assert result.records == [{"value": ""}]


def test_multiple_empty_root_records_are_left_for_model_review() -> None:
    result = validate_ttp_template(
        "Value: {{ value | WORD }}",
        ["Different: one", "Different: two"],
        _optional_line_schema(),
    )

    assert result.valid
    assert result.records == [{}, {}]


def test_null_optional_value_is_rejected_by_frozen_schema() -> None:
    issues = validate_records_against_schema(
        [{"value": None}],
        _line_schema(),
    )

    assert _codes(issues) == {"schema.record_mismatch"}


def test_isolated_worker_applies_tightened_result_size_limit() -> None:
    result = validate_ttp_template(
        "{{ value | _line_ }}",
        ["some parsed content"],
        _line_schema(),
        max_result_bytes=10,
    )

    assert result.records == []
    assert _codes(result.issues) == {"ttp.result_too_large"}
