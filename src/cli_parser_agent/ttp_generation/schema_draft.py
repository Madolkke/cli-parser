"""Private, lightweight field drafts and deterministic Schema assembly.

Only naming and serialization are deterministic. Valid label references do not
prove complete labels, semantic ownership, or correct modeling of repeated rows.
Types, required flags, descriptions and supported constraints remain model choices.
"""

from __future__ import annotations

import json
import keyword
import re
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from .contracts import ValidationIssue
from .schema_draft_sources import SourceLine
from .validation.json_schema import (
    MAX_FIELD_NAME_CHARS,
    MAX_SCHEMA_BYTES,
    MAX_SCHEMA_DEPTH,
    MAX_SCHEMA_PROPERTIES,
    schema_draft_attribute_keywords,
    validate_result_schema,
)

MAX_DRAFT_REFERENCES = 1024
_DRAFT = "https://json-schema.org/draft/2020-12/schema"
_TYPES = {"string", "integer", "number", "boolean", "object", "array"}
_NAME = re.compile(r"^[a-z][a-z0-9]*(?:_[a-z0-9]+)*$")
_WORD = re.compile(r"[A-Za-z0-9_]")
_FALLBACK_REASONS = {
    "unlabeled",
    "ambiguous_source",
    "invalid_name",
    "conflict",
    "split_component",
}
_MESSAGES = {
    "invalid_shape": "draft must match the versioned field contract",
    "too_large": "draft exceeds the UTF-8 byte limit",
    "invalid_sources": "source lines must have unique valid identifiers",
    "invalid_reference": "label must reference one complete displayed source line",
    "ambiguous_reference": "label quote must occur exactly once in its source line",
    "partial_word": "label quote cannot start or end inside an ASCII word",
    "source_order": "label parts must follow source order within one input fragment",
    "reference_limit": "draft exceeds the label reference limit",
    "invalid_name": "field needs a legal name or a necessary semantic fallback",
    "unnecessary_fallback": "fallback must match the verifiable naming condition",
    "name_collision": "sibling fields must have distinct final names",
    "invalid_attribute": "attributes may contain only supported nonstructural keywords",
    "depth_exceeded": "compiled schema nesting exceeds the supported limit",
    "property_limit": "draft exceeds the supported property count",
}


@dataclass(frozen=True, slots=True)
class Compilation:
    schema: dict[str, Any] | None = field(repr=False)
    issues: tuple[ValidationIssue, ...]
    facts: dict[str, int]

    @property
    def accepted(self) -> bool:
        return self.schema is not None and not self.issues


def _issue(code: str, path: str = "/") -> ValidationIssue:
    return ValidationIssue(
        code=f"schema_draft.{code}",
        stage="schema",
        path=path,
        message=_MESSAGES[code],
        details={},
    )


class _Rejected(Exception):
    def __init__(self, code: str, path: str):
        self.issue = _issue(code, path)
        super().__init__(code)


def _reject(code: str, path: str) -> Any:
    raise _Rejected(code, path)


def _shape(value: Any, required: set[str], optional: set[str], path: str) -> dict:
    if (
        not isinstance(value, dict)
        or not required <= value.keys()
        or not value.keys() <= required | optional
    ):
        _reject("invalid_shape", path)
    return value


def _valid_name(name: str, node_type: str) -> bool:
    return bool(
        len(name) <= MAX_FIELD_NAME_CHARS
        and _NAME.fullmatch(name)
        and not keyword.iskeyword(name)
        and (name != "ignore" or node_type in {"object", "array"})
    )


def _normalize(parts: Sequence[str]) -> str:
    if any(not part.isascii() for part in parts):
        return ""
    return re.sub(r"[^a-z0-9]+", "_", " ".join(parts).lower()).strip("_")


def _limit(value: int, hard_limit: int) -> int:
    if type(value) is not int or value < 1:
        raise ValueError("schema limits must be positive integers")
    return min(value, hard_limit)


def _json_shape(raw: Any) -> bool:
    pending = [raw]
    visited = set()
    while pending:
        value = pending.pop()
        if type(value) in {str, int, float, bool, type(None)}:
            continue
        if not isinstance(value, (dict, list)):
            return False
        if id(value) in visited:
            continue  # json.dumps subsequently detects circular references.
        visited.add(id(value))
        if isinstance(value, dict):
            if any(not isinstance(key, str) for key in value):
                return False
            pending.extend(value.values())
        else:
            pending.extend(value)
    return True


@dataclass(slots=True)
class _Compiler:
    sources: dict[str, SourceLine]
    max_depth: int
    max_properties: int
    facts: dict[str, int]
    schema_paths: dict[str, str] = field(default_factory=dict)

    def labels(self, refs: Any, path: str) -> str:
        if not isinstance(refs, list) or not refs:
            _reject("invalid_shape", path)
        parts = []
        previous: tuple[int, int, int, int] | None = None
        for index, ref in enumerate(refs):
            ref_path = f"{path}/{index}"
            self.facts["reference_count"] += 1
            if self.facts["reference_count"] > MAX_DRAFT_REFERENCES:
                _reject("reference_limit", ref_path)
            _shape(ref, {"line_id", "quote"}, set(), ref_path)
            line_id, quote = ref["line_id"], ref["quote"]
            if (
                not isinstance(line_id, str)
                or not isinstance(quote, str)
                or not quote.strip()
                or "\n" in quote
                or "\r" in quote
            ):
                _reject("invalid_reference", ref_path)
            line = self.sources.get(line_id)
            if line is None or not line.complete:
                _reject("invalid_reference", ref_path)
            start = line.text.find(quote)
            if start < 0:
                _reject("invalid_reference", ref_path)
            if line.text.find(quote, start + 1) >= 0:
                _reject("ambiguous_reference", ref_path)
            end = start + len(quote)
            if (
                start
                and _WORD.fullmatch(line.text[start - 1])
                and _WORD.fullmatch(quote[0])
            ) or (
                end < len(line.text)
                and _WORD.fullmatch(line.text[end])
                and _WORD.fullmatch(quote[-1])
            ):
                _reject("partial_word", ref_path)
            position = (line.input_index, line.fragment_index, line.line_index, start)
            if previous is not None and (
                position[:2] != previous[:2]
                or position[2] < previous[2]
                or (position[2] == previous[2] and start < previous[3])
            ):
                _reject("source_order", ref_path)
            previous = (*position[:3], end)
            parts.append(quote)
        return _normalize(parts)

    def node(self, raw: Any, path: str, schema_path: str, depth: int) -> dict[str, Any]:
        if depth > self.max_depth:
            _reject("depth_exceeded", path)
        _shape(raw, {"type"}, {"fields", "items", "attributes"}, path)
        node_type = raw["type"]
        if not isinstance(node_type, str) or node_type not in _TYPES:
            _reject("invalid_shape", f"{path}/type")
        structural = (
            {"fields"}
            if node_type == "object"
            else ({"items"} if node_type == "array" else set())
        )
        _shape(raw, {"type"} | structural, {"attributes"}, path)
        self.schema_paths[schema_path] = path
        attributes = raw.get("attributes", {})
        if not isinstance(attributes, dict) or not attributes.keys() <= (
            schema_draft_attribute_keywords(node_type)
        ):
            _reject("invalid_attribute", f"{path}/attributes")
        result = {"type": node_type, **attributes}
        for attribute in attributes:
            self.schema_paths[f"{schema_path}/{attribute}"] = (
                f"{path}/attributes/{attribute}"
            )
        if node_type == "object":
            properties, required = self.fields(
                raw["fields"], f"{path}/fields", schema_path, depth
            )
            result.update(
                properties=properties, required=required, additionalProperties=False
            )
        elif node_type == "array":
            result["items"] = self.node(
                raw["items"], f"{path}/items", f"{schema_path}/items", depth + 1
            )
        return result

    def fields(
        self, raw: Any, path: str, schema_path: str, depth: int
    ) -> tuple[dict, list]:
        if not isinstance(raw, list):
            _reject("invalid_shape", path)
        self.facts["property_count"] += len(raw)
        if self.facts["property_count"] > self.max_properties:
            _reject("property_limit", path)
        names = []
        bases = []
        for index, item in enumerate(raw):
            item_path = f"{path}/{index}"
            _shape(item, {"name", "required", "node"}, set(), item_path)
            if type(item["required"]) is not bool or not isinstance(item["node"], dict):
                _reject("invalid_shape", item_path)
            node_type = item["node"].get("type")
            if not isinstance(node_type, str) or node_type not in _TYPES:
                _reject("invalid_shape", f"{item_path}/node/type")
            name = item["name"]
            if isinstance(name, dict) and "fallback" in name:
                _shape(name, {"fallback", "reason"}, {"source"}, f"{item_path}/name")
                if (
                    not isinstance(name["fallback"], str)
                    or not isinstance(name["reason"], str)
                    or name["reason"] not in _FALLBACK_REASONS
                    or not _valid_name(name["fallback"], node_type)
                ):
                    _reject("invalid_name", f"{item_path}/name")
                base = (
                    self.labels(name["source"], f"{item_path}/name/source")
                    if "source" in name
                    else None
                )
                if name["reason"] == "invalid_name" and (
                    base is None or _valid_name(base, node_type)
                ):
                    _reject("unnecessary_fallback", f"{item_path}/name")
                self.facts["fallback_name_count"] += 1
                self.facts[f"fallback_{name['reason']}_count"] += 1
                names.append(name["fallback"])
            else:
                _shape(name, {"source"}, set(), f"{item_path}/name")
                base = self.labels(name["source"], f"{item_path}/name/source")
                if not _valid_name(base, node_type):
                    _reject("invalid_name", f"{item_path}/name")
                self.facts["source_name_count"] += 1
                names.append(base)
            bases.append(base)
        counts = Counter(base for base in bases if base is not None)
        for index, (item, base) in enumerate(zip(raw, bases, strict=True)):
            if item["name"].get("reason") == "conflict" and (
                base is None
                or not _valid_name(base, item["node"]["type"])
                or counts[base] < 2
            ):
                _reject("unnecessary_fallback", f"{path}/{index}/name")
        seen = set()
        for index, name in enumerate(names):
            if name in seen:
                _reject("name_collision", f"{path}/{index}/name")
            seen.add(name)
        properties = {}
        required = []
        for index, (item, name) in enumerate(zip(raw, names, strict=True)):
            properties[name] = self.node(
                item["node"],
                f"{path}/{index}/node",
                f"{schema_path}/properties/{name}",
                depth + 1,
            )
            if item["required"]:
                required.append(name)
        return properties, required

    def safe_validation_issue(self, issue: ValidationIssue) -> ValidationIssue:
        schema_path = issue.path or ""
        parts = schema_path.split("/")
        while "/".join(parts) not in self.schema_paths and parts:
            parts.pop()
        path = self.schema_paths.get("/".join(parts), "/") or "/"
        return issue.model_copy(update={"path": path})


def compile_schema_draft(
    raw: Mapping[str, Any],
    sources: Sequence[SourceLine],
    *,
    max_schema_bytes: int = MAX_SCHEMA_BYTES,
    max_schema_depth: int = MAX_SCHEMA_DEPTH,
    max_schema_properties: int = MAX_SCHEMA_PROPERTIES,
) -> Compilation:
    """Compile one bounded draft without mutating input or exposing source text."""
    byte_limit = _limit(max_schema_bytes, MAX_SCHEMA_BYTES)
    depth_limit = _limit(max_schema_depth, MAX_SCHEMA_DEPTH)
    property_limit = _limit(max_schema_properties, MAX_SCHEMA_PROPERTIES)
    facts = {
        "property_count": 0,
        "source_name_count": 0,
        "fallback_name_count": 0,
        "reference_count": 0,
        "source_rejection_count": 0,
        "name_conflict_count": 0,
        **{f"fallback_{reason}_count": 0 for reason in sorted(_FALLBACK_REASONS)},
    }
    try:
        if not isinstance(raw, Mapping):
            _reject("invalid_shape", "/")
        if not _json_shape(dict(raw)):
            _reject("invalid_shape", "/")
        try:
            encoded = json.dumps(
                dict(raw), ensure_ascii=False, allow_nan=False, separators=(",", ":")
            )
            size = len(encoded.encode("utf-8"))
            data = json.loads(encoded)
        except (ValueError, TypeError, RecursionError, UnicodeEncodeError):
            _reject("invalid_shape", "/")
        facts["draft_bytes"] = size
        if size > byte_limit:
            _reject("too_large", "/")
        source_map = {}
        for line in sources:
            if (
                not isinstance(line, SourceLine)
                or type(line.input_index) is not int
                or line.input_index < 0
                or type(line.fragment_index) is not int
                or line.fragment_index < 0
                or type(line.line_index) is not int
                or line.line_index < 1
                or not isinstance(line.text, str)
                or type(line.complete) is not bool
                or line.line_id
                != f"i{line.input_index}f{line.fragment_index}l{line.line_index}"
                or line.line_id in source_map
            ):
                _reject("invalid_sources", "/")
            source_map[line.line_id] = line
        _shape(data, {"version", "fields"}, {"attributes"}, "/")
        if type(data["version"]) is not int or data["version"] != 1:
            _reject("invalid_shape", "/version")
        compiler = _Compiler(source_map, depth_limit, property_limit, facts)
        schema = compiler.node(
            {
                "type": "object",
                "fields": data["fields"],
                "attributes": data.get("attributes", {}),
            },
            "",
            "",
            1,
        )
        schema["$schema"] = _DRAFT
        issues = validate_result_schema(
            schema,
            max_schema_bytes=byte_limit,
            max_schema_depth=depth_limit,
            max_schema_properties=property_limit,
        )
        if issues:
            return Compilation(
                None,
                tuple(compiler.safe_validation_issue(issue) for issue in issues),
                facts,
            )
        return Compilation(schema, (), facts)
    except _Rejected as error:
        if error.issue.code in {
            "schema_draft.invalid_sources",
            "schema_draft.invalid_reference",
            "schema_draft.ambiguous_reference",
            "schema_draft.partial_word",
            "schema_draft.source_order",
            "schema_draft.reference_limit",
        }:
            facts["source_rejection_count"] = 1
        if error.issue.code == "schema_draft.name_collision":
            facts["name_conflict_count"] = 1
        return Compilation(None, (error.issue,), facts)
