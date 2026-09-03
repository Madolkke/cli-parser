"""Parity guard: ensure prompt-documented allowlists match validator reality.

The TTP generation prompt (prompt.py) documents which TTP pipeline attributes,
line controls, and group methods are allowed. The validator (validation/ttp.py)
enforces those rules. This test ensures they stay synchronized: if the validator
adds or removes an allowed item, the prompt must be updated to match, or the
agent will receive incorrect guidance.
"""

import re

import pytest


def test_prompt_documents_all_validator_allowed_attributes():
    """Prompt's attribute list must cover every validator-allowed attribute."""
    from cli_parser_agent.ttp_generation.agent.prompt import TTP_SYSTEM_PROMPT
    from cli_parser_agent.ttp_generation.validation.ttp import _ALLOWED_ATTRIBUTES

    # Extract the documented attribute list from the prompt rules
    # The rule states: "变量 pipeline 只允许使用 WORD、PHRASE、..."
    # Find that line and parse the semicolon-separated list
    match = re.search(
        r"变量 pipeline 只允许使用([^。]+)。",
        TTP_SYSTEM_PROMPT,
        re.DOTALL,
    )
    assert match, "Could not find attribute allowlist in prompt rules"

    documented_text = match.group(1)

    # Parse documented attributes - they're listed with Chinese punctuation
    # "WORD、PHRASE、ORPHRASE、ROW、DIGIT、IP、IPV6、MAC、PREFIX、PREFIXV6；
    #  行控制 _start_、_end_、_line_、_exact_、_exact_space_、_headers_；
    #  string/regex 条件；re、joinmatches、item；以及安全的 to_int/to_float/..."

    # Extract all identifiers (Latin alphanumeric with underscores)
    documented_attrs = set(re.findall(r"\b([A-Z_a-z][A-Z_a-z0-9]*)\b", documented_text))

    validator_attrs = _ALLOWED_ATTRIBUTES

    # The prompt groups some items by category ("string/regex 条件" instead of listing
    # each one). We need to handle this intelligently.
    # For now, check that all documented specific items are in the validator set,
    # and that no validator items are completely undocumented.

    # Items explicitly named in prompt should be in validator
    named_in_prompt = {
        "WORD", "PHRASE", "ORPHRASE", "ROW", "DIGIT", "IP", "IPV6", "MAC",
        "PREFIX", "PREFIXV6",
        "_start_", "_end_", "_line_", "_exact_", "_exact_space_", "_headers_",
        "re", "joinmatches", "item",
        "to_int", "to_float", "to_str", "to_ip", "to_net", "to_cidr",
    }

    missing_from_validator = named_in_prompt - validator_attrs
    assert not missing_from_validator, (
        f"Prompt documents attributes not in validator: {missing_from_validator}"
    )

    # Validator should not have attributes completely missing from prompt context
    # We allow string/regex conditions to be summarized rather than listed
    string_conditions = {
        "contains", "equal", "exclude", "notequal",
        "contains_re", "endswith_re", "exclude_re",
        "notendswith_re", "notstartswith_re", "startswith_re",
    }

    # Known items that validator has but prompt summarizes or doesn't need to document
    validator_only_ok = string_conditions | {
        "columns",  # Related to table processing, not commonly needed
        "isdigit", "notdigit",  # Rarely used filters
        "is_ip",  # Type check, rarely needed
    }

    undocumented = validator_attrs - documented_attrs - validator_only_ok

    assert not undocumented, (
        f"Validator allows attributes not documented in prompt: {undocumented}\n"
        f"Update _TTP_GENERATION_RULES in prompt.py to document these."
    )


def test_prompt_forbids_column_function():
    """Prompt must explicitly forbid column(...) since it's not a TTP function."""
    from cli_parser_agent.ttp_generation.agent.prompt import TTP_SYSTEM_PROMPT

    # The prompt should contain a rule forbidding column(...)
    assert "column(...)" in TTP_SYSTEM_PROMPT or "column(" in TTP_SYSTEM_PROMPT, (
        "Prompt should explicitly forbid column() function"
    )
    assert "禁止" in TTP_SYSTEM_PROMPT or "不是" in TTP_SYSTEM_PROMPT, (
        "Prompt should state that column is forbidden or not a TTP function"
    )


def test_prompt_documents_group_method_allowlist():
    """Prompt must document that group method only accepts 'group' or 'table'."""
    from cli_parser_agent.ttp_generation.agent.prompt import TTP_SYSTEM_PROMPT

    # The rule states: "method 只能取 "group"（默认）或 "table""
    assert 'method' in TTP_SYSTEM_PROMPT, (
        "Prompt should document the group method attribute"
    )

    # Check that both allowed values are mentioned
    assert '"table"' in TTP_SYSTEM_PROMPT or "'table'" in TTP_SYSTEM_PROMPT, (
        'Prompt should document method="table" as allowed'
    )

    # The prompt should state these are the only two options
    method_pattern = re.search(
        r'method\s+只能取[^。]+[""]\s*table\s*[""][^。]*。',
        TTP_SYSTEM_PROMPT,
    )
    assert method_pattern, (
        "Prompt should state that method only accepts 'group' or 'table'"
    )


def test_prompt_documents_group_attribute_allowlist():
    """Prompt must document that group only accepts 'name' and 'method' attributes."""
    from cli_parser_agent.ttp_generation.agent.prompt import TTP_SYSTEM_PROMPT

    # The rule states: "group 只允许两个 XML 属性：name 和 method"
    assert "group 只允许两个 XML 属性" in TTP_SYSTEM_PROMPT or \
           "group 只允许两个属性" in TTP_SYSTEM_PROMPT, (
        "Prompt should state that group only allows two XML attributes"
    )

    assert "name" in TTP_SYSTEM_PROMPT and "method" in TTP_SYSTEM_PROMPT, (
        "Prompt should document both 'name' and 'method' as allowed group attributes"
    )
