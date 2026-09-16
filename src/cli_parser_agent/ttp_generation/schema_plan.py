"""Private, evidence-based schema plans and their deterministic compiler.

Source coordinates refer only to fragments actually sent to the schema model.
The compiler verifies coordinates and ownership, not the semantic correctness of
the model's labels, entity boundaries, or claimed completeness of enumeration.
Neither a plan nor a compilation result is a public generation contract.
"""

from __future__ import annotations

import json
import keyword
import re
from collections import Counter, defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from .contracts import ValidationIssue
from .validation.json_schema import validate_result_schema

MAX_PLAN_BYTES = 64 * 1024
MAX_PLAN_NODES = 256
MAX_PLAN_DEPTH = 16
MAX_PLAN_REFERENCES = 1024
_MAX_ISSUES = 24
_DRAFT = "https://json-schema.org/draft/2020-12/schema"
_NAME = re.compile(r"^[a-z][a-z0-9]*(?:_[a-z0-9]+)*$")
_INTEGER = re.compile(r"^(?:0|[1-9][0-9]*)$")
_ID = r"^[a-z][a-z0-9_]{0,63}$"


class _PlanModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)


class SourceRef(_PlanModel):
    """A half-open Unicode code-point interval in one displayed fragment."""

    input_index: int = Field(ge=0, lt=5)
    fragment_index: int = Field(ge=0)
    start: int = Field(ge=0)
    end: int = Field(ge=0)


class PlanInstance(_PlanModel):
    """An observed object or one observed element of a collection."""

    instance_id: str = Field(pattern=_ID)
    parent_instance_id: str = Field(pattern=_ID)
    spans: list[SourceRef] = Field(min_length=1, max_length=MAX_PLAN_REFERENCES)
    complete: bool = False


class PlanOccurrence(_PlanModel):
    """One field value in a parent instance, including explicitly empty slots."""

    parent_instance_id: str = Field(pattern=_ID)
    segments: list[SourceRef] = Field(min_length=1, max_length=MAX_PLAN_REFERENCES)
    empty_line: SourceRef | None = None


class EmptyCollection(_PlanModel):
    """An explicit empty table or section, without fabricating an element."""

    parent_instance_id: str = Field(pattern=_ID)
    marker: SourceRef


class PlanNode(_PlanModel):
    id: str = Field(pattern=_ID)
    parent_id: str = Field(default="root", pattern=_ID)
    kind: Literal["object", "collection", "value"]
    label_refs: list[SourceRef] = Field(default_factory=list)
    qualifier_refs: list[SourceRef] = Field(default_factory=list)
    fallback_name: str | None = Field(default=None, max_length=120)
    fallback_reason: (
        Literal["unlabeled", "invalid_name", "conflict", "split_component"] | None
    ) = None
    role: (
        Literal[
            "name",
            "identifier",
            "status",
            "count",
            "version",
            "build",
            "timestamp",
            "duration",
            "measurement",
            "text",
        ]
        | None
    ) = None
    capture: Literal["single", "joined_token", "joined_values", "multiline"] = "single"
    evidence_complete: bool = False
    instances: list[PlanInstance] = Field(default_factory=list)
    occurrences: list[PlanOccurrence] = Field(default_factory=list)
    empty_collections: list[EmptyCollection] = Field(default_factory=list)

    @model_validator(mode="after")
    def distinguish_values_and_containers(self) -> PlanNode:
        if self.kind == "value":
            if (
                self.role is None
                or self.instances
                or self.empty_collections
                or not self.occurrences
            ):
                raise ValueError("value shape")
        elif (
            self.role is not None
            or self.occurrences
            or not (self.instances or self.empty_collections)
            or self.capture != "single"
            or (self.kind != "collection" and self.empty_collections)
        ):
            raise ValueError("container shape")
        if (self.fallback_name is None) != (self.fallback_reason is None):
            raise ValueError("fallback shape")
        return self


class SchemaPlan(_PlanModel):
    version: Literal[1]
    nodes: list[PlanNode] = Field(min_length=1, max_length=MAX_PLAN_NODES)

    @model_validator(mode="before")
    @classmethod
    def version_is_integer(cls, data: Any) -> Any:
        if isinstance(data, Mapping) and type(data.get("version")) is not int:
            raise ValueError("integer version required")
        return data


@dataclass(frozen=True, slots=True)
class SourceFragment:
    """Trusted displayed fragment, or empty/incomplete input metadata.

    An empty fragment preserves an input whose sampling exposed no trustworthy
    evidence. It must be incomplete and cannot be the target of any reference.
    """

    input_index: int
    fragment_index: int
    text: str = field(repr=False)
    complete: bool = True


@dataclass(frozen=True, slots=True)
class SchemaPlanCompilation:
    schema: dict[str, Any] | None = field(repr=False)
    issues: tuple[ValidationIssue, ...]
    facts: dict[str, int | bool]

    @property
    def accepted(self) -> bool:
        return self.schema is not None and not self.issues


def _issue(code: str, path: str = "/", **details: int | bool) -> ValidationIssue:
    return ValidationIssue(
        code=f"schema_plan.{code}",
        stage="schema",
        path=path,
        message=_MESSAGES[code],
        details=details,
    )


_MESSAGES = {
    "invalid_shape": "plan must match the versioned modeling contract",
    "too_large": "plan exceeds the UTF-8 byte limit",
    "invalid_sources": "source fragments must have unique, valid coordinates",
    "invalid_reference": "reference must remain within one displayed fragment",
    "empty_label": "label and qualifier references must identify nonempty text",
    "label_order": "label parts must follow original order within one fragment",
    "reference_limit": "plan exceeds the evidence reference limit",
    "invalid_parent": "nodes must form a rooted tree of object or collection parents",
    "duplicate_id": "node and instance identifiers must be unique in their scopes",
    "depth_exceeded": "compiled schema nesting exceeds the supported limit",
    "invalid_instance": "instances must belong to an observed instance of their parent",
    "outside_parent": "evidence must be inside the referenced parent instance",
    "overlapping_instances": "distinct sibling instances must not overlap",
    "duplicate_occurrence": "each field has at most one occurrence per parent instance",
    "invalid_segments": "value segments must be ordered and disjoint in one input",
    "empty_slot_evidence": "empty slots need a zero-width value and complete line",
    "empty_collection_evidence": "empty collections need a parent-owned marker",
    "invalid_name": "a necessary semantic fallback must use a legal field name",
    "unnecessary_fallback": "semantic naming cannot replace a valid unambiguous label",
    "name_collision": "use evidenced qualifiers or necessary semantic names",
    "invalid_qualifier": "extra qualifiers require a sibling name collision",
    "invalid_split": "split names require shared labels and disjoint value evidence",
}


def _valid_name(value: str, kind: str) -> bool:
    return bool(
        len(value) <= 120
        and _NAME.fullmatch(value)
        and not keyword.iskeyword(value)
        and (value != "ignore" or kind != "value")
    )


def _normalize(parts: Sequence[str]) -> str:
    # Non-ASCII labels require explicit semantic naming, never transliteration.
    if any(not part.isascii() for part in parts):
        return ""
    return re.sub(r"[^a-z0-9]+", "_", " ".join(parts).lower()).strip("_")


def _ref_key(ref: SourceRef) -> tuple[int, int]:
    return ref.input_index, ref.fragment_index


def _contains(outer: SourceRef, inner: SourceRef) -> bool:
    return (
        _ref_key(outer) == _ref_key(inner)
        and outer.start <= inner.start <= inner.end <= outer.end
    )


def _overlaps(left: SourceRef, right: SourceRef) -> bool:
    return _ref_key(left) == _ref_key(right) and max(left.start, right.start) < min(
        left.end, right.end
    )


def _node_refs(node: PlanNode) -> list[SourceRef]:
    return [
        *node.label_refs,
        *node.qualifier_refs,
        *(ref for instance in node.instances for ref in instance.spans),
        *(empty.marker for empty in node.empty_collections),
        *(ref for occurrence in node.occurrences for ref in occurrence.segments),
        *(occ.empty_line for occ in node.occurrences if occ.empty_line is not None),
    ]


def _safe_shape_path(loc: tuple[Any, ...]) -> str:
    allowed = (
        set(SchemaPlan.model_fields)
        | set(PlanNode.model_fields)
        | set(PlanInstance.model_fields)
        | set(PlanOccurrence.model_fields)
        | set(EmptyCollection.model_fields)
        | set(SourceRef.model_fields)
    )
    parts = []
    for part in loc:
        if isinstance(part, int) or part in allowed:
            parts.append(str(part))
        else:
            break
    return "/" + "/".join(parts)


def _read_plan(raw: SchemaPlan | Mapping[str, Any]) -> SchemaPlan | ValidationIssue:
    try:
        data = (
            raw.model_dump(exclude_defaults=True)
            if isinstance(raw, SchemaPlan)
            else raw
        )
        size = len(
            json.dumps(
                data, ensure_ascii=False, allow_nan=False, separators=(",", ":")
            ).encode("utf-8")
        )
    except (TypeError, ValueError, RecursionError, UnicodeEncodeError):
        return _issue("invalid_shape")
    if size > MAX_PLAN_BYTES:
        return _issue("too_large", limit=MAX_PLAN_BYTES)
    try:
        return SchemaPlan.model_validate(data)
    except ValidationError as exc:
        return _issue("invalid_shape", _safe_shape_path(exc.errors()[0]["loc"]))


def _source_map(
    sources: Sequence[SourceFragment],
) -> dict[tuple[int, int], SourceFragment] | None:
    result = {}
    for source in sources:
        if (
            not isinstance(source, SourceFragment)
            or type(source.input_index) is not int
            or not 0 <= source.input_index < 5
            or type(source.fragment_index) is not int
            or source.fragment_index < 0
            or not isinstance(source.text, str)
            or type(source.complete) is not bool
            or (not source.text and source.complete)
        ):
            return None
        key = source.input_index, source.fragment_index
        if key in result:
            return None
        result[key] = source
    inputs = {key[0] for key in result}
    if not result or inputs != set(range(len(inputs))):
        return None
    return result


def compile_schema_plan(
    plan: SchemaPlan | Mapping[str, Any],
    sources: Sequence[SourceFragment],
) -> SchemaPlanCompilation:
    """Compile a modeling plan without mutating it or copying evidence to errors.

    The returned schema may contain generated descriptions; issues and facts are
    safe bounded structural diagnostics and never contain source text or values.
    """
    parsed = _read_plan(plan)
    if isinstance(parsed, ValidationIssue):
        return SchemaPlanCompilation(None, (parsed,), {})
    fragments = _source_map(sources)
    if fragments is None:
        return SchemaPlanCompilation(None, (_issue("invalid_sources"),), {})
    return _Compiler(parsed, fragments).compile()


class _Compiler:
    def __init__(self, plan: SchemaPlan, fragments: dict):
        self.plan = plan
        self.fragments = fragments
        self.issues: list[ValidationIssue] = []
        self.nodes = {node.id: node for node in plan.nodes}
        self.indices = {node.id: index for index, node in enumerate(plan.nodes)}
        self.names: dict[str, str] = {}
        self.children: dict[str, list[PlanNode]] = defaultdict(list)
        self.instances: dict[str, dict[str, PlanInstance]] = {}
        self.complete: dict[str, bool] = {}
        self.order: list[PlanNode] = []
        self.required: dict[str, bool] = {}
        self.fallback_count = 0
        self.reference_count = sum(len(_node_refs(node)) for node in plan.nodes)

    def error(self, code: str, node: PlanNode, suffix: str = "") -> None:
        if len(self.issues) < _MAX_ISSUES:
            self.issues.append(_issue(code, f"/nodes/{self.indices[node.id]}{suffix}"))

    def text(self, ref: SourceRef) -> str:
        return self.fragments[_ref_key(ref)].text[ref.start : ref.end]

    def compile(self) -> SchemaPlanCompilation:
        self.check_references_and_tree()
        if not self.issues:
            self.check_instances()
        if not self.issues:
            self.assign_names()
        schema = None
        if not self.issues:
            schema = self.object_schema("root")
            schema = {"$schema": _DRAFT, **schema}
            self.issues.extend(validate_result_schema(schema))
        facts = {
            "nodes": len(self.plan.nodes),
            "references": self.reference_count,
            "input_count": len({key[0] for key in self.fragments}),
            "incomplete_inputs": len(
                {
                    key[0]
                    for key, fragment in self.fragments.items()
                    if not fragment.complete
                }
            ),
            "fallback_names": self.fallback_count,
            "evidence_incomplete_nodes": sum(
                not value for value in self.complete.values()
            ),
            "enumeration_verified": False,
            "required_fields": sum(self.required.values()),
        }
        return SchemaPlanCompilation(
            schema if not self.issues else None,
            tuple(self.issues[:_MAX_ISSUES]),
            facts,
        )

    def check_references_and_tree(self) -> None:
        if len(self.nodes) != len(self.plan.nodes) or "root" in self.nodes:
            self.issues.append(_issue("duplicate_id"))
            return
        if self.reference_count > MAX_PLAN_REFERENCES:
            self.issues.append(_issue("reference_limit", limit=MAX_PLAN_REFERENCES))
            return
        for node in self.plan.nodes:
            for ref in _node_refs(node):
                source = self.fragments.get(_ref_key(ref))
                if (
                    source is None
                    or not source.text
                    or not 0 <= ref.start <= ref.end <= len(source.text)
                ):
                    self.error("invalid_reference", node)
            if any(
                ref.start == ref.end
                or (_ref_key(ref) in self.fragments and not self.text(ref).strip())
                for ref in [*node.label_refs, *node.qualifier_refs]
            ):
                self.error("empty_label", node)
            for refs in (node.label_refs, node.qualifier_refs):
                if any(
                    _ref_key(left) != _ref_key(right) or left.end > right.start
                    for left, right in zip(refs, refs[1:], strict=False)
                ):
                    self.error("label_order", node)
            parent = self.nodes.get(node.parent_id)
            if node.parent_id != "root" and (parent is None or parent.kind == "value"):
                self.error("invalid_parent", node, "/parent_id")
            self.children[node.parent_id].append(node)
        depths = {"root": 1}
        remaining = list(self.plan.nodes)
        while remaining:
            ready = [node for node in remaining if node.parent_id in depths]
            if not ready:
                self.error("invalid_parent", remaining[0], "/parent_id")
                break
            for node in ready:
                depths[node.id] = depths[node.parent_id] + (
                    2 if node.kind == "collection" else 1
                )
                if depths[node.id] > MAX_PLAN_DEPTH:
                    self.error("depth_exceeded", node)
                self.order.append(node)
                remaining.remove(node)

    def check_instances(self) -> None:
        by_input: dict[int, list[SourceRef]] = defaultdict(list)
        complete_input: dict[int, bool] = defaultdict(lambda: True)
        for (input_index, fragment_index), fragment in self.fragments.items():
            by_input[input_index].append(
                SourceRef(
                    input_index=input_index,
                    fragment_index=fragment_index,
                    start=0,
                    end=len(fragment.text),
                )
            )
            complete_input[input_index] &= fragment.complete
        roots = {
            f"input_{index}": PlanInstance(
                instance_id=f"input_{index}",
                parent_instance_id="root",
                spans=spans,
                complete=complete_input[index] and len(spans) == 1,
            )
            for index, spans in by_input.items()
        }
        self.instances["root"] = roots
        used_ids = set(roots)
        for node in self.order:
            parents = self.instances.get(node.parent_id, {})
            self.complete[node.id] = bool(
                node.evidence_complete
                and parents
                and all(instance.complete for instance in parents.values())
                and (node.parent_id == "root" or self.complete[node.parent_id])
                and all(instance.complete for instance in node.instances)
                and self.reference_count < MAX_PLAN_REFERENCES
            )
            seen: Counter[str] = Counter()
            if node.kind == "value":
                for occurrence in node.occurrences:
                    seen[occurrence.parent_instance_id] += 1
                    parent = parents.get(occurrence.parent_instance_id)
                    if parent is None:
                        self.error("invalid_instance", node)
                        continue
                    refs = occurrence.segments
                    if occurrence.empty_line is not None:
                        refs = [*refs, occurrence.empty_line]
                    self.inside_parent(node, parent, refs)
                    self.check_occurrence(node, occurrence)
                if any(count > 1 for count in seen.values()):
                    self.error("duplicate_occurrence", node)
            else:
                node_instances = {}
                for instance in node.instances:
                    seen[instance.parent_instance_id] += 1
                    if instance.instance_id in used_ids:
                        self.error("duplicate_id", node)
                    used_ids.add(instance.instance_id)
                    node_instances[instance.instance_id] = instance
                    parent = parents.get(instance.parent_instance_id)
                    if parent is None:
                        self.error("invalid_instance", node)
                        continue
                    if any(ref.start == ref.end for ref in instance.spans):
                        self.error("invalid_instance", node)
                    if any(
                        _overlaps(left, right)
                        for index, left in enumerate(instance.spans)
                        for right in instance.spans[index + 1 :]
                    ):
                        self.error("invalid_instance", node)
                    self.inside_parent(node, parent, instance.spans)
                    for other in node.instances:
                        if (
                            other is instance
                            or other.parent_instance_id != instance.parent_instance_id
                        ):
                            continue
                        if any(
                            _overlaps(left, right)
                            for left in instance.spans
                            for right in other.spans
                        ):
                            self.error("overlapping_instances", node)
                if node.kind == "object" and any(count > 1 for count in seen.values()):
                    self.error("invalid_instance", node)
                for empty in node.empty_collections:
                    parent = parents.get(empty.parent_instance_id)
                    if (
                        parent is None
                        or not self.text(empty.marker).strip()
                        or seen[empty.parent_instance_id]
                    ):
                        self.error("empty_collection_evidence", node)
                        continue
                    self.inside_parent(node, parent, [empty.marker])
                    seen[empty.parent_instance_id] += 1
                self.instances[node.id] = node_instances
            self.required[node.id] = bool(
                self.complete[node.id] and set(seen) == set(parents)
            )

    def inside_parent(
        self, node: PlanNode, parent: PlanInstance, refs: list[SourceRef]
    ) -> None:
        for ref in refs:
            if not any(_contains(span, ref) for span in parent.spans):
                self.error("outside_parent", node)

    def check_occurrence(self, node: PlanNode, occurrence: PlanOccurrence) -> None:
        refs = occurrence.segments
        if node.capture == "single" and len(refs) != 1:
            self.error("invalid_segments", node)
        if len({ref.input_index for ref in refs}) != 1:
            self.error("invalid_segments", node)
        if any(
            _ref_key(left) != _ref_key(right) or left.end > right.start
            for left, right in zip(refs, refs[1:], strict=False)
        ):
            # One value may not silently join across unobserved sampling gaps.
            self.error("invalid_segments", node)
        empty = any(ref.start == ref.end for ref in refs)
        line = occurrence.empty_line
        if empty:
            if (
                len(refs) != 1
                or line is None
                or line.start == line.end
                or not _contains(line, refs[0])
            ):
                self.error("empty_slot_evidence", node)
                return
            source = self.fragments[_ref_key(line)].text
            text = self.text(line)
            content = text.removesuffix("\n").removesuffix("\r")
            if (
                not content.strip()
                or "\n" in content
                or "\r" in content
                or refs[0].start > line.start + len(content)
                or (line.start != 0 and source[line.start - 1] != "\n")
                or (
                    line.end != len(source)
                    and source[line.end - 1] != "\n"
                    and source[line.end] != "\n"
                )
            ):
                self.error("empty_slot_evidence", node)
        elif line is not None:
            self.error("empty_slot_evidence", node)

    def assign_names(self) -> None:
        for siblings in self.children.values():
            base = {
                node.id: _normalize([self.text(ref) for ref in node.label_refs])
                for node in siblings
            }
            collisions = Counter(base.values())
            qualified = dict(base)
            for node in siblings:
                if node.qualifier_refs:
                    if not base[node.id] or collisions[base[node.id]] < 2:
                        self.error("invalid_qualifier", node)
                    qualified[node.id] = _normalize(
                        [
                            *(self.text(ref) for ref in node.qualifier_refs),
                            *(self.text(ref) for ref in node.label_refs),
                        ]
                    )
            qualified_counts = Counter(qualified.values())
            for node in siblings:
                name = qualified[node.id]
                reason = node.fallback_reason
                if reason:
                    allowed = (
                        (reason == "unlabeled" and not node.label_refs)
                        or (
                            reason == "invalid_name"
                            and bool(node.label_refs)
                            and not _valid_name(name, node.kind)
                        )
                        or (
                            reason == "conflict"
                            and bool(name)
                            and qualified_counts[name] > 1
                        )
                        or (
                            reason == "split_component"
                            and self.valid_split(node, siblings)
                        )
                    )
                    if not allowed:
                        self.error("unnecessary_fallback", node)
                    name = node.fallback_name or ""
                    self.fallback_count += 1
                if not _valid_name(name, node.kind):
                    self.error("invalid_name", node)
                self.names[node.id] = name
            if len({self.names[node.id] for node in siblings}) != len(siblings):
                self.error("name_collision", siblings[0])

    def valid_split(self, node: PlanNode, siblings: list[PlanNode]) -> bool:
        if node.kind != "value" or not node.label_refs:
            return False
        partners = [
            other
            for other in siblings
            if other.id != node.id
            and other.kind == "value"
            and other.label_refs == node.label_refs
        ]
        if not partners:
            return False
        for other in partners:
            for left in node.occurrences:
                for right in other.occurrences:
                    if left.parent_instance_id == right.parent_instance_id and any(
                        _overlaps(a, b) for a in left.segments for b in right.segments
                    ):
                        self.error("invalid_split", node)
                        return False
        return True

    def object_schema(self, parent_id: str) -> dict[str, Any]:
        properties = {}
        required = []
        for node in sorted(
            self.children[parent_id], key=lambda item: self.names[item.id]
        ):
            name = self.names[node.id]
            if node.kind == "value":
                schema_type = "string"
                if (
                    node.role == "count"
                    and self.complete[node.id]
                    and node.capture == "single"
                    and all(
                        _INTEGER.fullmatch(self.text(occ.segments[0]))
                        for occ in node.occurrences
                    )
                ):
                    schema_type = "integer"
                shape = {
                    "type": schema_type,
                    "description": self.description(node, schema_type),
                }
            elif node.kind == "object":
                shape = self.object_schema(node.id)
            else:
                shape = {"type": "array", "items": self.object_schema(node.id)}
            properties[name] = shape
            if self.required[node.id]:
                required.append(name)
        result = {
            "type": "object",
            "properties": properties,
            "additionalProperties": False,
        }
        if required:
            result["required"] = required
        return result

    def description(self, node: PlanNode, schema_type: str) -> str:
        label = _normalize([self.text(ref) for ref in node.label_refs])
        origin = (
            f"原文标签 {label}" if _valid_name(label, node.kind) else "所标记的原文值槽"
        )
        role = {
            "name": "名称",
            "identifier": "标识符",
            "status": "状态",
            "count": "计数",
            "version": "版本",
            "build": "构建标识",
            "timestamp": "时间戳",
            "duration": "时长",
            "measurement": "度量值",
            "text": "文本",
        }[node.role]
        if schema_type == "integer":
            return f"{origin}对应的{role}，按已观察到的非负十进制整数转换。"
        capture = {
            "single": "保留完整业务值及值内符号",
            "joined_token": "仅将同一词项的视觉折行片段以空分隔符连接",
            "joined_values": "纵向列举值用单个空格连接，保留词项内部空格和符号",
            "multiline": "自由文本保留行界，以换行连接",
        }[node.capture]
        return (
            f"{origin}对应的{role}；{capture}；"
            "明确空槽保留空字符串，所属行不存在才省略。"
        )
