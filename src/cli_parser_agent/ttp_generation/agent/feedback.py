"""Bounded, value-free projections of deterministic template diagnostics."""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from typing import TYPE_CHECKING, Any

from ..contracts import ValidationIssue

if TYPE_CHECKING:
    from .session import GenerationSession

MAX_FEEDBACK_ISSUES = 24
MAX_FEEDBACK_BYTES = 8 * 1024
MAX_MISSING_REQUIRED = 24

_KNOWN_CODES = frozenset(
    [
        "schema.record_mismatch",
        "schema_not_frozen",
        "generation_already_succeeded",
        "generation_already_terminated",
        "ttp_submission_limit",
        "record_count_mismatch",
        "record_root_not_object",
        "generation.timeout",
        "ttp.duplicate_line_variable",
        "ttp.empty_template",
        "ttp.forbidden_group_attribute",
        "ttp.forbidden_tag",
        "ttp.forbidden_template_attribute",
        "ttp.forbidden_xml_declaration",
        "ttp.group_attribute_too_long",
        "ttp.group_depth_exceeded",
        "ttp.invalid_field_name",
        "ttp.invalid_group_method",
        "ttp.invalid_group_name",
        "ttp.invalid_ignore_syntax",
        "ttp.incompatible_argument_pipe",
        "ttp.invalid_line_control",
        "ttp.invalid_root_tag",
        "ttp.invalid_utf8",
        "ttp.invalid_variable_syntax",
        "ttp.invalid_xml",
        "ttp.no_variables",
        "ttp.submission_invalid",
        "ttp.template_too_large",
        "ttp.unsafe_group_attribute",
        "ttp.unsafe_variable_attribute",
        "ttp.validator_failed",
        "ttp.timeout",
        "ttp.worker_bootstrap_failed",
        "ttp.worker_error",
        "ttp.worker_host_unsupported",
        "ttp.worker_start_failed",
        "ttp.invalid_shape",
        "ttp.invalid_mapping",
        "ttp.invalid_record",
        "ttp.multiple_root_objects",
        "ttp.result_too_large",
        "ttp.invalid_timeout",
        "ttp.no_inputs",
        "ttp.test_call_limit",
        "ttp.test_input_invalid",
        "ttp.test_validator_failed",
        "ttp.test_input_empty",
        "ttp.test_input_invalid_utf8",
        "ttp.test_input_too_large",
        "ttp.unchanged_submission",
    ],
)
_KEYWORDS = frozenset(
    [
        "type",
        "required",
        "additionalProperties",
        "enum",
        "minItems",
        "maxItems",
        "minLength",
        "maxLength",
        "minimum",
        "maximum",
        "exclusiveMinimum",
        "exclusiveMaximum",
        "multipleOf",
    ],
)
_JSON_TYPES = frozenset(
    {"null", "boolean", "integer", "number", "string", "array", "object"},
)
_EXCEPTION_TYPES = frozenset(
    [
        "SystemExit",
        "ValueError",
        "TypeError",
        "KeyError",
        "IndexError",
        "AttributeError",
        "RuntimeError",
        "RecursionError",
        "MemoryError",
        "OverflowError",
        "ZeroDivisionError",
        "OSError",
        "EOFError",
        "AssertionError",
        "ImportError",
        "ModuleNotFoundError",
        "error",
    ],
)
_ACTIONS = {
    "ttp.incompatible_argument_pipe": {"split_pipe_argument"},
    "ttp.invalid_ignore_syntax": {"replace_with_ignore_call"},
    "ttp.invalid_xml": {"escape_xml_metacharacters"},
    "ttp.invalid_line_control": {"attach_to_schema_field"},
    "ttp.unchanged_submission": {"finish_or_modify_template", "modify_template"},
}
_FIELD_NAME = re.compile(r"[a-z][a-z0-9]*(?:_[a-z0-9]+)*")
_TEMPLATE_PATH = re.compile(
    r"(?P<groups>/template(?:/group\[[0-9]{1,5}\])*)"
    r"(?:/@(?P<attribute>[^/]+))?",
)


def _bounded_int(value: Any) -> bool:
    return type(value) is int and 0 <= value <= 2_147_483_647


def _schema_node(
    schema: Mapping[str, Any] | None, path: Any
) -> Mapping[str, Any] | None:
    if not isinstance(path, str) or len(path) > 2048 or not path.startswith("/"):
        return None
    node = schema
    for part in () if path == "/" else path[1:].split("/"):
        if not isinstance(node, Mapping):
            return None
        if part == "*" and node.get("type") == "array":
            node = node.get("items")
        elif (
            len(part) <= 120
            and _FIELD_NAME.fullmatch(part)
            and node.get("type") == "object"
            and isinstance(node.get("properties"), Mapping)
        ):
            node = node["properties"].get(part)
        else:
            return None
    return node if isinstance(node, Mapping) else None


def _project_issue(
    issue: Any,
    *,
    schema: Mapping[str, Any] | None,
    input_count: int,
) -> dict[str, Any]:
    if isinstance(issue, ValidationIssue):
        item: Mapping[str, Any] = {
            "code": issue.code,
            "path": issue.path,
            "output_index": issue.output_index,
            "details": issue.details,
        }
    else:
        item = issue if isinstance(issue, Mapping) else {}
    raw_code = item.get("code")
    code = (
        raw_code
        if isinstance(raw_code, str) and raw_code in _KNOWN_CODES
        else "validation.unknown_issue"
    )
    result: dict[str, Any] = {
        "code": code,
        "input_index": None,
        "path": None,
        "keyword": None,
        "details": {},
    }
    if code == "validation.unknown_issue":
        return result
    index = item.get("output_index")
    if type(index) is int and 0 <= index < input_count:
        result["input_index"] = index
    raw_details = item.get("details")
    details = raw_details if isinstance(raw_details, Mapping) else {}
    safe_details = result["details"]
    raw_path = item.get("path")
    if code == "schema.record_mismatch":
        node = _schema_node(schema, raw_path)
        if node is not None:
            result["path"] = raw_path
        keyword = details.get("keyword")
        if isinstance(keyword, str) and keyword in _KEYWORDS:
            result["keyword"] = keyword
        if keyword == "required" and node is not None:
            missing = details.get("missing_required")
            required = node.get("required", [])
            if isinstance(missing, (list, tuple)) and isinstance(required, list):
                names = sorted(
                    {
                        name
                        for name in missing
                        if isinstance(name, str)
                        and len(name) <= 120
                        and _FIELD_NAME.fullmatch(name)
                        and name in required
                    },
                )
                safe_details["missing_required"] = names[:MAX_MISSING_REQUIRED]
                safe_details["missing_required_omitted"] = max(
                    0,
                    len(names) - MAX_MISSING_REQUIRED,
                )
        elif keyword == "additionalProperties":
            count = details.get("unexpected_property_count")
            if _bounded_int(count):
                safe_details["unexpected_property_count"] = count
        elif keyword == "type":
            for key in ("expected_type", "actual_type"):
                value = details.get(key)
                if isinstance(value, str) and value in _JSON_TYPES:
                    safe_details[key] = value
    elif isinstance(raw_path, str) and len(raw_path) <= 2048:
        match = _TEMPLATE_PATH.fullmatch(raw_path)
        if match is not None and code.startswith("ttp."):
            path = match.group("groups")
            attribute = match.group("attribute")
            if attribute is not None:
                path += "/@" + (attribute if attribute in {"name", "method"} else "*")
            result["path"] = path
    action = details.get("required_action")
    if isinstance(action, str) and action in _ACTIONS.get(code, set()):
        safe_details["required_action"] = action
    if code in {"ttp.invalid_xml", "ttp.incompatible_argument_pipe"}:
        for key in ("line", "column"):
            value = details.get(key)
            if _bounded_int(value):
                safe_details[key] = value
    if code == "ttp.worker_error":
        exception = details.get("exception_type")
        if isinstance(exception, str):
            safe_details["exception_type"] = (
                exception if exception in _EXCEPTION_TYPES else "OtherError"
            )
    return result


def _serialize(payload: dict[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=True, separators=(",", ":"))


def _feedback_block(
    *,
    state: dict[str, Any],
    issues: Sequence[Any],
    schema: Mapping[str, Any] | None,
    input_count: int,
) -> str:
    payload = {
        "feedback_version": 1,
        **state,
        "issues": [],
        "issues_total": len(issues),
        "issues_omitted": len(issues),
    }
    selected = payload["issues"]
    for issue in issues:
        if len(selected) >= MAX_FEEDBACK_ISSUES:
            break
        selected.append(_project_issue(issue, schema=schema, input_count=input_count))
        payload["issues_omitted"] = len(issues) - len(selected)
        if len(_serialize(payload).encode("utf-8")) > MAX_FEEDBACK_BYTES:
            selected.pop()
            payload["issues_omitted"] = len(issues) - len(selected)
            break
    return "<validation_feedback>\n" + _serialize(payload) + "\n</validation_feedback>"


def submission_feedback(
    session: GenerationSession,
    *,
    accepted: bool,
    candidate_updated: bool,
    returned_record_count: int,
    issues: Sequence[Any],
) -> str:
    return _feedback_block(
        state={
            "scope": "full_input_validation",
            "accepted": accepted,
            "expected_record_count": len(session.command_outputs),
            "returned_record_count": returned_record_count,
            "submissions_used": session.ttp_submissions,
            "remaining_submissions": max(
                0,
                session.max_ttp_submissions - session.ttp_submissions,
            ),
            "candidate_updated": candidate_updated,
            "retained_candidate_submission_index": (
                session.validated_ttp_submission_index
                if session.has_validated_ttp_candidate
                else None
            ),
        },
        issues=issues,
        schema=session.frozen_schema,
        input_count=len(session.command_outputs),
    )


def test_feedback(
    session: GenerationSession,
    *,
    parse_succeeded: bool,
    issues: Sequence[Any],
) -> str:
    return _feedback_block(
        state={
            "scope": "parse_only",
            "parse_succeeded": parse_succeeded,
            "tests_used": session.ttp_test_calls,
            "remaining_tests": max(
                0, session.max_ttp_test_calls - session.ttp_test_calls
            ),
        },
        issues=issues,
        schema=None,
        input_count=1,
    )
