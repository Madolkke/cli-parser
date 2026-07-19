"""Deterministic validation for the generated result JSON Schema."""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from typing import Any

from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError

from ..contracts import FieldEvidence, ValidationIssue

MAX_SCHEMA_BYTES = 64 * 1024
MAX_SCHEMA_DEPTH = 16
MAX_SCHEMA_PROPERTIES = 256
MAX_SCHEMA_EVIDENCE = 256
MAX_ENUM_VALUES = 128
MAX_VALIDATION_ISSUES = 100
MAX_FIELD_NAME_CHARS = 120
MAX_ISSUE_PATH_CHARS = 2_048

_DRAFT_2020_12 = "https://json-schema.org/draft/2020-12/schema"
_FIELD_NAME_RE = re.compile(r"^[a-z][a-z0-9]*(?:_[a-z0-9]+)*$")
_SCALAR_TYPES = {"string", "integer", "number", "boolean"}
_ALLOWED_TYPES = _SCALAR_TYPES | {"object", "array"}
_COMMON_KEYWORDS = {"type", "title", "description", "enum"}
_KEYWORDS_BY_TYPE = {
    "object": {"properties", "required", "additionalProperties"},
    "array": {"items", "minItems", "maxItems"},
    "string": {"minLength", "maxLength"},
    "integer": {
        "minimum",
        "maximum",
        "exclusiveMinimum",
        "exclusiveMaximum",
        "multipleOf",
    },
    "number": {
        "minimum",
        "maximum",
        "exclusiveMinimum",
        "exclusiveMaximum",
        "multipleOf",
    },
    "boolean": set(),
}
_ALWAYS_FORBIDDEN = {
    "$anchor",
    "$comment",
    "$defs",
    "$dynamicAnchor",
    "$dynamicRef",
    "$id",
    "$ref",
    "allOf",
    "anyOf",
    "contains",
    "contentEncoding",
    "contentMediaType",
    "contentSchema",
    "default",
    "dependentRequired",
    "dependentSchemas",
    "else",
    "examples",
    "format",
    "if",
    "not",
    "oneOf",
    "pattern",
    "patternProperties",
    "propertyNames",
    "then",
    "unevaluatedItems",
    "unevaluatedProperties",
}


def _issue(
    code: str,
    message: str,
    *,
    path: str | None = None,
    output_index: int | None = None,
    details: dict[str, Any] | None = None,
) -> ValidationIssue:
    return ValidationIssue(
        code=code,
        stage="schema",
        message=message,
        path=path,
        output_index=output_index,
        details=details or {},
    )


def _pointer(parts: Sequence[str | int]) -> str:
    if not parts:
        return "/"
    pointer = "/" + "/".join(
        str(part).replace("~", "~0").replace("/", "~1") for part in parts
    )
    return pointer[:MAX_ISSUE_PATH_CHARS]


def _schema_instance_pointer(parts: Sequence[str | int]) -> str:
    """Collapse concrete array indexes into one actionable schema path."""

    return _pointer(tuple("*" if isinstance(part, int) else part for part in parts))


def _json_size(schema: Mapping[str, Any]) -> int | None:
    try:
        encoded = json.dumps(
            schema,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
            allow_nan=False,
        ).encode("utf-8")
    except (RecursionError, TypeError, ValueError, UnicodeEncodeError):
        return None
    return len(encoded)


def _effective_limit(value: int, hard_limit: int, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError(f"{name} must be a positive integer")
    return min(value, hard_limit)


def _enum_matches_type(value: Any, schema_type: str) -> bool:
    if schema_type == "string":
        return isinstance(value, str)
    if schema_type == "boolean":
        return isinstance(value, bool)
    if schema_type == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if schema_type == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    return False


def _walk_schema(
    node: Any,
    *,
    schema_path: tuple[str | int, ...],
    field_path: tuple[str, ...],
    logical_depth: int,
    state: dict[str, Any],
) -> None:
    issues: list[ValidationIssue] = state["issues"]
    if len(issues) >= MAX_VALIDATION_ISSUES:
        return
    if not isinstance(node, Mapping):
        issues.append(
            _issue(
                "schema.node_not_object",
                "every result schema node must be an object",
                path=_pointer(schema_path),
            ),
        )
        return

    max_schema_depth: int = state["max_schema_depth"]
    max_schema_properties: int = state["max_schema_properties"]
    if logical_depth > max_schema_depth:
        issues.append(
            _issue(
                "schema.depth_exceeded",
                f"result schema nesting exceeds {max_schema_depth}",
                path=_pointer(schema_path),
            ),
        )
        return

    schema_type = node.get("type")
    if not isinstance(schema_type, str) or schema_type not in _ALLOWED_TYPES:
        issues.append(
            _issue(
                "schema.invalid_type",
                "each schema node must declare one supported JSON type",
                path=_pointer((*schema_path, "type")),
            ),
        )
        return

    allowed = _COMMON_KEYWORDS | _KEYWORDS_BY_TYPE[schema_type]
    if not schema_path:
        allowed = allowed | {"$schema"}
    for keyword in node:
        if keyword in _ALWAYS_FORBIDDEN:
            issues.append(
                _issue(
                    "schema.forbidden_keyword",
                    "result schema contains a forbidden JSON Schema keyword",
                    path=_pointer((*schema_path, keyword)),
                ),
            )
        elif keyword not in allowed:
            issues.append(
                _issue(
                    "schema.unsupported_keyword",
                    "result schema contains an unsupported JSON Schema keyword",
                    path=_pointer((*schema_path, keyword)),
                ),
            )

    if "$schema" in node and node["$schema"] != _DRAFT_2020_12:
        issues.append(
            _issue(
                "schema.wrong_draft",
                "result schema must use JSON Schema Draft 2020-12",
                path=_pointer((*schema_path, "$schema")),
            ),
        )

    enum = node.get("enum")
    if enum is not None:
        enum_path = _pointer((*schema_path, "enum"))
        if not isinstance(enum, list) or not enum:
            issues.append(
                _issue(
                    "schema.invalid_enum",
                    "enum must be a non-empty array",
                    path=enum_path,
                ),
            )
        elif len(enum) > MAX_ENUM_VALUES:
            issues.append(
                _issue(
                    "schema.enum_too_large",
                    f"enum cannot contain more than {MAX_ENUM_VALUES} values",
                    path=enum_path,
                ),
            )
        elif schema_type not in _SCALAR_TYPES or any(
            not _enum_matches_type(value, schema_type) for value in enum
        ):
            issues.append(
                _issue(
                    "schema.invalid_enum",
                    "enum is only supported for scalar values of the declared type",
                    path=enum_path,
                ),
            )

    if schema_type == "object":
        properties = node.get("properties")
        required = node.get("required")
        if not isinstance(properties, Mapping):
            issues.append(
                _issue(
                    "schema.properties_required",
                    "object schemas must declare a properties object",
                    path=_pointer((*schema_path, "properties")),
                ),
            )
            return
        if node.get("additionalProperties") is not False:
            issues.append(
                _issue(
                    "schema.object_not_closed",
                    "object schemas must set additionalProperties to false",
                    path=_pointer((*schema_path, "additionalProperties")),
                ),
            )
        property_names = list(properties)
        if (
            not isinstance(required, list)
            or any(not isinstance(name, str) for name in required)
            or len(required) != len(set(required))
            or set(required) != set(property_names)
        ):
            required_names = (
                {name for name in required if isinstance(name, str)}
                if isinstance(required, list)
                else set()
            )
            issues.append(
                _issue(
                    "schema.required_mismatch",
                    "required must contain every property exactly once",
                    path=_pointer((*schema_path, "required")),
                    details={
                        "missing_required": sorted(
                            set(property_names) - required_names,
                        ),
                        "unknown_required": sorted(
                            required_names - set(property_names),
                        ),
                    },
                ),
            )
        state["property_count"] += len(properties)
        if state["property_count"] > max_schema_properties:
            issues.append(
                _issue(
                    "schema.property_limit_exceeded",
                    f"result schema cannot exceed {max_schema_properties} properties",
                    path=_pointer((*schema_path, "properties")),
                ),
            )
            return
        for name, child in properties.items():
            child_schema_path = (*schema_path, "properties", str(name))
            if (
                not isinstance(name, str)
                or len(name) > MAX_FIELD_NAME_CHARS
                or not _FIELD_NAME_RE.fullmatch(name)
            ):
                issues.append(
                    _issue(
                        "schema.invalid_property_name",
                        "property names must be ASCII snake_case",
                        path=_pointer(child_schema_path),
                    ),
                )
                continue
            _walk_schema(
                child,
                schema_path=child_schema_path,
                field_path=(*field_path, name),
                logical_depth=logical_depth + 1,
                state=state,
            )
    elif schema_type == "array":
        if "items" not in node:
            issues.append(
                _issue(
                    "schema.items_required",
                    "array schemas must declare one items schema",
                    path=_pointer((*schema_path, "items")),
                ),
            )
            return
        _walk_schema(
            node["items"],
            schema_path=(*schema_path, "items"),
            field_path=(*field_path, "*"),
            logical_depth=logical_depth + 1,
            state=state,
        )
    else:
        if field_path:
            state["leaf_paths"].add(_pointer(field_path))


def validate_result_schema(
    schema: Mapping[str, Any],
    *,
    max_schema_bytes: int = MAX_SCHEMA_BYTES,
    max_schema_depth: int = MAX_SCHEMA_DEPTH,
    max_schema_properties: int = MAX_SCHEMA_PROPERTIES,
) -> list[ValidationIssue]:
    """Validate the safe, closed subset of Draft 2020-12 used by this project."""

    max_schema_bytes = _effective_limit(
        max_schema_bytes,
        MAX_SCHEMA_BYTES,
        "max_schema_bytes",
    )
    max_schema_depth = _effective_limit(
        max_schema_depth,
        MAX_SCHEMA_DEPTH,
        "max_schema_depth",
    )
    max_schema_properties = _effective_limit(
        max_schema_properties,
        MAX_SCHEMA_PROPERTIES,
        "max_schema_properties",
    )
    issues: list[ValidationIssue] = []
    if not isinstance(schema, Mapping):
        return [
            _issue(
                "schema.not_object",
                "result schema must be a JSON object",
                path="/",
            ),
        ]

    size = _json_size(schema)
    if size is None:
        return [
            _issue(
                "schema.not_json_serializable",
                "result schema must contain only finite JSON values",
                path="/",
            ),
        ]
    if size > max_schema_bytes:
        return [
            _issue(
                "schema.too_large",
                f"result schema exceeds {max_schema_bytes} UTF-8 bytes",
                path="/",
            ),
        ]

    if schema.get("type") != "object":
        issues.append(
            _issue(
                "schema.root_not_object",
                "result schema root type must be object",
                path="/type",
            ),
        )
    if "$schema" not in schema:
        issues.append(
            _issue(
                "schema.draft_required",
                "result schema must explicitly declare JSON Schema Draft 2020-12",
                path="/$schema",
            ),
        )

    state: dict[str, Any] = {
        "issues": issues,
        "leaf_paths": set(),
        "property_count": 0,
        "max_schema_depth": max_schema_depth,
        "max_schema_properties": max_schema_properties,
    }
    _walk_schema(
        schema,
        schema_path=(),
        field_path=(),
        logical_depth=1,
        state=state,
    )
    try:
        Draft202012Validator.check_schema(dict(schema))
    except (RecursionError, SchemaError):
        issues.append(
            _issue(
                "schema.invalid_draft_2020_12",
                "result schema is not valid JSON Schema Draft 2020-12",
                path="/",
            ),
        )
    if not state["leaf_paths"]:
        issues.append(
            _issue(
                "schema.no_leaf_fields",
                "result schema must describe at least one scalar leaf field",
                path="/",
            ),
        )
    return issues[:MAX_VALIDATION_ISSUES]


def schema_leaf_paths(
    schema: Mapping[str, Any],
    *,
    max_schema_depth: int = MAX_SCHEMA_DEPTH,
    max_schema_properties: int = MAX_SCHEMA_PROPERTIES,
) -> set[str]:
    """Return supported leaf paths after structural validation."""

    max_schema_depth = _effective_limit(
        max_schema_depth,
        MAX_SCHEMA_DEPTH,
        "max_schema_depth",
    )
    max_schema_properties = _effective_limit(
        max_schema_properties,
        MAX_SCHEMA_PROPERTIES,
        "max_schema_properties",
    )
    state: dict[str, Any] = {
        "issues": [],
        "leaf_paths": set(),
        "property_count": 0,
        "max_schema_depth": max_schema_depth,
        "max_schema_properties": max_schema_properties,
    }
    _walk_schema(
        schema,
        schema_path=(),
        field_path=(),
        logical_depth=1,
        state=state,
    )
    return set(state["leaf_paths"])


def validate_field_evidence(
    schema: Mapping[str, Any],
    evidence: Sequence[FieldEvidence],
    command_outputs: Sequence[str],
    *,
    max_schema_depth: int = MAX_SCHEMA_DEPTH,
    max_schema_properties: int = MAX_SCHEMA_PROPERTIES,
) -> list[ValidationIssue]:
    """Require at least one real source excerpt for every inferred leaf field."""

    if len(evidence) > MAX_SCHEMA_EVIDENCE:
        return [
            _issue(
                "schema.evidence_limit_exceeded",
                f"field evidence cannot exceed {MAX_SCHEMA_EVIDENCE} items",
                path="/",
            ),
        ]

    issues: list[ValidationIssue] = []
    leaf_paths = schema_leaf_paths(
        schema,
        max_schema_depth=max_schema_depth,
        max_schema_properties=max_schema_properties,
    )
    evidenced_paths: set[str] = set()
    submitted_paths: set[str] = set()

    for item in evidence:
        if item.path not in leaf_paths:
            issues.append(
                _issue(
                    "schema.evidence_unknown_path",
                    "field evidence path does not identify a schema leaf",
                    path=item.path,
                    output_index=item.output_index,
                ),
            )
            continue
        if item.path in submitted_paths:
            issues.append(
                _issue(
                    "schema.evidence_duplicate_path",
                    "submit exactly one evidence item for each schema leaf",
                    path=item.path,
                    output_index=item.output_index,
                ),
            )
            continue
        submitted_paths.add(item.path)
        if item.output_index >= len(command_outputs):
            issues.append(
                _issue(
                    "schema.evidence_output_index",
                    "field evidence references an unavailable command output",
                    path=item.path,
                    output_index=item.output_index,
                ),
            )
            continue
        if item.excerpt not in command_outputs[item.output_index]:
            matching_output_indexes = [
                index
                for index, output in enumerate(command_outputs)
                if item.excerpt in output
            ]
            issues.append(
                _issue(
                    "schema.evidence_not_found",
                    "field evidence excerpt was not found in the referenced "
                    "output; copy it exactly from that output or use an index "
                    "listed in matching_output_indexes",
                    path=item.path,
                    output_index=item.output_index,
                    details={
                        "matching_output_indexes": matching_output_indexes,
                        "required_action": (
                            "change_output_index"
                            if matching_output_indexes
                            else "replace_excerpt"
                        ),
                    },
                ),
            )
            continue
        evidenced_paths.add(item.path)

    for path in sorted(leaf_paths - evidenced_paths):
        issues.append(
            _issue(
                "schema.evidence_missing",
                "schema leaf is missing source evidence",
                path=path,
            ),
        )
    return issues[:MAX_VALIDATION_ISSUES]


def validate_schema_proposal(
    schema: Mapping[str, Any],
    evidence: Sequence[FieldEvidence],
    command_outputs: Sequence[str],
    *,
    max_schema_bytes: int = MAX_SCHEMA_BYTES,
    max_schema_depth: int = MAX_SCHEMA_DEPTH,
    max_schema_properties: int = MAX_SCHEMA_PROPERTIES,
) -> list[ValidationIssue]:
    """Validate a schema submission and its evidence before freezing it."""

    issues = validate_result_schema(
        schema,
        max_schema_bytes=max_schema_bytes,
        max_schema_depth=max_schema_depth,
        max_schema_properties=max_schema_properties,
    )
    if issues:
        return issues
    return validate_field_evidence(
        schema,
        evidence,
        command_outputs,
        max_schema_depth=max_schema_depth,
        max_schema_properties=max_schema_properties,
    )


def validate_records_against_schema(
    records: Sequence[Mapping[str, Any]],
    schema: Mapping[str, Any],
    *,
    max_schema_bytes: int = MAX_SCHEMA_BYTES,
    max_schema_depth: int = MAX_SCHEMA_DEPTH,
    max_schema_properties: int = MAX_SCHEMA_PROPERTIES,
) -> list[ValidationIssue]:
    """Validate parsed records without exposing their potentially secret values."""

    issues = validate_result_schema(
        schema,
        max_schema_bytes=max_schema_bytes,
        max_schema_depth=max_schema_depth,
        max_schema_properties=max_schema_properties,
    )
    if issues:
        return issues

    validator = Draft202012Validator(dict(schema))
    issues_by_output: list[list[ValidationIssue]] = []
    for output_index, record in enumerate(records):
        output_issues: list[ValidationIssue] = []
        seen_errors: set[tuple[str, str, str]] = set()
        for error in validator.iter_errors(record):
            path = _schema_instance_pointer(tuple(error.absolute_path))
            keyword = str(error.validator)
            details: dict[str, Any] = {"keyword": keyword}
            if (
                keyword == "required"
                and isinstance(error.instance, Mapping)
                and isinstance(error.validator_value, list)
            ):
                details["missing_required"] = sorted(
                    name
                    for name in error.validator_value
                    if isinstance(name, str) and name not in error.instance
                )
            if (
                keyword == "additionalProperties"
                and isinstance(error.instance, Mapping)
                and isinstance(error.schema, Mapping)
                and isinstance(error.schema.get("properties"), Mapping)
            ):
                allowed = set(error.schema["properties"])
                details["unexpected_property_count"] = sum(
                    name not in allowed for name in error.instance
                )
            signature = (
                path,
                keyword,
                json.dumps(details, sort_keys=True),
            )
            if signature in seen_errors:
                continue
            seen_errors.add(signature)
            output_issues.append(
                ValidationIssue(
                    code="schema.record_mismatch",
                    stage="acceptance",
                    message="parsed record does not satisfy the frozen result schema",
                    output_index=output_index,
                    path=path,
                    details=details,
                ),
            )
            if len(output_issues) >= MAX_VALIDATION_ISSUES:
                break
        issues_by_output.append(output_issues)

    # Interleave outputs so a noisy first sample cannot consume the full feedback
    # budget before later samples receive any actionable diagnostics.
    issue_index = 0
    while len(issues) < MAX_VALIDATION_ISSUES:
        added = False
        for output_issues in issues_by_output:
            if issue_index >= len(output_issues):
                continue
            issues.append(output_issues[issue_index])
            added = True
            if len(issues) >= MAX_VALIDATION_ISSUES:
                break
        if not added:
            break
        issue_index += 1
    return issues


__all__ = [
    "MAX_SCHEMA_BYTES",
    "MAX_SCHEMA_DEPTH",
    "MAX_SCHEMA_EVIDENCE",
    "MAX_SCHEMA_PROPERTIES",
    "schema_leaf_paths",
    "validate_field_evidence",
    "validate_records_against_schema",
    "validate_result_schema",
    "validate_schema_proposal",
]
