from __future__ import annotations

import asyncio
import keyword
import re
import time
from pathlib import Path

import pytest

from cli_parser_agent.ttp_generation.agent.prompt import TTP_SYSTEM_PROMPT
from cli_parser_agent.ttp_generation.validation import (
    inspect_ttp_template,
    parse_ttp_template,
    validate_records_against_schema,
    validate_ttp_template,
)


def test_parse_ttp_template_preserves_raw_single_input_shape() -> None:
    outcome = parse_ttp_template(
        "Value: {{ value }}",
        "Value: abc",
        timeout_seconds=5,
    )

    assert outcome.valid
    assert outcome.issues == []
    assert outcome.result == [{"value": "abc"}]


def test_parse_ttp_template_does_not_require_a_schema_or_root_object() -> None:
    outcome = parse_ttp_template(
        '<group name="items*">\n{{ value | WORD }}\n</group>',
        "one\ntwo",
        timeout_seconds=5,
    )

    assert outcome.valid
    assert isinstance(outcome.result, list)
    assert outcome.result


def test_parse_ttp_template_rejects_blank_and_oversized_test_input() -> None:
    blank = parse_ttp_template("Value: {{ value }}", " \n")
    oversized = parse_ttp_template("Value: {{ value }}", "x" * (1024 * 1024 + 1))
    oversized_utf8 = parse_ttp_template(
        "Value: {{ value }}",
        "中" * (1024 * 1024 // len("中".encode()) + 1),
    )

    assert [issue.code for issue in blank.issues] == ["ttp.test_input_empty"]
    assert [issue.code for issue in oversized.issues] == ["ttp.test_input_too_large"]
    assert [issue.code for issue in oversized_utf8.issues] == [
        "ttp.test_input_too_large",
    ]


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


@pytest.mark.parametrize(
    "expression",
    [
        'value | re("up|down")',
        'value | re("Alpha One.*|Beta Two.*")',
        'value | exclude("red fox|blue bird")',
        "value | contains('red|blue')",
        'value | equal("red|blue")',
        'value | notequal("red|blue")',
        'value | contains_re("red|blue")',
        'value | endswith_re("red|blue")',
        'value | exclude_re("red|blue")',
        'value | notendswith_re("red|blue")',
        'value | notstartswith_re("red|blue")',
        'value | startswith_re("red|blue")',
        'value | joinmatches("|")',
        'ignore("red|blue")',
        r'value | re(r"red\|blue")',
    ],
)
def test_argument_pipes_are_rejected_before_evaluation(expression: str) -> None:
    template = '<group name="items*">\n{{ ' + expression + " }}\n</group>"

    issues = inspect_ttp_template(template)

    assert len(issues) == 1
    issue = issues[0]
    assert issue.code == "ttp.incompatible_argument_pipe"
    assert issue.path == "/template/group[0]"
    assert issue.details == {
        "required_action": "split_pipe_argument",
        "line": 2,
        "column": template.splitlines()[1].rfind("|") + 1,
    }
    assert expression not in issue.message
    assert expression not in str(issue.details)


@pytest.mark.parametrize("encoded_pipe", ["&#124;", "&#x7c;"])
def test_argument_pipe_check_runs_after_xml_decoding(encoded_pipe: str) -> None:
    template = '{{ value | re("red' + encoded_pipe + 'blue") }}'

    issue = inspect_ttp_template(template)[0]

    assert issue.code == "ttp.incompatible_argument_pipe"
    assert issue.path == "/template"
    assert issue.details == {"required_action": "split_pipe_argument"}


def test_argument_pipe_position_does_not_guess_for_repeated_expressions() -> None:
    expression = '{{ value | exclude("red|blue") }}'
    template = expression + "\n" + expression

    issues = inspect_ttp_template(template)

    assert len(issues) == 2
    assert all(
        issue.details == {"required_action": "split_pipe_argument"} for issue in issues
    )


def test_argument_pipe_reports_multiline_position_and_containing_group() -> None:
    template = (
        '<template>\n<group>\n<group name="items*">\n'
        '{{ value }}\n</group>\n{{ status |\n re("up|down") }}\n'
        "</group>\n</template>"
    )

    issue = inspect_ttp_template(template)[0]

    assert issue.code == "ttp.incompatible_argument_pipe"
    assert issue.path == "/template/group[0]"
    assert issue.details == {
        "required_action": "split_pipe_argument",
        "line": 7,
        "column": 8,
    }


def test_long_argument_pipe_diagnostics_remain_bounded() -> None:
    secret_argument = "a" * 4_000 + "|" + "b" * 4_000
    template = '{{ value | exclude("' + secret_argument + '") }}\n'

    issues = inspect_ttp_template(template * 7)

    assert len(issues) == 7
    assert all(issue.code == "ttp.incompatible_argument_pipe" for issue in issues)
    assert all(len(str(issue.model_dump())) < 500 for issue in issues)
    assert secret_argument not in str(issues)


@pytest.mark.parametrize(
    "template",
    [
        "{{ value | WORD | to_str }}",
        '{{ value | exclude("red fox") | exclude("blue bird") }}',
        '{{ value | re("up") | re("down") }}',
        r'{{ value | re("[\x7c]") }}',
        r'{{ value | re("[\u007c]") }}',
        r'{{ ignore("[\x7c]") }} {{ value }}',
        "| {{ value | WORD }} |",
    ],
)
def test_compatible_pipeline_and_argument_spellings_stay_allowed(
    template: str,
) -> None:
    assert inspect_ttp_template(template) == []


def test_separate_regex_filters_match_each_supported_alternative() -> None:
    template = 'State: {{ state | re("up") | re("down") }}'

    result = validate_ttp_template(
        template, ["State: up", "State: down"], _line_schema("state")
    )
    unmatched = validate_ttp_template(
        template, ["State: elsewhere"], _line_schema("state")
    )

    assert result.valid
    assert result.records == [{"state": "up"}, {"state": "down"}]
    assert not unmatched.valid


def test_separate_exclude_filters_remove_each_unwanted_value() -> None:
    template = (
        '<group name="items*">\n'
        '{{ value | ORPHRASE | exclude("red fox") | exclude("blue bird") }}\n'
        "</group>"
    )

    result = validate_ttp_template(
        template,
        ["red fox\nblue bird\ngreen leaf"],
        _array_schema("items", "value"),
    )

    assert result.valid
    assert result.records == [{"items": [{"value": "green leaf"}]}]


def test_incompatible_argument_pipes_never_start_a_worker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def unexpected_worker(*args, **kwargs):
        pytest.fail("incompatible argument reached a TTP worker")

    module = "cli_parser_agent.ttp_generation.validation.ttp"
    monkeypatch.setattr(f"{module}._run_ttp_isolated", unexpected_worker)
    monkeypatch.setattr(f"{module}._run_ttp_test_isolated", unexpected_worker)
    template = '{{ value | re("Alpha One.*|Beta Two.*") }}'

    submitted = validate_ttp_template(template, ["Alpha One"], _line_schema())
    tested = parse_ttp_template(template, "Alpha One")

    assert _codes(submitted.issues) == {"ttp.incompatible_argument_pipe"}
    assert _codes(tested.issues) == {"ttp.incompatible_argument_pipe"}
    assert submitted.records == []
    assert tested.result is None


def test_real_linux_outputs_are_fully_parsed_with_ignore_calls() -> None:
    outputs = [
        "\n".join(
            f"{index}: eth{index}: <UP> mtu 1500 qdisc noop state UP"
            for index in range(1, 7)
        ),
        "\n".join(
            f"{index}: enp{index}: <UP> mtu 1500 qdisc noop state UP"
            for index in range(1, 6)
        ),
        "\n".join(
            f"{index}: lo{index}: <UP> mtu 1500 qdisc noop state UP"
            for index in range(1, 3)
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
    assert [len(record["interfaces"]) for record in result.records] == [6, 5, 2]


def test_real_inventory_outputs_capture_every_serial_number() -> None:
    outputs = [
        "\n".join(
            f"PID: module-{index}, VID: 1.0, SN: SN{index:04d}" for index in range(1, 5)
        ),
        "\n".join(
            f"PID: module-{index}, VID: 1.0, SN: SN{index:04d}" for index in range(1, 7)
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
    source = "\n".join(
        (
            f'NAME: "component {index}", DESCR: "description {index}"\n'
            f"PID: {pid} , VID: 0xFF, SN: SN{index:04d}"
        )
        for index, pid in enumerate(("", "module-a", "module-b", "module-c"), start=1)
    )
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
        "name": "component 1",
        "descr": "description 1",
        "pid": "",
        "vid": "0xFF",
        "sn": "SN0001",
    }
    assert [component["pid"] for component in components[1:]] == [
        "module-a",
        "module-b",
        "module-c",
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


def test_group_method_table_is_accepted_and_preserves_source_order() -> None:
    """The prompt now steers toward method="table"; pin that it is legal.

    Sibling same-name groups are parsed separately and appended per group, so
    source order is lost. Multiple match lines in one default-method group lose
    rows entirely, because a later line is a continuation of the current
    record. method="table" is the only construct that does both correctly.
    """
    template = (
        '<group name="rows*" method="table">\n'
        "{{ port }} {{ status }} {{ speed }}\n"
        "{{ port }} {{ status }}\n"
        "</group>\n"
    )
    schema = {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "type": "object",
        "properties": {
            "rows": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "port": {"type": "string"},
                        "status": {"type": "string"},
                        "speed": {"type": "string"},
                    },
                    "required": ["port", "status"],
                    "additionalProperties": False,
                },
            },
        },
        "required": ["rows"],
        "additionalProperties": False,
    }

    result = validate_ttp_template(
        template,
        ["a1 up 1\na2 down\na3 up 3\n"],
        schema,
    )

    assert result.valid, _codes(result.issues)
    assert [row["port"] for row in result.records[0]["rows"]] == ["a1", "a2", "a3"]


def test_unknown_group_method_is_rejected() -> None:
    template = '<group name="rows*" method="bogus">\n{{ port }}\n</group>\n'

    result = validate_ttp_template(
        template,
        ["a1\n"],
        _array_schema("rows", "port"),
    )

    assert not result.valid
    assert "ttp.invalid_group_method" in _codes(result.issues)


def _prompt_template(marker: str) -> str:
    templates = re.findall(r"```(?:xml|text)\n(.*?)\n```", TTP_SYSTEM_PROMPT, re.DOTALL)
    matches = [template for template in templates if marker in template]
    assert len(matches) == 1
    return matches[0] + "\n"


def test_prompt_basic_array_recipe_preserves_each_row() -> None:
    template = _prompt_template('<group name="interfaces*">')
    schema = {
        "type": "object",
        "properties": {
            "interfaces": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        key: {"type": "string"} for key in ("port", "name", "status")
                    },
                    "required": ["port", "name", "status"],
                    "additionalProperties": False,
                },
            },
        },
        "required": ["interfaces"],
        "additionalProperties": False,
    }

    result = validate_ttp_template(
        template,
        ["a1  core switch  up\na2  edge  down\n"],
        schema,
    )

    assert result.valid, _codes(result.issues)
    assert result.records == [
        {
            "interfaces": [
                {"port": "a1", "name": "core switch", "status": "up"},
                {"port": "a2", "name": "edge", "status": "down"},
            ]
        }
    ]


def test_prompt_ignore_recipe_skips_multiple_tokens_without_extra_fields() -> None:
    template = _prompt_template("{{ ignore(DIGIT) }}:")
    schema = {
        "type": "object",
        "properties": {key: {"type": "string"} for key in ("name", "mtu", "state")},
        "required": ["name", "mtu", "state"],
        "additionalProperties": False,
    }

    result = validate_ttp_template(
        template,
        ["2: eth0: <BROADCAST MULTICAST>\nmtu 1500 qdisc fq state UP\n"],
        schema,
    )

    assert result.valid, _codes(result.issues)
    assert result.records == [{"name": "eth0", "mtu": "1500", "state": "UP"}]


def test_prompt_section_headings_keep_same_labels_in_their_own_sections() -> None:
    template = _prompt_template('<group name="primary">')
    counter_schema = {
        "type": "object",
        "properties": {
            "packets": {"type": "string"},
            "errors": {"type": "string"},
        },
        "required": ["packets", "errors"],
        "additionalProperties": False,
    }
    schema = {
        "type": "object",
        "properties": {"primary": counter_schema, "backup": counter_schema},
        "required": ["primary", "backup"],
        "additionalProperties": False,
    }
    sources = [
        "Primary counters:\nPackets: 120\nErrors: 2\n"
        "Backup counters:\nPackets: 85\nErrors: 0\n",
        "Backup counters:\nPackets: 300\nErrors: 7\n"
        "Primary counters:\nPackets: 410\nErrors: 9\n",
    ]

    result = validate_ttp_template(template, sources, schema)

    assert result.valid, _codes(result.issues)
    assert result.records == [
        {
            "primary": {"packets": "120", "errors": "2"},
            "backup": {"packets": "85", "errors": "0"},
        },
        {
            "primary": {"packets": "410", "errors": "9"},
            "backup": {"packets": "300", "errors": "7"},
        },
    ]


def test_prompt_section_start_alone_can_capture_a_later_optional_field() -> None:
    """Pin the documented limit instead of promising a false section boundary."""
    template = _prompt_template('<group name="primary">').replace(
        "Errors: {{ errors | DIGIT }}\n",
        "Errors: {{ errors | DIGIT }}\nLabel: {{ label | WORD }}\n",
    )
    counter_schema = {
        "type": "object",
        "properties": {
            "packets": {"type": "string"},
            "errors": {"type": "string"},
            "label": {"type": "string"},
        },
        "required": ["packets", "errors"],
        "additionalProperties": False,
    }
    schema = {
        "type": "object",
        "properties": {"primary": counter_schema, "backup": counter_schema},
        "required": ["primary", "backup"],
        "additionalProperties": False,
    }
    sources = [
        "Primary counters:\nPackets: 120\nErrors: 2\nLabel: active\n"
        "Backup counters:\nPackets: 85\nErrors: 0\n",
        "Backup counters:\nPackets: 300\nErrors: 7\nLabel: standby\n"
        "Primary counters:\nPackets: 410\nErrors: 9\n",
        "Primary counters:\nPackets: 120\nErrors: 2\n"
        "Backup counters:\nPackets: 85\nErrors: 0\nLabel: standby\n",
        "Backup counters:\nPackets: 300\nErrors: 7\n"
        "Primary counters:\nPackets: 410\nErrors: 9\nLabel: active\n",
    ]

    result = validate_ttp_template(template, sources, schema)

    assert result.valid, _codes(result.issues)
    assert result.records == [
        {
            "primary": {"packets": "120", "errors": "2", "label": "active"},
            "backup": {"packets": "85", "errors": "0"},
        },
        {
            "primary": {"packets": "410", "errors": "9"},
            "backup": {"packets": "300", "errors": "7", "label": "standby"},
        },
        {
            "primary": {"packets": "120", "errors": "2", "label": "standby"},
            "backup": {"packets": "85", "errors": "0", "label": "standby"},
        },
        {
            "primary": {"packets": "410", "errors": "9", "label": "active"},
            "backup": {"packets": "300", "errors": "7", "label": "active"},
        },
    ]
    assert "仅起始标题不足以防止从后续章节补捕缺失字段" in TTP_SYSTEM_PROMPT


def test_prompt_structure_tags_and_literal_xml_have_distinct_roles() -> None:
    template = _prompt_template('<group name="link">')
    schema = {
        "type": "object",
        "properties": {
            "link": {
                "type": "object",
                "properties": {
                    "state": {"type": "string"},
                    "status": {"type": "string"},
                },
                "required": ["state", "status"],
                "additionalProperties": False,
            },
        },
        "required": ["link"],
        "additionalProperties": False,
    }

    result = validate_ttp_template(template, ["State: <up> & ready\n"], schema)

    assert result.valid, _codes(result.issues)
    assert result.records == [{"link": {"state": "up", "status": "ready"}}]


def test_prompt_table_variants_preserve_indented_row_order_and_exclude_header() -> None:
    template = _prompt_template('<group name="interfaces*" method="table">')
    schema = {
        "type": "object",
        "properties": {
            "interfaces": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        key: {"type": "string"}
                        for key in ("port", "name", "status", "speed")
                    },
                    "required": ["port", "status"],
                    "additionalProperties": False,
                },
            },
        },
        "required": ["interfaces"],
        "additionalProperties": False,
    }
    source = (
        "Port Name Status Speed\n"
        "  a1 core switch up 1000\n"
        "\ta2 down 100\n"
        "    a3 up\n"
        "a4 edge up 10\n"
    )

    result = validate_ttp_template(template, [source], schema)

    assert result.valid, _codes(result.issues)
    assert result.records == [
        {
            "interfaces": [
                {"port": "a1", "name": "core switch", "status": "up", "speed": "1000"},
                {"port": "a2", "status": "down", "speed": "100"},
                {"port": "a3", "status": "up"},
                {"port": "a4", "name": "edge", "status": "up", "speed": "10"},
            ]
        }
    ]


def test_prompt_empty_field_recipe_keeps_literal_empty_strings() -> None:
    template = _prompt_template("PID: {{ pid")
    schema = {
        "type": "object",
        "properties": {key: {"type": "string"} for key in ("pid", "vid", "sn")},
        "required": ["pid", "vid", "sn"],
        "additionalProperties": False,
    }

    result = validate_ttp_template(
        template,
        ["PID:  ,\nVID: V1, SN: ABC\n", "PID: PID123  ,\nVID: V2, SN: DEF\n"],
        schema,
    )

    assert result.valid, _codes(result.issues)
    assert result.records == [
        {"pid": "", "vid": "V1", "sn": "ABC"},
        {"pid": "PID123", "vid": "V2", "sn": "DEF"},
    ]


def test_prompt_mixed_root_recipe_keeps_scalar_and_nested_array() -> None:
    template = _prompt_template("Routing Tables: {{ routing_table_type")
    schema = {
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

    result = validate_ttp_template(
        template,
        ["Routing Tables: IPv4\n  10.0.0.0/24 OSPF\n\t192.0.2.0/24 Static\n"],
        schema,
    )

    assert result.valid, _codes(result.issues)
    assert result.records == [
        {
            "routing_table_type": "IPv4",
            "routes": [
                {"destination_mask": "10.0.0.0/24", "protocol": "OSPF"},
                {"destination_mask": "192.0.2.0/24", "protocol": "Static"},
            ],
        }
    ]


def test_joinmatches_continuation_rejoins_a_column_wrapped_row() -> None:
    """Wrapped rows are the opposite case from column-count variants.

    A default-method group's later match line continues the current record,
    which is exactly what a wrapped row needs. Adding method="table" here
    would turn each continuation into its own record instead.

    The template is extracted from the system prompt so the documented recipe
    cannot drift away from working TTP.
    """
    template = _prompt_template('<group name="neighbors*">')
    source = (
        "gi21 28:6f:7f:0b:75:a0 Gi0 Fjallarodgardsfor 105\n"
        "                                skola-AP03\n"
        "gi22 28:6f:7f:0b:75:a1 Gi1 Short 100\n"
    )
    schema = {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "type": "object",
        "properties": {
            "neighbors": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "port": {"type": "string"},
                        "device_id": {"type": "string"},
                        "port_id": {"type": "string"},
                        "name": {"type": "string"},
                        "ttl": {"type": "string"},
                    },
                    "required": [
                        "port",
                        "device_id",
                        "port_id",
                        "name",
                        "ttl",
                    ],
                    "additionalProperties": False,
                },
            },
        },
        "required": ["neighbors"],
        "additionalProperties": False,
    }

    result = validate_ttp_template(template, [source], schema)

    assert result.valid, _codes(result.issues)
    rows = result.records[0]["neighbors"]
    assert [row["port"] for row in rows] == ["gi21", "gi22"]
    assert rows[0]["name"] == "Fjallarodgardsforskola-AP03"
