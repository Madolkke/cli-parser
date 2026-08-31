"""Strict local contracts and deterministic scoring for Agent evaluations."""

from __future__ import annotations

import hashlib
import json
import math
import re
import tomllib
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Literal, cast

from cli_parser_agent import GenerationPolicy, GenerationResult
from cli_parser_agent.ttp_generation.validation import (
    parse_ttp_template,
    validate_records_against_schema,
    validate_result_schema,
    validate_ttp_template,
)

MANIFEST_VERSION = 1
MAX_INPUTS = 1
TTP_TEMPLATE_MANIFEST_VERSION = 1
MAX_TTP_TEMPLATE_INPUTS = 5
TEST_SET_MANIFEST_VERSION = 1
TEST_SET_MAX_INPUTS = 5
TEST_SET_TEMPLATE_MAX_BYTES = 64 * 1024
SEMANTIC_PILOT_SUITE = "semantic-pilot"
DATASET_REGISTRY_VERSION = 1
MAX_INPUT_BYTES = 1024 * 1024
SUPPORTED_NODE_TYPES = frozenset(
    {"object", "array", "string", "integer", "number", "boolean"},
)

_ID_RE = re.compile(r"^[a-z][a-z0-9]*(?:[._-][a-z0-9]+)*$")
_TAG_RE = re.compile(r"^[a-z][a-z0-9]*(?:[-_][a-z0-9]+)*$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_FIELD_RE = re.compile(r"^[a-z][a-z0-9_]*$")
_ANSI_RE = re.compile(r"\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07]*(?:\x07|\x1b\\))")
_PAGER_RE = re.compile(
    r"(?:--+\s*(?:more|\(\s*more\s*\))\s*--+|\bpress\s+(?:any\s+key|"
    r"enter|space)\s+to\s+continue\b)",
    re.IGNORECASE,
)
_CREDENTIAL_PATTERNS = (
    re.compile(r"-----BEGIN (?:OPENSSH |RSA |EC |DSA )?PRIVATE KEY-----"),
    re.compile(r"\bAuthorization\s*:\s*(?:Basic|Bearer)\s+\S+", re.IGNORECASE),
    re.compile(
        r"\b(?:api[_ -]?key|access[_ -]?token|auth[_ -]?token)\s*[:=]\s*\S+",
        re.IGNORECASE,
    ),
    re.compile(r"\b(?:password|passwd|pre-shared-key)\s*[:=]\s*\S+", re.I),
    re.compile(r"^\s*(?:enable\s+)?secret\s+\S+", re.I | re.MULTILINE),
    re.compile(r"^\s*snmp-server\s+community\s+\S+", re.I | re.MULTILINE),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"\bhttps?://[^\s/:]+:[^\s/@]+@", re.IGNORECASE),
)

JsonObject = dict[str, Any]
NodeType = Literal["object", "array", "string", "integer", "number", "boolean"]

# Issue domains are deliberately coarse.  The evaluation summary is allowed to
# retain issue codes, but must not retain their human-readable messages or any
# candidate/input payloads.
_ISSUE_DOMAIN_PREFIXES = frozenset(
    {
        "agent",
        "acceptance",
        "budget",
        "generation",
        "model",
        "records",
        "runner",
        "schema",
        "telemetry",
        "ttp",
        "worker",
    },
)
_SAFE_ISSUE_CODE_RE = re.compile(r"^[a-z][a-z0-9_]*(?:\.[a-z0-9_]+)*$")
_REVIEW_LABELS = frozenset({"reasonable", "repairable", "unreasonable"})
_REVIEW_PHASES = frozenset({"schema", "ttp"})
_REVIEW_DIMENSION_RE = re.compile(r"^[a-z][a-z0-9_]{0,31}$")
_REVIEW_VALUE_RE = re.compile(r"^[a-z][a-z0-9_-]{0,31}$")


class HarnessError(ValueError):
    """A bounded, safe-to-display evaluation definition error."""


@dataclass(frozen=True, slots=True)
class EvaluationInput:
    path: str
    sha256: str
    absolute_path: Path
    text: str


@dataclass(frozen=True, slots=True)
class SchemaNode:
    path: str
    type: NodeType
    required: bool

    def as_dict(self) -> dict[str, Any]:
        return {"path": self.path, "type": self.type, "required": self.required}


@dataclass(frozen=True, slots=True)
class EvaluationTarget:
    records: tuple[JsonObject, ...]
    schema_contract: tuple[SchemaNode, ...]
    path: str
    sha256: str

    def as_datapoint_target(self) -> dict[str, Any]:
        return {
            "records": list(self.records),
            "schema_contract": [node.as_dict() for node in self.schema_contract],
        }


@dataclass(frozen=True, slots=True)
class EvaluationCase:
    id: str
    command: str
    suites: tuple[str, ...]
    tags: tuple[str, ...]
    inputs: tuple[EvaluationInput, ...]
    target: EvaluationTarget


@dataclass(frozen=True, slots=True)
class EvaluationManifest:
    version: int
    sha256: str
    cases: tuple[EvaluationCase, ...]


@dataclass(frozen=True, slots=True)
class TtpTemplateTarget:
    """Expected records for a caller-supplied TTP-only schema case."""

    records: tuple[JsonObject, ...]
    path: str
    sha256: str


@dataclass(frozen=True, slots=True)
class TtpTemplateCase:
    """One external-schema TTP-only evaluation case."""

    id: str
    suites: tuple[str, ...]
    tags: tuple[str, ...]
    schema: JsonObject
    schema_path: str
    schema_sha256: str
    inputs: tuple[EvaluationInput, ...]
    target: TtpTemplateTarget


@dataclass(frozen=True, slots=True)
class TtpTemplateManifest:
    """Fully preflighted external-schema TTP-only test manifest."""

    version: int
    path: Path
    sha256: str
    cases: tuple[TtpTemplateCase, ...]


@dataclass(frozen=True, slots=True)
class TestSetCase:
    """One self-contained four-part command-output test set."""

    id: str
    command: str
    suites: tuple[str, ...]
    tags: tuple[str, ...]
    path: str
    absolute_path: Path
    inputs: tuple[EvaluationInput, ...]
    schema: JsonObject
    template: str
    expected_records: tuple[JsonObject, ...]
    file_sha256: Mapping[str, Any]
    original_input_indices: tuple[int, ...] = ()


@dataclass(frozen=True, slots=True)
class TestSetManifest:
    """Strict index of independent four-part test sets."""

    version: int
    path: Path
    sha256: str
    cases: tuple[TestSetCase, ...]


@dataclass(frozen=True, slots=True)
class DatasetFileSpec:
    """A file declared by the TOML dataset registry."""

    file: str
    sha256: str


@dataclass(frozen=True, slots=True)
class DatasetRegistryEntry:
    """Registry metadata and filesystem state for one dataset."""

    id: int
    name: str
    command: str
    platform: str
    source: str
    tags: tuple[str, ...]
    absolute_path: Path
    inputs: tuple[DatasetFileSpec, ...]
    default_input: str | None
    default_input_index: int | None
    template: DatasetFileSpec | None
    schema: DatasetFileSpec | None
    expected: DatasetFileSpec | None
    stage: Literal["inputs-only", "template", "complete"]
    present_files: tuple[str, ...]
    missing_files: tuple[str, ...]
    input_texts: tuple[EvaluationInput, ...]
    template_text: str | None
    registry_errors: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class DatasetRegistry:
    """The sole TOML registry used by the standard test-set runner."""

    version: int
    path: Path
    sha256: str
    datasets: tuple[DatasetRegistryEntry, ...]


@dataclass(frozen=True, slots=True)
class DatasetPreflightReport:
    """Safe, payload-free status for one registry entry."""

    dataset: DatasetRegistryEntry
    status: Literal["pending", "passed", "failed"]
    input_scope: Literal["default", "full"] = "default"
    selected_input_indices: tuple[int, ...] = ()
    errors: tuple[str, ...] = ()
    case: TestSetCase | None = None
    template_inputs_passed: int = 0
    baseline_exact: bool | None = None
    template_smoke_results: tuple[dict[str, Any], ...] = ()

    def as_dict(self) -> dict[str, Any]:
        entry = self.dataset
        selected_inputs = [
            {
                "input_index": index,
                "display_number": index + 1,
                "file": entry.inputs[index].file,
            }
            for index in self.selected_input_indices
        ]
        return {
            "id": entry.id,
            "name": entry.name,
            "command": entry.command,
            "platform": entry.platform,
            "source": entry.source,
            "tags": list(entry.tags),
            "stage": entry.stage,
            "status": self.status,
            "input_count": len(entry.inputs),
            "input_scope": self.input_scope,
            "default_input": entry.default_input,
            "selected_input": (
                selected_inputs[0]["file"] if len(selected_inputs) == 1 else None
            ),
            "selected_input_count": len(selected_inputs),
            "selected_inputs": selected_inputs,
            "present_files": list(entry.present_files),
            "missing_files": list(entry.missing_files),
            "eligible": {
                "baseline": self.status == "passed"
                and entry.stage in {"template", "complete"},
                "ttp_only": self.status == "passed" and entry.stage == "complete",
            },
            "template_inputs_passed": self.template_inputs_passed,
            "template_smoke": list(self.template_smoke_results),
            "baseline_exact": self.baseline_exact,
            "errors": list(self.errors),
        }


def _is_line_text_placeholder_schema(schema: Mapping[str, Any]) -> bool:
    """Return whether a schema is the temporary lines[].text migration shape."""

    properties = schema.get("properties")
    if not isinstance(properties, Mapping) or set(properties) != {"lines"}:
        return False
    lines = properties.get("lines")
    if not isinstance(lines, Mapping) or lines.get("type") != "array":
        return False
    items = lines.get("items")
    if not isinstance(items, Mapping) or items.get("type") != "object":
        return False
    item_properties = items.get("properties")
    return isinstance(item_properties, Mapping) and set(item_properties) == {"text"}


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise HarnessError(f"JSON contains duplicate object key: {key}")
        result[key] = value
    return result


def _read_json(path: Path, label: str) -> tuple[Any, bytes]:
    try:
        payload = path.read_bytes()
    except OSError as error:
        raise HarnessError(f"{label} could not be read") from error
    if payload.startswith(b"\xef\xbb\xbf"):
        raise HarnessError(f"{label} must not contain a UTF-8 BOM")
    try:
        value = json.loads(
            payload.decode("utf-8", errors="strict"),
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=lambda value: (_raise_invalid_constant(value)),
        )
    except HarnessError:
        raise
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise HarnessError(f"{label} is not strict UTF-8 JSON") from error
    return value, payload


def _raise_invalid_constant(value: str) -> None:
    raise HarnessError(f"JSON contains unsupported numeric constant: {value}")


def _require_exact_keys(value: Mapping[str, Any], keys: set[str], label: str) -> None:
    missing = keys - value.keys()
    extra = value.keys() - keys
    if missing:
        raise HarnessError(f"{label} is missing keys: {', '.join(sorted(missing))}")
    if extra:
        raise HarnessError(f"{label} has unsupported keys: {', '.join(sorted(extra))}")


def _require_string(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise HarnessError(f"{label} must be a non-empty string")
    return value


def _string_list(
    value: Any,
    label: str,
    *,
    pattern: re.Pattern[str],
) -> tuple[str, ...]:
    if not isinstance(value, list) or not value:
        raise HarnessError(f"{label} must be a non-empty array")
    result: list[str] = []
    for index, item in enumerate(value):
        text = _require_string(item, f"{label}[{index}]")
        if not pattern.fullmatch(text):
            raise HarnessError(f"{label}[{index}] has an unsupported identifier")
        result.append(text)
    if len({item.casefold() for item in result}) != len(result):
        raise HarnessError(f"{label} must not contain duplicates")
    return tuple(result)


def _optional_string_list(
    value: Any,
    label: str,
    *,
    pattern: re.Pattern[str],
) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, list):
        raise HarnessError(f"{label} must be an array")
    result: list[str] = []
    for index, item in enumerate(value):
        text = _require_string(item, f"{label}[{index}]")
        if not pattern.fullmatch(text):
            raise HarnessError(f"{label}[{index}] has an unsupported identifier")
        result.append(text)
    if len({item.casefold() for item in result}) != len(result):
        raise HarnessError(f"{label} must not contain duplicates")
    return tuple(result)


def _safe_repo_path(root: Path, value: Any, label: str) -> tuple[str, Path]:
    text = _require_string(value, label)
    if "\\" in text or ":" in text:
        raise HarnessError(f"{label} must use a relative POSIX path")
    relative = PurePosixPath(text)
    if (
        relative.is_absolute()
        or not relative.parts
        or any(part in {"", ".", ".."} for part in relative.parts)
    ):
        raise HarnessError(f"{label} must be a traversal-free relative path")
    resolved_root = root.resolve()
    candidate = resolved_root.joinpath(*relative.parts).resolve()
    if not candidate.is_relative_to(resolved_root):
        raise HarnessError(f"{label} resolves outside the project root")
    return text, candidate


def _sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _validate_sha256(value: Any, label: str) -> str:
    text = _require_string(value, label)
    if not _SHA256_RE.fullmatch(text):
        raise HarnessError(f"{label} must be a lowercase SHA-256")
    return text


def _read_input(path: Path, expected_sha256: str, command: str, label: str) -> str:
    if not path.is_file():
        raise HarnessError(f"{label} does not identify a file")
    try:
        payload = path.read_bytes()
    except OSError as error:
        raise HarnessError(f"{label} could not be read") from error
    if _sha256(payload) != expected_sha256:
        raise HarnessError(f"{label} SHA-256 does not match")
    if not payload or len(payload) > MAX_INPUT_BYTES:
        raise HarnessError(f"{label} must contain 1 to {MAX_INPUT_BYTES} bytes")
    if payload.startswith(b"\xef\xbb\xbf"):
        raise HarnessError(f"{label} must not contain a UTF-8 BOM")
    try:
        text = payload.decode("utf-8", errors="strict")
    except UnicodeDecodeError as error:
        raise HarnessError(f"{label} is not strict UTF-8") from error
    issues: list[str] = []
    if not text.strip():
        issues.append("empty content")
    if "\r" in text:
        issues.append("CR newlines")
    if "\x00" in text:
        issues.append("NUL characters")
    if any(ord(char) < 32 and char not in {"\t", "\n"} for char in text):
        issues.append("C0 control characters")
    if _ANSI_RE.search(text) or "\x1b" in text:
        issues.append("ANSI terminal escapes")
    if _PAGER_RE.search(text):
        issues.append("terminal pager markers")
    normalized_command = " ".join(command.split()).casefold()
    if any(
        " ".join(line.strip().split()).casefold() == normalized_command
        for line in text.splitlines()
        if line.strip()
    ):
        issues.append("command echo")
    if any(pattern.search(text) for pattern in _CREDENTIAL_PATTERNS):
        issues.append("credential pattern")
    if issues:
        raise HarnessError(f"{label} failed preflight: {', '.join(issues)}")
    return text


def _validate_schema_path(path: Any, label: str) -> str:
    text = _require_string(path, label)
    if text == "/":
        return text
    if not text.startswith("/") or text.endswith("/"):
        raise HarnessError(f"{label} must be '/' or a non-empty absolute path")
    parts = text[1:].split("/")
    if parts[0] == "*":
        raise HarnessError(f"{label} root must be an object")
    if any(part != "*" and not _FIELD_RE.fullmatch(part) for part in parts):
        raise HarnessError(f"{label} uses unsupported path segments")
    return text


def _parent_path(path: str) -> str | None:
    if path == "/":
        return None
    parts = path[1:].split("/")
    if len(parts) == 1:
        return "/"
    return "/" + "/".join(parts[:-1])


def _json_type(value: Any) -> NodeType:
    if isinstance(value, dict):
        return "object"
    if isinstance(value, list):
        return "array"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int):
        return "integer"
    if isinstance(value, float):
        return "number"
    if isinstance(value, str):
        return "string"
    raise HarnessError("target contains a non-JSON value")


def _validate_contract(
    nodes: tuple[SchemaNode, ...],
    records: tuple[JsonObject, ...],
) -> None:
    declared: dict[str, SchemaNode] = {}
    for node in nodes:
        if node.path in declared:
            raise HarnessError(f"schema contract contains duplicate path: {node.path}")
        declared[node.path] = node
    root = declared.get("/")
    if root != SchemaNode(path="/", type="object", required=False):
        raise HarnessError("schema contract root must be a non-required object")
    for path, node in declared.items():
        parent_path = _parent_path(path)
        if parent_path is None:
            continue
        parent = declared.get(parent_path)
        if parent is None:
            raise HarnessError(f"schema contract is missing parent path: {parent_path}")
        if path.endswith("/*"):
            if parent.type != "array" or node.required:
                raise HarnessError(f"array item contract is invalid at {path}")
        elif parent.type != "object":
            raise HarnessError(f"object property contract is invalid at {path}")

    children: dict[str, dict[str, SchemaNode]] = {}
    for path, node in declared.items():
        parent_path = _parent_path(path)
        if parent_path is None or path.endswith("/*"):
            continue
        name = path.rsplit("/", 1)[-1]
        children.setdefault(parent_path, {})[name] = node

    observed: set[str] = set()

    def validate_value(value: Any, path: str) -> None:
        node = declared.get(path)
        if node is None:
            raise HarnessError(f"target record contains an undeclared path: {path}")
        actual_type = _json_type(value)
        if actual_type != node.type:
            raise HarnessError(
                f"target record type does not match schema contract at {path}",
            )
        observed.add(path)

        if isinstance(value, dict):
            if not value:
                raise HarnessError(f"target contains an empty object at {path}")
            expected = children.get(path, {})
            for name, child_node in expected.items():
                if child_node.required and name not in value:
                    child_path = f"/{name}" if path == "/" else f"{path}/{name}"
                    raise HarnessError(
                        f"target record is missing a required path: {child_path}",
                    )
            for name, child in value.items():
                if not _FIELD_RE.fullmatch(name):
                    raise HarnessError(
                        f"target field is not ASCII snake_case: {name}",
                    )
                child_path = f"/{name}" if path == "/" else f"{path}/{name}"
                validate_value(child, child_path)
        elif isinstance(value, list):
            if not value:
                raise HarnessError(f"target contains an empty array at {path}")
            item_path = f"{path}/*"
            if item_path not in declared:
                raise HarnessError(
                    f"schema contract is missing array item path: {item_path}",
                )
            for child in value:
                validate_value(child, item_path)

    for record in records:
        validate_value(record, "/")

    unobserved = sorted(declared.keys() - observed)
    if unobserved:
        raise HarnessError(
            "schema contract contains paths absent from expected records: "
            + ",".join(unobserved),
        )


def _walk_scalars(value: Any) -> list[str]:
    if isinstance(value, dict):
        return [scalar for child in value.values() for scalar in _walk_scalars(child)]
    if isinstance(value, list):
        return [scalar for child in value for scalar in _walk_scalars(child)]
    if isinstance(value, str):
        return [value]
    return []


def _load_target(
    root: Path,
    raw_target: Any,
    inputs: tuple[EvaluationInput, ...],
    label: str,
) -> EvaluationTarget:
    if not isinstance(raw_target, dict):
        raise HarnessError(f"{label} must be an object")
    _require_exact_keys(raw_target, {"path", "sha256"}, label)
    path_text, path = _safe_repo_path(root, raw_target["path"], f"{label}.path")
    expected_sha256 = _validate_sha256(raw_target["sha256"], f"{label}.sha256")
    value, payload = _read_json(path, f"{label}.file")
    if _sha256(payload) != expected_sha256:
        raise HarnessError(f"{label}.file SHA-256 does not match")
    if not isinstance(value, dict):
        raise HarnessError(f"{label}.file root must be an object")
    _require_exact_keys(value, {"records", "schema_contract"}, f"{label}.file")
    raw_records = value["records"]
    if not isinstance(raw_records, list) or len(raw_records) != len(inputs):
        raise HarnessError(f"{label}.records must correspond one-to-one with inputs")
    if any(not isinstance(record, dict) for record in raw_records):
        raise HarnessError(f"{label}.records must contain only objects")
    records = tuple(raw_records)

    raw_nodes = value["schema_contract"]
    if not isinstance(raw_nodes, list) or not raw_nodes:
        raise HarnessError(f"{label}.schema_contract must be a non-empty array")
    nodes: list[SchemaNode] = []
    for index, raw_node in enumerate(raw_nodes):
        node_label = f"{label}.schema_contract[{index}]"
        if not isinstance(raw_node, dict):
            raise HarnessError(f"{node_label} must be an object")
        _require_exact_keys(raw_node, {"path", "type", "required"}, node_label)
        path_value = _validate_schema_path(raw_node["path"], f"{node_label}.path")
        type_value = raw_node["type"]
        if type_value not in SUPPORTED_NODE_TYPES:
            raise HarnessError(f"{node_label}.type is unsupported")
        required = raw_node["required"]
        if not isinstance(required, bool):
            raise HarnessError(f"{node_label}.required must be a boolean")
        nodes.append(SchemaNode(path_value, type_value, required))
    target = EvaluationTarget(
        records=records,
        schema_contract=tuple(nodes),
        path=path_text,
        sha256=expected_sha256,
    )
    _validate_contract(target.schema_contract, target.records)
    for index, record in enumerate(target.records):
        for scalar in _walk_scalars(record):
            if scalar not in inputs[index].text:
                raise HarnessError(
                    f"{label}.records[{index}] contains a string absent from input",
                )
    return target


def load_evaluation_manifest(
    project_root: Path,
    manifest_path: Path,
) -> EvaluationManifest:
    """Load and fully preflight a repository-backed evaluation manifest."""

    root = project_root.resolve()
    path = manifest_path.resolve()
    if not path.is_relative_to(root):
        raise HarnessError("manifest must be located inside the project root")
    raw, payload = _read_json(path, "evaluation manifest")
    if not isinstance(raw, dict):
        raise HarnessError("evaluation manifest root must be an object")
    _require_exact_keys(raw, {"version", "cases"}, "evaluation manifest")
    if raw["version"] != MANIFEST_VERSION:
        raise HarnessError(f"evaluation manifest version must be {MANIFEST_VERSION}")
    raw_cases = raw["cases"]
    if not isinstance(raw_cases, list) or not raw_cases:
        raise HarnessError("evaluation manifest cases must be a non-empty array")

    cases: list[EvaluationCase] = []
    seen_ids: set[str] = set()
    seen_targets: set[str] = set()
    for case_index, raw_case in enumerate(raw_cases):
        label = f"evaluation manifest cases[{case_index}]"
        if not isinstance(raw_case, dict):
            raise HarnessError(f"{label} must be an object")
        _require_exact_keys(
            raw_case,
            {"id", "command", "suites", "tags", "inputs", "target"},
            label,
        )
        case_id = _require_string(raw_case["id"], f"{label}.id")
        if not _ID_RE.fullmatch(case_id) or case_id in seen_ids:
            raise HarnessError(f"{label}.id is invalid or duplicated")
        seen_ids.add(case_id)
        command = _require_string(raw_case["command"], f"{label}.command")
        suites = _string_list(raw_case["suites"], f"{label}.suites", pattern=_TAG_RE)
        tags = _string_list(raw_case["tags"], f"{label}.tags", pattern=_TAG_RE)
        raw_inputs = raw_case["inputs"]
        if not isinstance(raw_inputs, list) or len(raw_inputs) != MAX_INPUTS:
            raise HarnessError(f"{label}.inputs must contain exactly {MAX_INPUTS} item")
        inputs: list[EvaluationInput] = []
        input_paths: set[str] = set()
        for input_index, raw_input in enumerate(raw_inputs):
            input_label = f"{label}.inputs[{input_index}]"
            if not isinstance(raw_input, dict):
                raise HarnessError(f"{input_label} must be an object")
            _require_exact_keys(raw_input, {"path", "sha256"}, input_label)
            path_text, absolute_path = _safe_repo_path(
                root,
                raw_input["path"],
                f"{input_label}.path",
            )
            if path_text.casefold() in input_paths:
                raise HarnessError(f"{label}.inputs must not repeat a path")
            input_paths.add(path_text.casefold())
            input_sha = _validate_sha256(raw_input["sha256"], f"{input_label}.sha256")
            inputs.append(
                EvaluationInput(
                    path=path_text,
                    sha256=input_sha,
                    absolute_path=absolute_path,
                    text=_read_input(absolute_path, input_sha, command, input_label),
                ),
            )
        target = _load_target(
            root,
            raw_case["target"],
            tuple(inputs),
            f"{label}.target",
        )
        if target.path.casefold() in seen_targets:
            raise HarnessError("each case must identify a distinct target file")
        seen_targets.add(target.path.casefold())
        cases.append(
            EvaluationCase(
                id=case_id,
                command=command,
                suites=suites,
                tags=tags,
                inputs=tuple(inputs),
                target=target,
            ),
        )
    return EvaluationManifest(
        version=MANIFEST_VERSION,
        sha256=_sha256(payload),
        cases=tuple(cases),
    )


def select_cases(
    manifest: EvaluationManifest,
    *,
    suite: str | None,
    case_ids: Sequence[str],
) -> tuple[EvaluationCase, ...]:
    if (suite is None) == (not case_ids):
        raise HarnessError("select exactly one of --suite or --case")
    if suite is not None:
        selected = tuple(case for case in manifest.cases if suite in case.suites)
        if not selected:
            raise HarnessError(f"suite does not select any cases: {suite}")
        return selected
    by_id = {case.id: case for case in manifest.cases}
    missing = sorted(set(case_ids) - by_id.keys())
    if missing:
        raise HarnessError(f"unknown case IDs: {', '.join(missing)}")
    if len(set(case_ids)) != len(case_ids):
        raise HarnessError("--case must not contain duplicate IDs")
    return tuple(by_id[case_id] for case_id in case_ids)


def _safe_manifest_path(
    root: Path,
    value: Any,
    label: str,
) -> tuple[str, Path]:
    """Resolve a traversal-free relative path from an external manifest."""

    text = _require_string(value, label)
    if "\\" in text or ":" in text:
        raise HarnessError(f"{label} must use a relative POSIX path")
    relative = PurePosixPath(text)
    if (
        relative.is_absolute()
        or not relative.parts
        or any(part in {"", ".", ".."} for part in relative.parts)
    ):
        raise HarnessError(f"{label} must be a traversal-free relative path")
    resolved_root = root.resolve()
    candidate = resolved_root.joinpath(*relative.parts).resolve()
    if not candidate.is_relative_to(resolved_root):
        raise HarnessError(f"{label} resolves outside the manifest directory")
    return text, candidate


def _load_template_input(
    path: Path,
    expected_sha256: str,
    label: str,
) -> str:
    if not path.is_file():
        raise HarnessError(f"{label} does not identify a file")
    try:
        payload = path.read_bytes()
    except OSError as error:
        raise HarnessError(f"{label} could not be read") from error
    if _sha256(payload) != expected_sha256:
        raise HarnessError(f"{label} SHA-256 does not match")
    if not payload or len(payload) > MAX_INPUT_BYTES:
        raise HarnessError(f"{label} must contain 1 to {MAX_INPUT_BYTES} bytes")
    if payload.startswith(b"\xef\xbb\xbf"):
        raise HarnessError(f"{label} must not contain a UTF-8 BOM")
    try:
        text = payload.decode("utf-8", errors="strict")
    except UnicodeDecodeError as error:
        raise HarnessError(f"{label} is not strict UTF-8") from error
    if not text.strip():
        raise HarnessError(f"{label} must not contain only whitespace")
    return text


def _load_ttp_template_schema(
    root: Path,
    raw_schema: Any,
    label: str,
) -> tuple[JsonObject, str, str]:
    if not isinstance(raw_schema, Mapping):
        raise HarnessError(f"{label} must be an object")
    _require_exact_keys(raw_schema, {"path", "sha256"}, label)
    path_text, path = _safe_manifest_path(root, raw_schema["path"], f"{label}.path")
    expected_sha256 = _validate_sha256(raw_schema["sha256"], f"{label}.sha256")
    value, payload = _read_json(path, f"{label}.file")
    if _sha256(payload) != expected_sha256:
        raise HarnessError(f"{label}.file SHA-256 does not match")
    if not isinstance(value, dict):
        raise HarnessError(f"{label}.file root must be an object")
    issues = validate_result_schema(value)
    if issues:
        raise HarnessError(f"{label}.file is not a supported result schema")
    return value, path_text, expected_sha256


def _load_ttp_template_target(
    root: Path,
    raw_target: Any,
    schema: Mapping[str, Any],
    inputs: tuple[EvaluationInput, ...],
    label: str,
) -> TtpTemplateTarget:
    if not isinstance(raw_target, Mapping):
        raise HarnessError(f"{label} must be an object")
    _require_exact_keys(raw_target, {"path", "sha256"}, label)
    path_text, path = _safe_manifest_path(root, raw_target["path"], f"{label}.path")
    expected_sha256 = _validate_sha256(raw_target["sha256"], f"{label}.sha256")
    value, payload = _read_json(path, f"{label}.file")
    if _sha256(payload) != expected_sha256:
        raise HarnessError(f"{label}.file SHA-256 does not match")
    if not isinstance(value, list) or len(value) != len(inputs):
        raise HarnessError(f"{label}.file must correspond one-to-one with inputs")
    if any(not isinstance(record, dict) for record in value):
        raise HarnessError(f"{label}.file must contain only object records")
    issues = validate_records_against_schema(value, schema)
    if issues:
        raise HarnessError(f"{label}.file records do not satisfy the schema")
    return TtpTemplateTarget(
        records=tuple(value),
        path=path_text,
        sha256=expected_sha256,
    )


def load_ttp_template_manifest(manifest_path: Path) -> TtpTemplateManifest:
    """Load an external, caller-owned TTP-only manifest without networking."""

    path = manifest_path.expanduser().resolve()
    raw, payload = _read_json(path, "TTP template manifest")
    if not isinstance(raw, Mapping):
        raise HarnessError("TTP template manifest root must be an object")
    _require_exact_keys(raw, {"version", "cases"}, "TTP template manifest")
    if raw["version"] != TTP_TEMPLATE_MANIFEST_VERSION:
        raise HarnessError(
            f"TTP template manifest version must be {TTP_TEMPLATE_MANIFEST_VERSION}",
        )
    raw_cases = raw["cases"]
    if not isinstance(raw_cases, list) or not raw_cases:
        raise HarnessError("TTP template manifest cases must be a non-empty array")

    root = path.parent
    cases: list[TtpTemplateCase] = []
    seen_ids: set[str] = set()
    for index, raw_case in enumerate(raw_cases):
        label = f"TTP template manifest cases[{index}]"
        if not isinstance(raw_case, Mapping):
            raise HarnessError(f"{label} must be an object")
        required_keys = {"id", "suites", "schema", "inputs", "expected_records"}
        allowed_keys = required_keys | {"tags"}
        missing = required_keys - raw_case.keys()
        extra = raw_case.keys() - allowed_keys
        if missing or extra:
            details = []
            if missing:
                details.append(f"missing keys: {', '.join(sorted(missing))}")
            if extra:
                details.append(f"unsupported keys: {', '.join(sorted(extra))}")
            raise HarnessError(f"{label} {'; '.join(details)}")
        case_id = _require_string(raw_case["id"], f"{label}.id")
        if not _ID_RE.fullmatch(case_id) or case_id in seen_ids:
            raise HarnessError(f"{label}.id is invalid or duplicated")
        seen_ids.add(case_id)
        suites = _string_list(raw_case["suites"], f"{label}.suites", pattern=_TAG_RE)
        tags = _optional_string_list(
            raw_case.get("tags"),
            f"{label}.tags",
            pattern=_TAG_RE,
        )
        schema, schema_path, schema_sha256 = _load_ttp_template_schema(
            root,
            raw_case["schema"],
            f"{label}.schema",
        )
        raw_inputs = raw_case["inputs"]
        if (
            not isinstance(raw_inputs, list)
            or not 1 <= len(raw_inputs) <= MAX_TTP_TEMPLATE_INPUTS
        ):
            raise HarnessError(
                f"{label}.inputs must contain 1 to {MAX_TTP_TEMPLATE_INPUTS} items",
            )
        inputs: list[EvaluationInput] = []
        input_paths: set[str] = set()
        for input_index, raw_input in enumerate(raw_inputs):
            input_label = f"{label}.inputs[{input_index}]"
            if not isinstance(raw_input, Mapping):
                raise HarnessError(f"{input_label} must be an object")
            _require_exact_keys(raw_input, {"path", "sha256"}, input_label)
            input_path, absolute_path = _safe_manifest_path(
                root,
                raw_input["path"],
                f"{input_label}.path",
            )
            if input_path.casefold() in input_paths:
                raise HarnessError(f"{label}.inputs must not repeat a path")
            input_paths.add(input_path.casefold())
            input_sha256 = _validate_sha256(
                raw_input["sha256"],
                f"{input_label}.sha256",
            )
            inputs.append(
                EvaluationInput(
                    path=input_path,
                    sha256=input_sha256,
                    absolute_path=absolute_path,
                    text=_load_template_input(
                        absolute_path,
                        input_sha256,
                        f"{input_label}.file",
                    ),
                ),
            )
        target = _load_ttp_template_target(
            root,
            raw_case["expected_records"],
            schema,
            tuple(inputs),
            f"{label}.expected_records",
        )
        cases.append(
            TtpTemplateCase(
                id=case_id,
                suites=suites,
                tags=tags,
                schema=schema,
                schema_path=schema_path,
                schema_sha256=schema_sha256,
                inputs=tuple(inputs),
                target=target,
            ),
        )
    return TtpTemplateManifest(
        version=TTP_TEMPLATE_MANIFEST_VERSION,
        path=path,
        sha256=_sha256(payload),
        cases=tuple(cases),
    )


def _load_test_set_text(
    path: Path,
    expected_sha256: str,
    label: str,
    *,
    max_bytes: int,
    require_nonempty: bool = True,
) -> str:
    if not path.is_file():
        raise HarnessError(f"{label} does not identify a file")
    try:
        payload = path.read_bytes()
    except OSError as error:
        raise HarnessError(f"{label} could not be read") from error
    if _sha256(payload) != expected_sha256:
        raise HarnessError(f"{label} SHA-256 does not match")
    if len(payload) > max_bytes or (require_nonempty and not payload):
        raise HarnessError(f"{label} exceeds its size or emptiness limit")
    if payload.startswith(b"\xef\xbb\xbf"):
        raise HarnessError(f"{label} must not contain a UTF-8 BOM")
    try:
        text = payload.decode("utf-8", errors="strict")
    except UnicodeDecodeError as error:
        raise HarnessError(f"{label} is not strict UTF-8") from error
    if require_nonempty and not text.strip():
        raise HarnessError(f"{label} must not contain only whitespace")
    return text


def _load_test_set_json(
    path: Path,
    expected_sha256: str,
    label: str,
    *,
    max_bytes: int,
) -> tuple[Any, bytes]:
    if not path.is_file():
        raise HarnessError(f"{label} does not identify a file")
    try:
        payload = path.read_bytes()
    except OSError as error:
        raise HarnessError(f"{label} could not be read") from error
    if _sha256(payload) != expected_sha256:
        raise HarnessError(f"{label} SHA-256 does not match")
    if len(payload) > max_bytes:
        raise HarnessError(f"{label} exceeds its size limit")
    if payload.startswith(b"\xef\xbb\xbf"):
        raise HarnessError(f"{label} must not contain a UTF-8 BOM")
    try:
        value = json.loads(
            payload.decode("utf-8", errors="strict"),
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=lambda value: (_raise_invalid_constant(value)),
        )
    except HarnessError:
        raise
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise HarnessError(f"{label} is not strict UTF-8 JSON") from error
    return value, payload


def _load_test_set_files(
    root: Path,
    case_path: Path,
    raw_files: Mapping[str, Any],
    label: str,
) -> tuple[
    tuple[EvaluationInput, ...],
    JsonObject,
    str,
    tuple[JsonObject, ...],
    dict[str, Any],
]:
    _require_exact_keys(
        raw_files,
        {"schema", "template", "expected", "inputs"},
        f"{label}.files",
    )

    def file_hash(key: str) -> str:
        raw_file = raw_files[key]
        if not isinstance(raw_file, Mapping):
            raise HarnessError(f"{label}.files.{key} must be an object")
        _require_exact_keys(raw_file, {"sha256"}, f"{label}.files.{key}")
        return _validate_sha256(raw_file["sha256"], f"{label}.files.{key}.sha256")

    schema_sha256 = file_hash("schema")
    schema, _ = _load_test_set_json(
        case_path / "schema.json",
        schema_sha256,
        f"{label}.schema.json",
        max_bytes=256 * 1024,
    )
    if not isinstance(schema, dict):
        raise HarnessError(f"{label}.schema.json must contain an object")
    if validate_result_schema(schema):
        raise HarnessError(f"{label}.schema.json is not a supported result schema")

    raw_inputs = raw_files["inputs"]
    if (
        not isinstance(raw_inputs, list)
        or not 1 <= len(raw_inputs) <= TEST_SET_MAX_INPUTS
    ):
        raise HarnessError(
            f"{label}.files.inputs must contain 1 to {TEST_SET_MAX_INPUTS} items",
        )
    inputs: list[EvaluationInput] = []
    expected_names = {f"{index:03d}.txt" for index in range(1, len(raw_inputs) + 1)}
    actual_names: set[str] = set()
    input_hashes: list[str] = []
    for index, raw_input in enumerate(raw_inputs, start=1):
        input_label = f"{label}.files.inputs[{index - 1}]"
        if not isinstance(raw_input, Mapping):
            raise HarnessError(f"{input_label} must be an object")
        _require_exact_keys(raw_input, {"name", "sha256"}, input_label)
        name = _require_string(raw_input["name"], f"{input_label}.name")
        if not re.fullmatch(r"[0-9]{3}\.txt", name) or name != f"{index:03d}.txt":
            raise HarnessError(f"{input_label}.name must be {index:03d}.txt")
        if name in actual_names:
            raise HarnessError(f"{label}.files.inputs contains duplicate names")
        actual_names.add(name)
        input_sha256 = _validate_sha256(raw_input["sha256"], f"{input_label}.sha256")
        input_hashes.append(input_sha256)
        input_path = case_path / "inputs" / name
        inputs.append(
            EvaluationInput(
                path=f"{case_path.name}/inputs/{name}",
                sha256=input_sha256,
                absolute_path=input_path,
                text=_load_test_set_text(
                    input_path,
                    input_sha256,
                    f"{input_label}.file",
                    max_bytes=MAX_INPUT_BYTES,
                ),
            ),
        )
    if actual_names != expected_names:
        raise HarnessError(f"{label}.files.inputs names are not contiguous")
    actual_input_files = {
        item.name for item in (case_path / "inputs").iterdir() if item.is_file()
    }
    if actual_input_files != expected_names:
        raise HarnessError(f"{label}.inputs contains unexpected files")

    template_sha256 = file_hash("template")
    template = _load_test_set_text(
        case_path / "template.ttp",
        template_sha256,
        f"{label}.template.ttp",
        max_bytes=TEST_SET_TEMPLATE_MAX_BYTES,
    )

    expected_sha256 = file_hash("expected")
    expected, _ = _load_test_set_json(
        case_path / "expected.json",
        expected_sha256,
        f"{label}.expected.json",
        max_bytes=8 * 1024 * 1024,
    )
    if not isinstance(expected, list) or len(expected) != len(inputs):
        raise HarnessError(f"{label}.expected.json must match the input count")
    if any(not isinstance(record, dict) for record in expected):
        raise HarnessError(f"{label}.expected.json must contain only object records")
    if validate_records_against_schema(expected, schema):
        raise HarnessError(f"{label}.expected.json records do not satisfy schema")

    baseline = validate_ttp_template(
        template,
        [item.text for item in inputs],
        schema,
        timeout_seconds=20.0,
        max_result_bytes=8 * 1024 * 1024,
    )
    if baseline.issues or baseline.records != expected:
        raise HarnessError(
            f"{label} standard template does not reproduce expected records",
        )
    return (
        tuple(inputs),
        schema,
        template,
        tuple(expected),
        {
            "schema": schema_sha256,
            "template": template_sha256,
            "expected": expected_sha256,
            "inputs": tuple(input_hashes),
        },
    )


def _dataset_file_spec(value: Any, label: str) -> DatasetFileSpec:
    if not isinstance(value, Mapping):
        raise HarnessError(f"{label} must be an object")
    _require_exact_keys(value, {"file", "sha256"}, label)
    file_name = _require_string(value["file"], f"{label}.file")
    sha256 = _validate_sha256(value["sha256"], f"{label}.sha256")
    return DatasetFileSpec(file=file_name, sha256=sha256)


def _verify_dataset_file(
    path: Path,
    spec: DatasetFileSpec,
    label: str,
) -> bool:
    """Verify a declared file when present, returning false for pending files."""

    if not path.is_file():
        return False
    try:
        payload = path.read_bytes()
    except OSError as error:
        raise HarnessError(f"{label} could not be read") from error
    if _sha256(payload) != spec.sha256:
        raise HarnessError(f"{label} SHA-256 does not match")
    return True


def _dataset_path_spec(
    dataset_path: Path,
    spec: DatasetFileSpec,
    expected_file: str,
    label: str,
) -> Path:
    path_text, path = _safe_manifest_path(dataset_path, spec.file, f"{label}.file")
    if path_text != expected_file:
        raise HarnessError(f"{label}.file must be {expected_file}")
    return path


def _dataset_inputs(
    entry: DatasetRegistryEntry,
    *,
    validate_content: bool,
) -> tuple[EvaluationInput, ...]:
    inputs: list[EvaluationInput] = []
    for index, spec in enumerate(entry.inputs, start=1):
        path = _dataset_path_spec(
            entry.absolute_path,
            spec,
            f"inputs/{index:03d}.txt",
            f"dataset {entry.name}.inputs[{index - 1}]",
        )
        label = f"dataset {entry.name}.inputs[{index - 1}].file"
        if not path.is_file():
            continue
        text = _load_test_set_text(
            path,
            spec.sha256,
            label,
            max_bytes=MAX_INPUT_BYTES,
        )
        inputs.append(
            EvaluationInput(
                path=f"test_sets/{entry.name}/inputs/{index:03d}.txt",
                sha256=spec.sha256,
                absolute_path=path,
                text=text,
            ),
        )
    return tuple(inputs)


def _validate_input_scope(value: str) -> Literal["default", "full"]:
    if value not in {"default", "full"}:
        raise HarnessError("input scope must be default or full")
    return cast(Literal["default", "full"], value)


def dataset_input_scope_metadata(
    entry: DatasetRegistryEntry,
    input_scope: Literal["default", "full"],
) -> dict[str, Any]:
    """Return payload-free information about inputs selected for a run."""

    if input_scope == "default":
        indices = (
            ()
            if entry.default_input_index is None
            else (entry.default_input_index,)
        )
    else:
        indices = tuple(range(len(entry.inputs)))
    return {
        "input_scope": input_scope,
        "default_input": entry.default_input,
        "selected_input": (
            entry.inputs[indices[0]].file if len(indices) == 1 else None
        ),
        "selected_input_indices": indices,
        "selected_inputs": tuple(
            {
                "input_index": index,
                "display_number": index + 1,
                "file": entry.inputs[index].file,
            }
            for index in indices
        ),
    }


def _scoped_dataset_inputs(
    entry: DatasetRegistryEntry,
    input_scope: Literal["default", "full"],
) -> tuple[tuple[EvaluationInput, ...], tuple[int, ...]]:
    inputs = _dataset_inputs(entry, validate_content=True)
    if len(inputs) != len(entry.inputs):
        raise HarnessError(f"dataset {entry.name} input files are incomplete")
    metadata = dataset_input_scope_metadata(entry, input_scope)
    selected_indices = cast(tuple[int, ...], metadata["selected_input_indices"])
    if not selected_indices:
        raise HarnessError(f"dataset {entry.name} has no default_input")
    return tuple(inputs[index] for index in selected_indices), selected_indices


def load_dataset_registry(registry_path: Path) -> DatasetRegistry:
    """Load the strict TOML registry and inspect filesystem completeness.

    This function intentionally does not run TTP or model code.  Missing files
    are represented as pending state so ``list`` remains useful while a case is
    being assembled; malformed present files and registry drift are errors.
    """

    path = registry_path.expanduser().resolve()
    try:
        payload = path.read_bytes()
    except OSError as error:
        raise HarnessError("dataset registry could not be read") from error
    if payload.startswith(b"\xef\xbb\xbf"):
        raise HarnessError("dataset registry must not contain a UTF-8 BOM")
    try:
        raw = tomllib.loads(payload.decode("utf-8", errors="strict"))
    except (UnicodeDecodeError, tomllib.TOMLDecodeError) as error:
        raise HarnessError("dataset registry is not valid UTF-8 TOML") from error
    if not isinstance(raw, Mapping):
        raise HarnessError("dataset registry root must be a table")
    _require_exact_keys(raw, {"version", "dataset"}, "dataset registry")
    if raw["version"] != DATASET_REGISTRY_VERSION:
        raise HarnessError(
            f"dataset registry version must be {DATASET_REGISTRY_VERSION}",
        )
    raw_datasets = raw["dataset"]
    if not isinstance(raw_datasets, list) or not raw_datasets:
        raise HarnessError("dataset registry dataset must be a non-empty array")

    test_sets_root = path.parent / "test_sets"
    if not test_sets_root.is_dir():
        raise HarnessError("dataset registry test_sets directory does not exist")
    actual_dirs = {child.name for child in test_sets_root.iterdir() if child.is_dir()}
    entries: list[DatasetRegistryEntry] = []
    seen_ids: set[int] = set()
    seen_names: set[str] = set()
    for index, raw_dataset in enumerate(raw_datasets):
        label = f"dataset registry dataset[{index}]"
        if not isinstance(raw_dataset, Mapping):
            raise HarnessError(f"{label} must be a table")
        required = {"id", "name", "command", "platform", "source", "tags", "inputs"}
        allowed = required | {"default_input", "template", "schema", "expected"}
        missing = required - raw_dataset.keys()
        extra = raw_dataset.keys() - allowed
        if missing:
            raise HarnessError(f"{label} is missing keys: {', '.join(sorted(missing))}")
        if extra:
            raise HarnessError(
                f"{label} has unsupported keys: {', '.join(sorted(extra))}"
            )
        dataset_id = raw_dataset["id"]
        if (
            isinstance(dataset_id, bool)
            or not isinstance(dataset_id, int)
            or dataset_id < 1
            or dataset_id in seen_ids
        ):
            raise HarnessError(f"{label}.id is invalid or duplicated")
        seen_ids.add(dataset_id)
        name = _require_string(raw_dataset["name"], f"{label}.name")
        if not _ID_RE.fullmatch(name) or name in seen_names:
            raise HarnessError(f"{label}.name is invalid or duplicated")
        seen_names.add(name)
        if name not in actual_dirs:
            raise HarnessError(f"{label}.name directory does not exist: {name}")
        dataset_path = test_sets_root / name
        actual_children = {child.name for child in dataset_path.iterdir()}
        allowed_children = {"inputs", "template.ttp", "schema.json", "expected.json"}
        unexpected_children = actual_children - allowed_children
        if unexpected_children:
            raise HarnessError(
                f"{label} contains unsupported files: "
                f"{', '.join(sorted(unexpected_children))}",
            )
        inputs_dir = dataset_path / "inputs"
        if not inputs_dir.is_dir():
            raise HarnessError(f"{label}.inputs directory does not exist")

        command = _require_string(raw_dataset["command"], f"{label}.command")
        platform = _require_string(raw_dataset["platform"], f"{label}.platform")
        source = _require_string(raw_dataset["source"], f"{label}.source")
        tags = _optional_string_list(
            raw_dataset["tags"], f"{label}.tags", pattern=_TAG_RE
        )
        raw_inputs = raw_dataset["inputs"]
        if (
            not isinstance(raw_inputs, list)
            or not 1 <= len(raw_inputs) <= TEST_SET_MAX_INPUTS
        ):
            raise HarnessError(
                f"{label}.inputs must contain 1 to {TEST_SET_MAX_INPUTS} items",
            )
        input_specs: list[DatasetFileSpec] = []
        expected_input_names = {
            f"{index:03d}.txt" for index in range(1, len(raw_inputs) + 1)
        }
        for input_index, raw_input in enumerate(raw_inputs, start=1):
            spec = _dataset_file_spec(raw_input, f"{label}.inputs[{input_index - 1}]")
            _dataset_path_spec(
                dataset_path,
                spec,
                f"inputs/{input_index:03d}.txt",
                f"{label}.inputs[{input_index - 1}]",
            )
            input_specs.append(spec)
        default_input = None
        default_input_index = None
        if "default_input" in raw_dataset:
            default_input = _require_string(
                raw_dataset["default_input"],
                f"{label}.default_input",
            )
            _safe_manifest_path(
                dataset_path,
                default_input,
                f"{label}.default_input",
            )
            try:
                default_input_index = next(
                    input_index
                    for input_index, spec in enumerate(input_specs)
                    if spec.file == default_input
                )
            except StopIteration:
                raise HarnessError(
                    f"{label}.default_input must match a declared input file"
                ) from None
        actual_input_names = {
            item.name for item in inputs_dir.iterdir() if item.is_file()
        }
        if actual_input_names - expected_input_names:
            raise HarnessError(f"{label}.inputs contains unexpected files")
        template_spec = (
            _dataset_file_spec(raw_dataset["template"], f"{label}.template")
            if "template" in raw_dataset
            else None
        )
        schema_spec = (
            _dataset_file_spec(raw_dataset["schema"], f"{label}.schema")
            if "schema" in raw_dataset
            else None
        )
        expected_spec = (
            _dataset_file_spec(raw_dataset["expected"], f"{label}.expected")
            if "expected" in raw_dataset
            else None
        )
        if (schema_spec is None) != (expected_spec is None):
            raise HarnessError(f"{label}.schema and expected must be declared together")
        registry_errors: list[str] = []
        for spec, filename, field in (
            (template_spec, "template.ttp", "template"),
            (schema_spec, "schema.json", "schema"),
            (expected_spec, "expected.json", "expected"),
        ):
            actual = (dataset_path / filename).is_file()
            if spec is None and actual:
                registry_errors.append(
                    f"{label}.{field} file exists but is not declared"
                )
            if spec is not None:
                _dataset_path_spec(dataset_path, spec, filename, f"{label}.{field}")
                _verify_dataset_file(dataset_path / filename, spec, f"{label}.{field}")
        for input_index, spec in enumerate(input_specs, start=1):
            _verify_dataset_file(
                dataset_path / spec.file,
                spec,
                f"{label}.inputs[{input_index - 1}]",
            )
        for filename in sorted(actual_input_names):
            if filename in expected_input_names:
                expected_spec_for_input = input_specs[int(filename[:3]) - 1]
                _verify_dataset_file(
                    inputs_dir / filename,
                    expected_spec_for_input,
                    f"{label}.inputs/{filename}",
                )

        schema_present = (dataset_path / "schema.json").is_file()
        expected_present = (dataset_path / "expected.json").is_file()
        if schema_present != expected_present:
            raise HarnessError(
                f"{label} must contain schema.json and expected.json together"
            )
        template_present = (dataset_path / "template.ttp").is_file()
        if schema_present:
            stage: Literal["inputs-only", "template", "complete"] = "complete"
        elif template_present:
            stage = "template"
        else:
            stage = "inputs-only"
        present_files = ["inputs"]
        missing_files: list[str] = []
        for input_index, _spec in enumerate(input_specs, start=1):
            filename = f"inputs/{input_index:03d}.txt"
            if (inputs_dir / f"{input_index:03d}.txt").is_file():
                present_files.append(filename)
            else:
                missing_files.append(filename)
        for filename, spec in (
            ("template.ttp", template_spec),
            ("schema.json", schema_spec),
            ("expected.json", expected_spec),
        ):
            if (dataset_path / filename).is_file():
                present_files.append(filename)
            elif spec is not None:
                missing_files.append(filename)
        input_texts = _dataset_inputs(
            DatasetRegistryEntry(
                id=dataset_id,
                name=name,
                command=command,
                platform=platform,
                source=source,
                tags=tags,
                absolute_path=dataset_path,
                inputs=tuple(input_specs),
                default_input=default_input,
                default_input_index=default_input_index,
                template=template_spec,
                schema=schema_spec,
                expected=expected_spec,
                stage=stage,
                present_files=tuple(present_files),
                missing_files=tuple(missing_files),
                input_texts=(),
                template_text=None,
                registry_errors=tuple(registry_errors),
            ),
            validate_content=False,
        )
        template_text = None
        if template_spec is not None and (dataset_path / "template.ttp").is_file():
            template_text = _load_test_set_text(
                dataset_path / "template.ttp",
                template_spec.sha256,
                f"{label}.template.ttp",
                max_bytes=TEST_SET_TEMPLATE_MAX_BYTES,
            )
        entries.append(
            DatasetRegistryEntry(
                id=dataset_id,
                name=name,
                command=command,
                platform=platform,
                source=source,
                tags=tags,
                absolute_path=dataset_path,
                inputs=tuple(input_specs),
                default_input=default_input,
                default_input_index=default_input_index,
                template=template_spec,
                schema=schema_spec,
                expected=expected_spec,
                stage=stage,
                present_files=tuple(present_files),
                missing_files=tuple(missing_files),
                input_texts=input_texts,
                template_text=template_text,
                registry_errors=tuple(registry_errors),
            ),
        )
    missing_dirs = actual_dirs - seen_names
    if missing_dirs:
        raise HarnessError(
            "unregistered test-set directories: " + ", ".join(sorted(missing_dirs)),
        )
    return DatasetRegistry(
        version=DATASET_REGISTRY_VERSION,
        path=path,
        sha256=_sha256(payload),
        datasets=tuple(entries),
    )


def _load_dataset_complete_case(
    entry: DatasetRegistryEntry,
    input_scope: Literal["default", "full"],
) -> TestSetCase:
    if entry.stage != "complete" or entry.missing_files:
        raise HarnessError(f"dataset {entry.name} is not complete")
    if entry.template is None or entry.schema is None or entry.expected is None:
        raise HarnessError(f"dataset {entry.name} is missing a four-part file")
    inputs, original_input_indices = _scoped_dataset_inputs(entry, input_scope)
    schema, _ = _load_test_set_json(
        entry.absolute_path / "schema.json",
        entry.schema.sha256,
        f"dataset {entry.name}.schema.json",
        max_bytes=256 * 1024,
    )
    if not isinstance(schema, dict) or validate_result_schema(schema):
        raise HarnessError(f"dataset {entry.name}.schema.json is not supported")
    template = _load_test_set_text(
        entry.absolute_path / "template.ttp",
        entry.template.sha256,
        f"dataset {entry.name}.template.ttp",
        max_bytes=TEST_SET_TEMPLATE_MAX_BYTES,
    )
    expected, _ = _load_test_set_json(
        entry.absolute_path / "expected.json",
        entry.expected.sha256,
        f"dataset {entry.name}.expected.json",
        max_bytes=8 * 1024 * 1024,
    )
    if not isinstance(expected, list):
        raise HarnessError(f"dataset {entry.name}.expected.json must be an array")
    if input_scope == "full" and len(expected) != len(entry.inputs):
        raise HarnessError(f"dataset {entry.name}.expected.json must match input count")
    if any(index >= len(expected) for index in original_input_indices):
        raise HarnessError(
            f"dataset {entry.name}.expected.json is missing the selected record"
        )
    selected_expected = tuple(expected[index] for index in original_input_indices)
    if any(not isinstance(record, dict) for record in selected_expected):
        raise HarnessError(f"dataset {entry.name}.expected.json must contain objects")
    if validate_records_against_schema(selected_expected, schema):
        raise HarnessError(f"dataset {entry.name}.expected.json violates schema")
    return TestSetCase(
        id=entry.name,
        command=entry.command,
        suites=(),
        tags=entry.tags,
        path=f"test_sets/{entry.name}",
        absolute_path=entry.absolute_path,
        inputs=inputs,
        schema=schema,
        template=template,
        expected_records=selected_expected,
        file_sha256={
            "schema": entry.schema.sha256,
            "template": entry.template.sha256,
            "expected": entry.expected.sha256,
            "inputs": tuple(
                entry.inputs[index].sha256 for index in original_input_indices
            ),
        },
        original_input_indices=original_input_indices,
    )


def preflight_dataset_registry(
    registry: DatasetRegistry,
    *,
    input_scope: Literal["default", "full"] = "default",
) -> tuple[DatasetPreflightReport, ...]:
    """Run deterministic checks for every registered dataset."""

    input_scope = _validate_input_scope(input_scope)
    reports: list[DatasetPreflightReport] = []
    for entry in registry.datasets:
        scope_metadata = dataset_input_scope_metadata(entry, input_scope)
        selected_input_indices = cast(
            tuple[int, ...],
            scope_metadata["selected_input_indices"],
        )
        if entry.registry_errors:
            reports.append(
                DatasetPreflightReport(
                    dataset=entry,
                    status="failed",
                    input_scope=input_scope,
                    selected_input_indices=selected_input_indices,
                    errors=entry.registry_errors,
                ),
            )
            continue
        if entry.missing_files:
            reports.append(
                DatasetPreflightReport(
                    dataset=entry,
                    status="pending",
                    input_scope=input_scope,
                    selected_input_indices=selected_input_indices,
                ),
            )
            continue
        if input_scope == "default" and entry.default_input_index is None:
            reports.append(
                DatasetPreflightReport(
                    dataset=entry,
                    status="pending",
                    input_scope=input_scope,
                ),
            )
            continue
        smoke_results: list[dict[str, Any]] = []
        try:
            if entry.stage == "inputs-only":
                reports.append(
                    DatasetPreflightReport(
                        dataset=entry,
                        status="pending",
                        input_scope=input_scope,
                        selected_input_indices=selected_input_indices,
                    ),
                )
                continue
            if entry.template_text is None:
                raise HarnessError(f"dataset {entry.name} template is unavailable")
            if entry.stage == "template":
                inputs, original_input_indices = _scoped_dataset_inputs(
                    entry,
                    input_scope,
                )
                passed = 0
                for input_index, item in zip(
                    original_input_indices,
                    inputs,
                    strict=True,
                ):
                    parsed = parse_ttp_template(entry.template_text, item.text)
                    result = parsed.result
                    root_type = (
                        "object"
                        if isinstance(result, dict)
                        else "array"
                        if isinstance(result, list)
                        else type(result).__name__
                    )
                    smoke_results.append(
                        {
                            "input_index": input_index,
                            "success": not parsed.issues,
                            "root_type": root_type,
                            "root_count": len(result)
                            if isinstance(result, list)
                            else 1,
                            "issue_codes": [
                                str(getattr(issue, "code", "ttp.parse_failed"))
                                for issue in parsed.issues
                            ],
                        },
                    )
                    if parsed.issues:
                        codes = tuple(
                            str(getattr(issue, "code", "ttp.parse_failed"))
                            for issue in parsed.issues
                        )
                        raise HarnessError(
                            f"dataset {entry.name} template failed input "
                            f"{input_index + 1}: {', '.join(codes)}",
                        )
                    passed += 1
                reports.append(
                    DatasetPreflightReport(
                        dataset=entry,
                        status="passed",
                        input_scope=input_scope,
                        selected_input_indices=selected_input_indices,
                        template_inputs_passed=passed,
                        template_smoke_results=tuple(smoke_results),
                    ),
                )
                continue
            case = _load_dataset_complete_case(entry, input_scope)
            baseline = validate_ttp_template(
                case.template,
                [item.text for item in case.inputs],
                case.schema,
                timeout_seconds=20.0,
                max_result_bytes=8 * 1024 * 1024,
            )
            exact = not baseline.issues and baseline.records == list(
                case.expected_records
            )
            if not exact:
                raise HarnessError(
                    f"dataset {entry.name} standard template baseline mismatch"
                )
            reports.append(
                DatasetPreflightReport(
                    dataset=entry,
                    status="passed",
                    input_scope=input_scope,
                    selected_input_indices=selected_input_indices,
                    case=case,
                    template_inputs_passed=len(case.inputs),
                    baseline_exact=True,
                    template_smoke_results=tuple(
                        {
                            "input_index": index,
                            "success": True,
                            "root_type": "object",
                            "root_count": 1,
                            "issue_codes": [],
                        }
                        for index in case.original_input_indices
                    ),
                ),
            )
        except HarnessError as error:
            reports.append(
                DatasetPreflightReport(
                    dataset=entry,
                    status="failed",
                    input_scope=input_scope,
                    selected_input_indices=selected_input_indices,
                    errors=(str(error),),
                    template_smoke_results=tuple(smoke_results),
                ),
            )
    return tuple(reports)


def select_dataset_entries(
    registry: DatasetRegistry,
    *,
    names: Sequence[str] = (),
    ids: Sequence[int] = (),
    tags: Sequence[str] = (),
) -> tuple[DatasetRegistryEntry, ...]:
    """Select all entries by default, or intersect explicit name/id/tag filters."""

    if len(set(names)) != len(names):
        raise HarnessError("--dataset must not contain duplicate names")
    if len(set(ids)) != len(ids):
        raise HarnessError("--dataset-id must not contain duplicate IDs")
    if len(set(tags)) != len(tags):
        raise HarnessError("--tag must not contain duplicate tags")
    by_name = {dataset.name: dataset for dataset in registry.datasets}
    by_id = {dataset.id: dataset for dataset in registry.datasets}
    unknown_names = sorted(set(names) - by_name.keys())
    unknown_ids = sorted(set(ids) - by_id.keys())
    if unknown_names:
        raise HarnessError("unknown datasets: " + ", ".join(unknown_names))
    if unknown_ids:
        raise HarnessError("unknown dataset IDs: " + ", ".join(map(str, unknown_ids)))
    selected = list(registry.datasets)
    if names:
        selected = [dataset for dataset in selected if dataset.name in names]
    if ids:
        selected = [dataset for dataset in selected if dataset.id in ids]
    for tag in tags:
        if not _TAG_RE.fullmatch(tag):
            raise HarnessError(f"invalid tag: {tag}")
        selected = [dataset for dataset in selected if tag in dataset.tags]
    if not selected:
        raise HarnessError("dataset selection did not match any datasets")
    return tuple(selected)


def load_test_set_manifest(manifest_path: Path) -> TestSetManifest:
    """Load and fully preflight the canonical four-part test-set index."""

    path = manifest_path.expanduser().resolve()
    raw, payload = _read_json(path, "test-set manifest")
    if not isinstance(raw, Mapping):
        raise HarnessError("test-set manifest root must be an object")
    _require_exact_keys(raw, {"version", "cases"}, "test-set manifest")
    if raw["version"] != TEST_SET_MANIFEST_VERSION:
        raise HarnessError(
            f"test-set manifest version must be {TEST_SET_MANIFEST_VERSION}",
        )
    raw_cases = raw["cases"]
    if not isinstance(raw_cases, list) or not raw_cases:
        raise HarnessError("test-set manifest cases must be a non-empty array")

    root = path.parent
    cases: list[TestSetCase] = []
    seen_ids: set[str] = set()
    seen_paths: set[str] = set()
    for index, raw_case in enumerate(raw_cases):
        label = f"test-set manifest cases[{index}]"
        if not isinstance(raw_case, Mapping):
            raise HarnessError(f"{label} must be an object")
        required = {"id", "path", "command", "suites", "files"}
        allowed = required | {"tags"}
        _require_exact_keys(raw_case, allowed, label)
        case_id = _require_string(raw_case["id"], f"{label}.id")
        if not _ID_RE.fullmatch(case_id) or case_id in seen_ids:
            raise HarnessError(f"{label}.id is invalid or duplicated")
        seen_ids.add(case_id)
        path_text, case_path = _safe_manifest_path(
            root,
            raw_case["path"],
            f"{label}.path",
        )
        if path_text.casefold() in seen_paths:
            raise HarnessError(f"{label}.path is duplicated")
        seen_paths.add(path_text.casefold())
        if not case_path.is_dir():
            raise HarnessError(f"{label}.path must identify a directory")
        actual_children = {child.name for child in case_path.iterdir()}
        if actual_children != {
            "inputs",
            "schema.json",
            "template.ttp",
            "expected.json",
        }:
            raise HarnessError(
                f"{label}.path must contain exactly the four test-set parts",
            )
        inputs_dir = case_path / "inputs"
        if not inputs_dir.is_dir():
            raise HarnessError(f"{label}.inputs must be a directory")
        command = _require_string(raw_case["command"], f"{label}.command")
        suites = _string_list(raw_case["suites"], f"{label}.suites", pattern=_TAG_RE)
        tags = _optional_string_list(
            raw_case.get("tags"),
            f"{label}.tags",
            pattern=_TAG_RE,
        )
        raw_files = raw_case["files"]
        if not isinstance(raw_files, Mapping):
            raise HarnessError(f"{label}.files must be an object")
        inputs, schema, template, expected, hashes = _load_test_set_files(
            root,
            case_path,
            raw_files,
            label,
        )
        if SEMANTIC_PILOT_SUITE in suites and _is_line_text_placeholder_schema(schema):
            raise HarnessError(
                f"{label}.schema.json uses the lines[].text placeholder and "
                f"cannot join {SEMANTIC_PILOT_SUITE}",
            )
        cases.append(
            TestSetCase(
                id=case_id,
                command=command,
                suites=suites,
                tags=tags,
                path=path_text,
                absolute_path=case_path,
                inputs=inputs,
                schema=schema,
                template=template,
                expected_records=expected,
                file_sha256=hashes,
            ),
        )
    return TestSetManifest(
        version=TEST_SET_MANIFEST_VERSION,
        path=path,
        sha256=_sha256(payload),
        cases=tuple(cases),
    )


def select_test_sets(
    manifest: TestSetManifest,
    *,
    suite: str | None,
    case_ids: Sequence[str],
) -> tuple[TestSetCase, ...]:
    """Select a suite or explicit case IDs from the canonical manifest."""

    if (suite is None) == (not case_ids):
        raise HarnessError("select exactly one of --suite or --case")
    if suite is not None:
        selected = tuple(case for case in manifest.cases if suite in case.suites)
        if not selected:
            raise HarnessError(f"suite does not select any cases: {suite}")
        return selected
    by_id = {case.id: case for case in manifest.cases}
    if len(set(case_ids)) != len(case_ids):
        raise HarnessError("--case must not contain duplicate IDs")
    missing = sorted(set(case_ids) - by_id.keys())
    if missing:
        raise HarnessError(f"unknown case IDs: {', '.join(missing)}")
    return tuple(by_id[case_id] for case_id in case_ids)


def select_ttp_template_cases(
    manifest: TtpTemplateManifest,
    *,
    suite: str | None,
    case_ids: Sequence[str],
) -> tuple[TtpTemplateCase, ...]:
    """Select one suite or a deduplicated set of TTP-only case IDs."""

    if (suite is None) == (not case_ids):
        raise HarnessError("select exactly one of --suite or --case")
    if suite is not None:
        selected = tuple(case for case in manifest.cases if suite in case.suites)
        if not selected:
            raise HarnessError(f"suite does not select any cases: {suite}")
        return selected
    by_id = {case.id: case for case in manifest.cases}
    missing = sorted(set(case_ids) - by_id.keys())
    if missing:
        raise HarnessError(f"unknown case IDs: {', '.join(missing)}")
    if len(set(case_ids)) != len(case_ids):
        raise HarnessError("--case must not contain duplicate IDs")
    return tuple(by_id[case_id] for case_id in case_ids)


def schema_signature(schema: Mapping[str, Any]) -> dict[str, SchemaNode]:
    """Project a JSON Schema into the benchmark's structural contract."""

    collected: dict[str, SchemaNode] = {}

    def visit(node: Any, path: str, required: bool) -> None:
        if not isinstance(node, Mapping):
            raise HarnessError(f"generated schema node is not an object at {path}")
        node_type = node.get("type")
        if node_type not in SUPPORTED_NODE_TYPES:
            raise HarnessError(f"generated schema type is unsupported at {path}")
        collected[path] = SchemaNode(path, node_type, required)
        if node_type == "object":
            properties = node.get("properties")
            required_names = node.get("required")
            if not isinstance(properties, Mapping):
                raise HarnessError(f"generated object schema is incomplete at {path}")
            if required_names is None:
                required_set: set[str] = set()
            elif isinstance(required_names, list):
                required_set = set(required_names)
            else:
                raise HarnessError(f"generated object schema is incomplete at {path}")
            for key, child in properties.items():
                if not isinstance(key, str):
                    raise HarnessError(
                        f"generated schema has a non-string field at {path}",
                    )
                child_path = f"/{key}" if path == "/" else f"{path}/{key}"
                visit(child, child_path, key in required_set)
        elif node_type == "array":
            visit(node.get("items"), f"{path}/*", False)

    visit(schema, "/", False)
    return collected


def schema_from_contract(nodes: Sequence[SchemaNode]) -> dict[str, Any]:
    """Rebuild a closed Draft 2020-12 schema from a golden structural contract.

    This is the inverse of :func:`schema_signature` and lets the template-only
    mode pin each case's golden schema, so TTP quality can be measured without
    the field-naming noise the Schema phase would otherwise introduce.
    """

    by_path = {node.path: node for node in nodes}
    if len(by_path) != len(nodes):
        raise HarnessError("schema contract contains duplicate paths")
    root = by_path.get("/")
    if root is None or root.type != "object":
        raise HarnessError("schema contract must declare an object root")

    children: dict[str, list[SchemaNode]] = {path: [] for path in by_path}
    for node in nodes:
        if node.path == "/":
            continue
        parent = _parent_path(node.path)
        if parent is None or parent not in children:
            raise HarnessError(f"schema contract node has no parent at {node.path}")
        children[parent].append(node)

    def build(path: str) -> dict[str, Any]:
        node = by_path[path]
        if node.type == "object":
            properties: dict[str, Any] = {}
            required: list[str] = []
            for child in children[path]:
                name = child.path.rsplit("/", 1)[-1]
                if name == "*":
                    raise HarnessError(f"object schema has an array child at {path}")
                properties[name] = build(child.path)
                if child.required:
                    required.append(name)
            schema: dict[str, Any] = {
                "type": "object",
                "properties": properties,
                "additionalProperties": False,
            }
            if required:
                schema["required"] = sorted(required)
            return schema
        if node.type == "array":
            items = [child for child in children[path] if child.path.endswith("/*")]
            if len(items) != 1:
                raise HarnessError(
                    f"array schema needs exactly one items node at {path}",
                )
            return {"type": "array", "items": build(items[0].path)}
        return {"type": node.type}

    return build("/")


def _canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _leaf_counter(value: Any) -> Counter[tuple[str, str]]:
    counter: Counter[tuple[str, str]] = Counter()

    def visit(item: Any, path: str) -> None:
        if isinstance(item, dict):
            for key, child in item.items():
                child_path = f"/{key}" if path == "/" else f"{path}/{key}"
                visit(child, child_path)
        elif isinstance(item, list):
            for child in item:
                visit(child, f"{path}/*")
        else:
            counter[(path, _canonical_json(item))] += 1

    visit(value, "/")
    return counter


def _precision_recall_f1(
    actual: Counter[Any],
    expected: Counter[Any],
) -> tuple[float, float, float]:
    overlap = sum((actual & expected).values())
    actual_count = sum(actual.values())
    expected_count = sum(expected.values())
    precision = overlap / actual_count if actual_count else 0.0
    recall = overlap / expected_count if expected_count else 0.0
    f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
    return precision, recall, f1


def wilson_interval(
    successes: int | float,
    total: int | float,
    *,
    confidence: float = 0.95,
) -> tuple[float, float]:
    """Return a bounded Wilson score interval for a binomial proportion.

    The helper intentionally accepts counts rather than raw observations so it
    can be used by both the Laminar evaluator and the local summary builder.
    Invalid/empty samples return ``(0.0, 0.0)`` instead of raising, which keeps
    diagnostics best-effort and never changes generation behavior.
    """

    try:
        successes_value = float(successes)
        total_value = float(total)
        confidence_value = float(confidence)
    except (TypeError, ValueError):
        return 0.0, 0.0
    if (
        not math.isfinite(successes_value)
        or not math.isfinite(total_value)
        or not math.isfinite(confidence_value)
        or total_value <= 0.0
        or not 0.0 < confidence_value < 1.0
    ):
        return 0.0, 0.0
    successes_value = min(max(successes_value, 0.0), total_value)
    # The normal approximation is sufficient for the fixed diagnostic
    # confidence level and avoids a dependency solely for this projection.
    z = 1.959963984540054
    if confidence_value != 0.95:
        # Inverse-normal values are intentionally limited to the supported
        # confidence levels used by the evaluation harness.
        z_by_confidence = {
            0.90: 1.6448536269514722,
            0.95: 1.959963984540054,
            0.99: 2.5758293035489004,
        }
        z = z_by_confidence.get(round(confidence_value, 2), z)
    proportion = successes_value / total_value
    denominator = 1.0 + z * z / total_value
    center = (proportion + z * z / (2.0 * total_value)) / denominator
    margin = (
        z
        * math.sqrt(
            proportion * (1.0 - proportion) / total_value
            + z * z / (4.0 * total_value * total_value),
        )
        / denominator
    )
    return max(0.0, center - margin), min(1.0, center + margin)


def _numeric_percentile(values: Sequence[float], percentile: float) -> float:
    ordered = sorted(float(value) for value in values)
    if not ordered:
        return 0.0
    index = max(0, min(len(ordered) - 1, math.ceil(percentile * len(ordered)) - 1))
    return ordered[index]


def aggregate_trial_scores(
    trials: Sequence[Mapping[str, Any]],
    *,
    metric_names: Sequence[str] | None = None,
    binary_metrics: Sequence[str] | None = None,
) -> dict[str, Any]:
    """Aggregate safe trial facts without retaining candidate payloads.

    ``trials`` may either contain metrics directly or under a ``metrics`` key.
    Every returned metric is numeric; binary metrics additionally receive a
    Wilson 95% interval.  The function is deliberately independent of the
    Laminar SDK so offline tests and post-run SQL projection can share it.
    """

    metric_sources: list[Mapping[str, Any]] = []
    for trial in trials:
        if not isinstance(trial, Mapping):
            continue
        source = trial.get("metrics")
        if isinstance(source, Mapping):
            # Keep top-level safe outcome flags available to aggregators while
            # preserving the existing nested metric contract.
            merged = dict(source)
            for key in ("strict_pass", "candidate_pass"):
                if key in trial and key not in merged:
                    value = trial[key]
                    merged[key] = float(value) if isinstance(value, bool) else value
            metric_sources.append(merged)
        else:
            metric_sources.append(trial)
    if metric_names is None:
        names: set[str] = set()
        for source in metric_sources:
            names.update(
                key
                for key, value in source.items()
                if isinstance(key, str)
                and isinstance(value, int | float)
                and not isinstance(value, bool)
            )
        metric_names = tuple(sorted(names))
    # Durations and counts can legitimately be zero or one; only classify
    # explicitly named outcome/funnel fields as Bernoulli observations.
    binary_set = {
        "candidate_pass",
        "strict_pass",
        "generation_success",
        "independent_acceptance",
        "public_issue_free",
        "record_count_match",
        "records_exact_match",
        "schema_contract_match",
        "finish_called",
        "first_ttp_passed",
        "trace_id_consistent",
        *(binary_metrics or ()),
    }
    result: dict[str, Any] = {
        "trial_count": len(metric_sources),
        "metrics": {},
        "binary": {},
    }
    for name in metric_names:
        values = [
            float(source[name])
            for source in metric_sources
            if isinstance(source.get(name), int | float)
            and not isinstance(source.get(name), bool)
            and math.isfinite(float(source[name]))
        ]
        if not values:
            continue
        result["metrics"][name] = {
            "count": len(values),
            "mean": sum(values) / len(values),
            "min": min(values),
            "max": max(values),
            "p50": _numeric_percentile(values, 0.50),
            "p95": _numeric_percentile(values, 0.95),
            "p99": _numeric_percentile(values, 0.99),
        }
        if name in binary_set:
            successes = sum(value == 1.0 for value in values)
            lower, upper = wilson_interval(successes, len(values))
            result["binary"][name] = {
                "successes": successes,
                "observations": len(values),
                "rate": successes / len(values),
                "wilson_95": {"lower": lower, "upper": upper},
            }
    return result


def summarize_span_metrics(spans: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Summarize safe span timings and LLM context-token growth.

    The function consumes SQL-projected numeric columns only.  It never reads
    span input/output payloads, so callers can use it for local summaries.
    Phase durations are used for the root coverage estimate because nested
    context-fit/round/LLM/TOOL spans would otherwise be double-counted.
    """

    segment_names = (
        "ttp.generate",
        "schema.phase",
        "ttp.phase",
        "context.fit",
        "agent.round",
        "generation.deadline_cleanup",
        "final.acceptance",
        "LLM",
        "TOOL",
    )
    durations: dict[str, list[float]] = {name: [] for name in segment_names}
    llm_tokens: list[tuple[float, float]] = []
    for ordinal, span in enumerate(spans):
        if not isinstance(span, Mapping):
            continue
        name = span.get("name")
        span_type = span.get("span_type")
        if not isinstance(name, str):
            name = ""
        if not isinstance(span_type, str):
            span_type = ""
        if name in durations:
            segment = str(name)
        elif span_type in {"LLM", "TOOL"}:
            segment = str(span_type)
        else:
            continue
        try:
            duration = float(span.get("duration", 0.0) or 0.0)
        except (TypeError, ValueError):
            duration = 0.0
        if math.isfinite(duration) and duration >= 0.0:
            durations[segment].append(duration)
        if segment == "LLM":
            try:
                input_tokens = float(span.get("input_tokens", 0.0) or 0.0)
            except (TypeError, ValueError):
                input_tokens = 0.0
            try:
                start_order = float(span.get("start_time", ordinal) or ordinal)
            except (TypeError, ValueError):
                start_order = float(ordinal)
            if math.isfinite(input_tokens) and input_tokens >= 0.0:
                llm_tokens.append((start_order, input_tokens))

    segment_stats: dict[str, dict[str, float | int]] = {}
    for name, values in durations.items():
        if not values:
            continue
        segment_stats[name] = {
            "count": len(values),
            "total_seconds": sum(values),
            "p50_seconds": _numeric_percentile(values, 0.50),
            "p95_seconds": _numeric_percentile(values, 0.95),
            "p99_seconds": _numeric_percentile(values, 0.99),
        }

    ordered_tokens = [value for _, value in sorted(llm_tokens)]
    token_growth: dict[str, float | int] = {
        "observations": len(ordered_tokens),
        "first_input_tokens": ordered_tokens[0] if ordered_tokens else 0.0,
        "last_input_tokens": ordered_tokens[-1] if ordered_tokens else 0.0,
        "max_input_tokens": max(ordered_tokens, default=0.0),
        "growth_slope_tokens_per_call": (
            (ordered_tokens[-1] - ordered_tokens[0]) / (len(ordered_tokens) - 1)
            if len(ordered_tokens) > 1
            else 0.0
        ),
    }
    generation_seconds = float(
        segment_stats.get("ttp.generate", {}).get("total_seconds", 0.0),
    )
    explained_seconds = sum(
        float(segment_stats.get(name, {}).get("total_seconds", 0.0))
        for name in ("schema.phase", "ttp.phase", "final.acceptance")
    )
    unexplained_seconds = max(0.0, generation_seconds - explained_seconds)
    explained_ratio = (
        min(1.0, explained_seconds / generation_seconds)
        if generation_seconds > 0.0
        else 0.0
    )
    return {
        "segment_stats": segment_stats,
        "token_growth": token_growth,
        "explained_duration_seconds": explained_seconds,
        "unexplained_duration_seconds": unexplained_seconds,
        "explained_duration_ratio": explained_ratio,
        "unexplained_duration_ratio": max(0.0, 1.0 - explained_ratio),
    }


def issue_domain(code: Any) -> str:
    """Map a public issue code to a bounded diagnostic fault domain."""

    if not isinstance(code, str) or not code:
        return "unknown"
    prefix = code.split(".", 1)[0].casefold()
    if prefix in _ISSUE_DOMAIN_PREFIXES:
        return prefix
    if prefix in {"record", "records"} or code.startswith("record_"):
        return "records"
    if prefix in {"timeout", "cancel", "cleanup"}:
        return "budget"
    return "unknown"


def issue_taxonomy(codes: Sequence[Any]) -> dict[str, Any]:
    """Return safe issue-code and coarse-domain counts."""

    normalized = [code for code in codes if isinstance(code, str) and code]
    code_counts = Counter(normalized)
    domain_counts = Counter(issue_domain(code) for code in normalized)
    return {
        "total": len(normalized),
        "unique": len(code_counts),
        "codes": dict(sorted(code_counts.items())),
        "domains": dict(sorted(domain_counts.items())),
    }


def _schema_counter(nodes: Mapping[str, SchemaNode]) -> Counter[tuple[str, str, bool]]:
    return Counter((path, node.type, node.required) for path, node in nodes.items())


def _schema_path_counter(nodes: Mapping[str, SchemaNode]) -> Counter[str]:
    return Counter(path for path in nodes)


def _schema_type_counter(nodes: Mapping[str, SchemaNode]) -> Counter[tuple[str, str]]:
    return Counter((path, node.type) for path, node in nodes.items())


def _schema_required_counter(
    nodes: Mapping[str, SchemaNode],
) -> Counter[tuple[str, bool]]:
    return Counter((path, node.required) for path, node in nodes.items())


def _value_shape_counts(value: Any) -> dict[str, int]:
    """Count structural properties without retaining any scalar values."""

    counts = {
        "scalar_count": 0,
        "empty_string_count": 0,
        "null_count": 0,
        "empty_container_count": 0,
        "empty_object_count": 0,
        "empty_array_count": 0,
    }

    def visit(item: Any) -> None:
        if isinstance(item, dict):
            if not item:
                counts["empty_container_count"] += 1
                counts["empty_object_count"] += 1
            for child in item.values():
                visit(child)
        elif isinstance(item, list):
            if not item:
                counts["empty_container_count"] += 1
                counts["empty_array_count"] += 1
            for child in item:
                visit(child)
        else:
            counts["scalar_count"] += 1
            if item is None:
                counts["null_count"] += 1
            elif item == "":
                counts["empty_string_count"] += 1

    visit(value)
    return counts


def score_records_by_input(
    actual_records: Sequence[Any],
    expected_records: Sequence[Any],
) -> list[dict[str, Any]]:
    """Score each input-aligned record using only bounded numeric facts."""

    diagnostics: list[dict[str, Any]] = []
    for index, expected in enumerate(expected_records):
        actual_present = index < len(actual_records)
        actual = actual_records[index] if actual_present else None
        actual_counter = _leaf_counter(actual) if actual_present else Counter()
        expected_counter = _leaf_counter(expected)
        precision, recall, f1 = _precision_recall_f1(actual_counter, expected_counter)
        actual_shape = (
            _value_shape_counts(actual)
            if actual_present
            else {key: 0 for key in _value_shape_counts({})}
        )
        expected_shape = _value_shape_counts(expected)
        diagnostics.append(
            {
                "input_index": index,
                "actual_present": actual_present,
                "expected_present": True,
                "actual_root_object": isinstance(actual, dict),
                "expected_root_object": isinstance(expected, dict),
                "records_exact_match": bool(
                    actual_present
                    and _canonical_json(actual) == _canonical_json(expected)
                ),
                "leaf_precision": precision,
                "leaf_recall": recall,
                "leaf_f1": f1,
                "actual_leaf_count": sum(actual_counter.values()),
                "expected_leaf_count": sum(expected_counter.values()),
                "actual_scalar_count": actual_shape["scalar_count"],
                "expected_scalar_count": expected_shape["scalar_count"],
                "actual_empty_string_count": actual_shape["empty_string_count"],
                "actual_null_count": actual_shape["null_count"],
                "actual_empty_container_count": actual_shape["empty_container_count"],
            },
        )
    return diagnostics


def score_ttp_template_output(
    output: Any,
    expected_records: Sequence[Any],
) -> dict[str, Any]:
    """Score a TTP-only trial without treating its supplied Schema as a metric."""

    zero_metrics = {
        "candidate_pass": 0.0,
        "generation_success": 0.0,
        "independent_acceptance": 0.0,
        "record_count_match": 0.0,
        "records_exact_match": 0.0,
        "leaf_precision": 0.0,
        "leaf_recall": 0.0,
        "leaf_f1": 0.0,
        "input_count": float(len(expected_records)),
        "input_present_count": 0.0,
        "input_exact_match_count": 0.0,
        "input_exact_match_rate": 0.0,
        "input_leaf_precision_macro": 0.0,
        "input_leaf_recall_macro": 0.0,
        "input_leaf_f1_macro": 0.0,
        "finish_called": 0.0,
        "first_ttp_passed": 0.0,
        "elapsed_seconds": 0.0,
        "agent_rounds": 0.0,
        "ttp_agent_rounds": 0.0,
        "tool_call_starts": 0.0,
        "tool_result_errors": 0.0,
        "ttp_submissions": 0.0,
        "ttp_test_calls": 0.0,
        "ttp_no_tool_responses": 0.0,
        "ttp_no_tool_retries": 0.0,
        "model_retries_observed": 0.0,
    }
    raw_result = (
        output.get("generation_result") if isinstance(output, Mapping) else None
    )
    acceptance = (
        output.get("independent_acceptance") if isinstance(output, Mapping) else None
    )
    if not isinstance(raw_result, Mapping) or not isinstance(acceptance, Mapping):
        return {
            "metrics": zero_metrics,
            "inputs": score_records_by_input((), expected_records),
        }

    artifact = raw_result.get("artifact")
    actual_records = (
        artifact.get("records")
        if isinstance(artifact, Mapping) and isinstance(artifact.get("records"), list)
        else []
    )
    generation_success = raw_result.get("status") == "success"
    acceptance_valid = acceptance.get("valid") is True
    count_matches = len(actual_records) == len(expected_records)
    records_exact = _canonical_json(actual_records) == _canonical_json(expected_records)
    leaf_precision, leaf_recall, leaf_f1 = _precision_recall_f1(
        _leaf_counter(actual_records),
        _leaf_counter(expected_records),
    )
    diagnostics = score_records_by_input(actual_records, expected_records)
    input_count = len(diagnostics)
    exact_count = sum(item["records_exact_match"] for item in diagnostics)
    present_count = sum(item["actual_present"] for item in diagnostics)
    metadata = raw_result.get("metadata")
    if not isinstance(metadata, Mapping):
        metadata = {}
    metrics = dict(zero_metrics)
    metrics.update(
        candidate_pass=float(generation_success and acceptance_valid and records_exact),
        generation_success=float(generation_success),
        independent_acceptance=float(acceptance_valid),
        record_count_match=float(count_matches),
        records_exact_match=float(records_exact),
        leaf_precision=leaf_precision,
        leaf_recall=leaf_recall,
        leaf_f1=leaf_f1,
        input_present_count=float(present_count),
        input_exact_match_count=float(exact_count),
        input_exact_match_rate=float(exact_count / input_count) if input_count else 0.0,
        input_leaf_precision_macro=(
            sum(item["leaf_precision"] for item in diagnostics) / input_count
            if input_count
            else 0.0
        ),
        input_leaf_recall_macro=(
            sum(item["leaf_recall"] for item in diagnostics) / input_count
            if input_count
            else 0.0
        ),
        input_leaf_f1_macro=(
            sum(item["leaf_f1"] for item in diagnostics) / input_count
            if input_count
            else 0.0
        ),
        finish_called=float(
            generation_success and metadata.get("termination_reason") == "success",
        ),
        first_ttp_passed=float(metadata.get("first_ttp_passed") is True),
    )
    for name in (
        "elapsed_seconds",
        "agent_rounds",
        "ttp_agent_rounds",
        "tool_call_starts",
        "tool_result_errors",
        "ttp_submissions",
        "ttp_test_calls",
        "ttp_no_tool_responses",
        "ttp_no_tool_retries",
        "model_retries_observed",
    ):
        value = metadata.get(name, 0)
        if isinstance(value, int | float) and not isinstance(value, bool):
            metrics[name] = float(value)
    return {
        "metrics": metrics,
        "inputs": diagnostics,
        "extra_actual_input_count": max(0, len(actual_records) - len(expected_records)),
    }


def _json_mapping(value: Any) -> Mapping[str, Any]:
    if isinstance(value, Mapping):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except (TypeError, ValueError):
            return {}
        return parsed if isinstance(parsed, Mapping) else {}
    return {}


def _candidate_payload(span: Mapping[str, Any]) -> Mapping[str, Any]:
    payload = span.get("output")
    if isinstance(payload, Mapping):
        return payload
    if isinstance(payload, str):
        return _json_mapping(payload)
    # SQL projections may already expose the tool result as the row itself.
    return span


def project_candidate_quality(
    span: Mapping[str, Any],
    *,
    expected_records: Sequence[Any] | None = None,
) -> dict[str, Any]:
    """Project one TTP tool span into safe candidate-quality facts.

    The projection never returns the template, captures, messages, or scalar
    values.  ``expected_records`` is used only to calculate numeric comparison
    metrics and is not copied into the result.
    """

    payload = _candidate_payload(span)
    capture = _json_mapping(payload.get("capture"))
    records = capture.get("records")
    records_value = records if isinstance(records, list) else []
    shape = _value_shape_counts(records_value)
    issue_values = payload.get("issues")
    issue_codes = (
        [
            issue.get("code")
            for issue in issue_values
            if (
                isinstance(issue, Mapping)
                and isinstance(issue.get("code"), str)
                and len(issue["code"]) <= 128
                and _SAFE_ISSUE_CODE_RE.fullmatch(issue["code"]) is not None
            )
        ]
        if isinstance(issue_values, list)
        else []
    )
    result: dict[str, Any] = {
        "phase": "ttp",
        "accepted": payload.get("accepted") is True,
        "candidate_available": payload.get("validated_candidate_available") is True,
        "submission_index": (
            payload.get("ttp_submission")
            if isinstance(payload.get("ttp_submission"), int)
            and not isinstance(payload.get("ttp_submission"), bool)
            else None
        ),
        "issue_codes": sorted(set(issue_codes)),
        "issue_domains": issue_taxonomy(issue_codes)["domains"],
        "capture_available": capture.get("available") is True,
        "capture_complete": capture.get("complete") is True,
        "capture_record_count": len(records_value),
        "capture_nonempty_record_count": sum(
            isinstance(record, dict) and bool(record) for record in records_value
        ),
        "capture_empty_container_count": shape["empty_container_count"],
        "capture_empty_string_count": shape["empty_string_count"],
        "capture_null_count": shape["null_count"],
        "capture_scalar_count": shape["scalar_count"],
    }
    if expected_records is not None:
        precision, recall, f1 = _precision_recall_f1(
            _leaf_counter(records_value),
            _leaf_counter(list(expected_records)),
        )
        result.update(
            capture_record_count_match=len(records_value) == len(expected_records),
            capture_records_exact_match=(
                _canonical_json(records_value)
                == _canonical_json(list(expected_records))
            ),
            capture_leaf_precision=precision,
            capture_leaf_recall=recall,
            capture_leaf_f1=f1,
        )
    return result


def _project_schema_quality(span: Mapping[str, Any]) -> dict[str, Any]:
    """Project a Schema submission without retaining raw schema data."""

    payload = _candidate_payload(span)
    issue_values = payload.get("issues")
    issue_codes = (
        [
            issue.get("code")
            for issue in issue_values
            if (
                isinstance(issue, Mapping)
                and isinstance(issue.get("code"), str)
                and len(issue["code"]) <= 128
                and _SAFE_ISSUE_CODE_RE.fullmatch(issue["code"]) is not None
            )
        ]
        if isinstance(issue_values, list)
        else []
    )
    submission_index = payload.get("schema_submission")
    if not isinstance(submission_index, int) or isinstance(
        submission_index,
        bool,
    ):
        submission_index = None
    return {
        "phase": "schema",
        "accepted": payload.get("accepted") is True,
        "candidate_available": payload.get("frozen") is True,
        "frozen": payload.get("frozen") is True,
        "submission_index": submission_index,
        "issue_codes": sorted(set(issue_codes)),
        "issue_domains": issue_taxonomy(issue_codes)["domains"],
    }


def project_candidate_trajectory(
    spans: Sequence[Mapping[str, Any]],
    *,
    expected_records: Sequence[Any] | None = None,
) -> dict[str, Any]:
    """Summarize all candidate submissions in a Trace without raw content."""

    candidates: list[dict[str, Any]] = []
    schema_candidates: list[dict[str, Any]] = []
    finish_called = False
    for ordinal, span in enumerate(spans):
        if not isinstance(span, Mapping):
            continue
        name = str(span.get("name", ""))
        payload = _candidate_payload(span)
        if name in {"finish_generation", "generation.finish_generation"}:
            finish_called = True
            continue
        if (
            name
            in {
                "submit_result_schema",
                "schema.submit_result_schema",
            }
            or "schema_submission" in payload
        ):
            schema_quality = _project_schema_quality(span)
            if schema_quality["submission_index"] is None:
                schema_quality["submission_index"] = ordinal + 1
            schema_candidates.append(schema_quality)
            continue
        if (
            name
            and name not in {"submit_ttp_template", "ttp.submit_ttp_template"}
            and "ttp_submission" not in payload
        ):
            continue
        if "ttp_submission" not in payload and "accepted" not in payload:
            continue
        quality = project_candidate_quality(span, expected_records=expected_records)
        if quality["submission_index"] is None:
            quality["submission_index"] = ordinal + 1
        candidates.append(quality)
    candidates.sort(key=lambda item: int(item["submission_index"]))
    schema_candidates.sort(key=lambda item: int(item["submission_index"]))
    accepted_indices = [
        int(item["submission_index"]) for item in candidates if item["accepted"]
    ]
    first_accepted = accepted_indices[0] if accepted_indices else None
    return {
        "schema_submission_count": len(schema_candidates),
        "schema_accepted_count": sum(item["accepted"] for item in schema_candidates),
        "schema_candidates": schema_candidates,
        "submission_count": len(candidates),
        "accepted_count": len(accepted_indices),
        "candidate_available_count": sum(
            item["candidate_available"] for item in candidates
        ),
        "first_accepted_submission": first_accepted,
        "last_accepted_submission": accepted_indices[-1] if accepted_indices else None,
        "accepted_indices": accepted_indices,
        "finish_called": finish_called,
        "finish_after_first_accepted": bool(
            finish_called and first_accepted is not None
        ),
        "issue_domains": dict(
            sorted(
                Counter(
                    domain for item in candidates for domain in item["issue_domains"]
                ).items(),
            ),
        ),
        "candidates": candidates,
    }


def project_human_reviews(
    spans: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    """Project Laminar HumanEvaluator spans into bounded review facts.

    Reviewers may annotate the same submission more than once.  The raw
    annotations stay in Laminar; this projection retains only safe labels,
    bounded dimensions and issue-code counts for the local evaluation report.
    """

    by_submission: dict[tuple[str, int], list[dict[str, Any]]] = {}
    for span in spans:
        if not isinstance(span, Mapping):
            continue
        payload = _json_mapping(span.get("output"))
        attributes = _json_mapping(span.get("attributes"))
        attribute_values = {
            key.rsplit(".", 1)[-1]: value
            for key, value in attributes.items()
            if isinstance(key, str)
        }

        raw_index = payload.get("submission_index")
        raw_phase = payload.get("phase")
        if raw_phase is None:
            raw_phase = _json_mapping(span.get("input")).get("phase")
        if raw_phase is None:
            raw_phase = attribute_values.get("review_phase", "ttp")
        if raw_phase not in _REVIEW_PHASES:
            continue
        if raw_index is None:
            raw_index = _json_mapping(span.get("input")).get(
                "submission_index",
            )
        if raw_index is None:
            raw_index = attribute_values.get("review_submission_index")
        if (
            not isinstance(raw_index, int)
            or isinstance(raw_index, bool)
            or raw_index < 1
        ):
            continue
        label = payload.get("label")
        if not isinstance(label, str):
            label = attribute_values.get("review_label")
        if label not in _REVIEW_LABELS:
            continue

        raw_dimensions = payload.get("dimensions")
        if raw_dimensions is None:
            raw_dimensions = attribute_values.get("review_dimensions")
        dimensions = _json_mapping(raw_dimensions)
        safe_dimensions = {
            key: value
            for key, value in dimensions.items()
            if (
                isinstance(key, str)
                and _REVIEW_DIMENSION_RE.fullmatch(key)
                and isinstance(value, str)
                and _REVIEW_VALUE_RE.fullmatch(value)
            )
        }

        raw_issue_codes = payload.get("issue_codes")
        if raw_issue_codes is None:
            raw_issue_codes = attribute_values.get("review_issue_codes")
        if isinstance(raw_issue_codes, str):
            try:
                raw_issue_codes = json.loads(raw_issue_codes)
            except (TypeError, ValueError):
                raw_issue_codes = []
        safe_issue_codes = sorted(
            {
                code
                for code in (raw_issue_codes or ())
                if isinstance(code, str)
                and len(code) <= 128
                and _SAFE_ISSUE_CODE_RE.fullmatch(code)
            },
        )
        by_submission.setdefault((raw_phase, raw_index), []).append(
            {
                "phase": raw_phase,
                "label": label,
                "dimensions": safe_dimensions,
                "issue_codes": safe_issue_codes,
            },
        )

    submissions: dict[str, dict[str, Any]] = {}
    label_counts: Counter[str] = Counter()
    issue_counts: Counter[str] = Counter()
    for (phase, index), reviews in sorted(by_submission.items()):
        labels = Counter(review["label"] for review in reviews)
        # Stable tie-breaking keeps reports reproducible when multiple
        # reviewers disagree.
        selected_label = max(
            labels,
            key=lambda label: (
                labels[label],
                {"reasonable": 2, "repairable": 1, "unreasonable": 0}[label],
            ),
        )
        dimensions: dict[str, Counter[str]] = {}
        for review in reviews:
            for key, value in review["dimensions"].items():
                dimensions.setdefault(key, Counter())[value] += 1
            issue_counts.update(review["issue_codes"])
        label_counts.update(labels)
        submission_key = str(index) if phase == "ttp" else f"{phase}:{index}"
        submissions[submission_key] = {
            "review_count": len(reviews),
            "phase": phase,
            "label": selected_label,
            "label_counts": dict(sorted(labels.items())),
            "dimensions": {
                key: dict(sorted(values.items()))
                for key, values in sorted(dimensions.items())
            },
            "issue_codes": sorted(
                {code for review in reviews for code in review["issue_codes"]},
            ),
        }
    return {
        "review_count": sum(len(reviews) for reviews in by_submission.values()),
        "reviewed_submission_count": len(submissions),
        "label_counts": dict(sorted(label_counts.items())),
        "issue_codes": dict(sorted(issue_counts.items())),
        "submissions": submissions,
    }


def attach_human_reviews(
    trajectory: Mapping[str, Any],
    reviews: Mapping[str, Any],
) -> dict[str, Any]:
    """Attach safe per-submission review facts to a candidate trajectory."""

    result = dict(trajectory)
    review_submissions = reviews.get("submissions")
    if not isinstance(review_submissions, Mapping):
        return result
    candidates = []
    for collection_name in ("candidates", "schema_candidates"):
        collection = []
        for candidate in trajectory.get(collection_name, ()):
            if not isinstance(candidate, Mapping):
                continue
            projected = dict(candidate)
            index = projected.get("submission_index")
            phase = projected.get("phase", "ttp")
            review_key = f"schema:{index}" if phase == "schema" else str(index)
            review = review_submissions.get(review_key)
            if isinstance(review, Mapping):
                projected["human_review"] = dict(review)
            collection.append(projected)
        if collection_name == "candidates":
            candidates = collection
        else:
            result["schema_candidates"] = collection
    result["candidates"] = candidates
    result["human_review"] = {
        key: value for key, value in reviews.items() if key != "submissions"
    }
    return result


def independent_acceptance(
    result: GenerationResult,
    command_outputs: Sequence[str],
    policy: GenerationPolicy,
) -> dict[str, Any]:
    """Repeat full deterministic acceptance outside the Agent."""

    if result.status != "success" or result.artifact is None:
        return {
            "valid": False,
            "schema_valid": False,
            "ttp_valid": False,
            "record_count_matches": False,
            "records_match_artifact": False,
            "issue_codes": [],
        }
    artifact = result.artifact
    schema_issues = validate_result_schema(
        artifact.result_schema,
        max_schema_bytes=policy.max_schema_bytes,
        max_schema_depth=policy.max_schema_depth,
        max_schema_properties=policy.max_schema_properties,
    )
    if schema_issues:
        return {
            "valid": False,
            "schema_valid": False,
            "ttp_valid": False,
            "record_count_matches": False,
            "records_match_artifact": False,
            "issue_codes": [issue.code for issue in schema_issues],
        }
    validation = validate_ttp_template(
        artifact.ttp_template,
        command_outputs,
        artifact.result_schema,
        timeout_seconds=policy.ttp_validation_timeout_seconds,
        max_result_bytes=policy.max_parse_result_bytes,
        max_ttp_template_bytes=policy.max_ttp_template_bytes,
        max_ttp_group_depth=policy.max_ttp_group_depth,
        max_ttp_regex_chars=policy.max_ttp_regex_chars,
        max_ttp_argument_chars=policy.max_ttp_argument_chars,
        max_schema_bytes=policy.max_schema_bytes,
        max_schema_depth=policy.max_schema_depth,
        max_schema_properties=policy.max_schema_properties,
    )
    count_matches = len(validation.records) == len(command_outputs)
    records_match = list(validation.records) == artifact.records
    return {
        "valid": validation.valid and count_matches and records_match,
        "schema_valid": True,
        "ttp_valid": validation.valid,
        "record_count_matches": count_matches,
        "records_match_artifact": records_match,
        "issue_codes": [issue.code for issue in validation.issues],
    }


def score_executor_output(output: Any, target: Any) -> dict[str, float]:
    """Return only numeric Laminar scores for one completed executor call."""

    zero = {
        "candidate_pass": 0.0,
        "generation_success": 0.0,
        "independent_acceptance": 0.0,
        "public_issue_free": 0.0,
        "record_count_match": 0.0,
        "records_exact_match": 0.0,
        "leaf_precision": 0.0,
        "leaf_recall": 0.0,
        "leaf_f1": 0.0,
        "schema_contract_match": 0.0,
        "schema_path_precision": 0.0,
        "schema_path_recall": 0.0,
        "schema_path_f1": 0.0,
        # ``schema_path_*`` above preserves the historical tuple contract
        # (path + type + required).  These projections separate the three
        # dimensions for diagnostic reporting without breaking old consumers.
        "schema_path_only_precision": 0.0,
        "schema_path_only_recall": 0.0,
        "schema_path_only_f1": 0.0,
        "schema_type_precision": 0.0,
        "schema_type_recall": 0.0,
        "schema_type_f1": 0.0,
        "schema_required_precision": 0.0,
        "schema_required_recall": 0.0,
        "schema_required_f1": 0.0,
        "input_count": 0.0,
        "input_present_count": 0.0,
        "input_exact_match_count": 0.0,
        "input_exact_match_rate": 0.0,
        "input_leaf_precision_macro": 0.0,
        "input_leaf_recall_macro": 0.0,
        "input_leaf_f1_macro": 0.0,
        "finish_called": 0.0,
        "first_ttp_passed": 0.0,
        "elapsed_seconds": 0.0,
        "agent_rounds": 0.0,
        "schema_agent_rounds": 0.0,
        "ttp_agent_rounds": 0.0,
        "tool_call_starts": 0.0,
        "tool_result_errors": 0.0,
        "schema_submissions": 0.0,
        "ttp_submissions": 0.0,
        "ttp_test_calls": 0.0,
        "schema_no_tool_responses": 0.0,
        "ttp_no_tool_responses": 0.0,
        "schema_no_tool_retries": 0.0,
        "ttp_no_tool_retries": 0.0,
        "model_retries_observed": 0.0,
    }
    if not isinstance(output, Mapping) or not isinstance(target, Mapping):
        return zero
    raw_result = output.get("generation_result")
    acceptance = output.get("independent_acceptance")
    expected_records = target.get("records")
    expected_contract = target.get("schema_contract")
    if (
        not isinstance(raw_result, Mapping)
        or not isinstance(acceptance, Mapping)
        or not isinstance(expected_records, list)
        or not isinstance(expected_contract, list)
    ):
        return zero

    generation_success = raw_result.get("status") == "success"
    issues = raw_result.get("issues")
    public_issue_free = isinstance(issues, list) and not any(
        isinstance(issue, Mapping) and issue.get("severity") == "error"
        for issue in issues
    )
    artifact = raw_result.get("artifact")
    actual_records: list[Any] = []
    actual_schema: Mapping[str, Any] | None = None
    if isinstance(artifact, Mapping):
        records_value = artifact.get("records")
        schema_value = artifact.get("result_schema")
        if isinstance(records_value, list):
            actual_records = records_value
        if isinstance(schema_value, Mapping):
            actual_schema = schema_value

    record_count_match = len(actual_records) == len(expected_records)
    records_exact_match = _canonical_json(actual_records) == _canonical_json(
        expected_records,
    )
    leaf_precision, leaf_recall, leaf_f1 = _precision_recall_f1(
        _leaf_counter(actual_records),
        _leaf_counter(expected_records),
    )

    expected_nodes: dict[str, SchemaNode] = {}
    try:
        for raw_node in expected_contract:
            if not isinstance(raw_node, Mapping):
                raise HarnessError("invalid expected schema contract")
            node = SchemaNode(
                path=str(raw_node["path"]),
                type=raw_node["type"],
                required=raw_node["required"],
            )
            expected_nodes[node.path] = node
        actual_nodes = schema_signature(actual_schema or {})
    except (HarnessError, KeyError, TypeError):
        actual_nodes = {}
    schema_precision, schema_recall, schema_f1 = _precision_recall_f1(
        _schema_counter(actual_nodes),
        _schema_counter(expected_nodes),
    )
    schema_path_only = _precision_recall_f1(
        _schema_path_counter(actual_nodes),
        _schema_path_counter(expected_nodes),
    )
    schema_type = _precision_recall_f1(
        _schema_type_counter(actual_nodes),
        _schema_type_counter(expected_nodes),
    )
    schema_required = _precision_recall_f1(
        _schema_required_counter(actual_nodes),
        _schema_required_counter(expected_nodes),
    )
    schema_contract_match = actual_nodes == expected_nodes

    input_diagnostics = score_records_by_input(actual_records, expected_records)
    input_count = len(input_diagnostics)
    input_exact_match_count = sum(
        item["records_exact_match"] for item in input_diagnostics
    )
    input_present_count = sum(item["actual_present"] for item in input_diagnostics)
    input_leaf_precision_macro = (
        sum(item["leaf_precision"] for item in input_diagnostics) / input_count
        if input_count
        else 0.0
    )
    input_leaf_recall_macro = (
        sum(item["leaf_recall"] for item in input_diagnostics) / input_count
        if input_count
        else 0.0
    )
    input_leaf_f1_macro = (
        sum(item["leaf_f1"] for item in input_diagnostics) / input_count
        if input_count
        else 0.0
    )

    metadata = raw_result.get("metadata")
    if not isinstance(metadata, Mapping):
        metadata = {}
    independent_valid = acceptance.get("valid") is True
    candidate_pass = all(
        (
            generation_success,
            independent_valid,
            public_issue_free,
            record_count_match,
            records_exact_match,
            schema_contract_match,
        ),
    )
    scores = dict(zero)
    scores.update(
        candidate_pass=float(candidate_pass),
        generation_success=float(generation_success),
        independent_acceptance=float(independent_valid),
        public_issue_free=float(public_issue_free),
        record_count_match=float(record_count_match),
        records_exact_match=float(records_exact_match),
        leaf_precision=leaf_precision,
        leaf_recall=leaf_recall,
        leaf_f1=leaf_f1,
        schema_contract_match=float(schema_contract_match),
        schema_path_precision=schema_precision,
        schema_path_recall=schema_recall,
        schema_path_f1=schema_f1,
        schema_path_only_precision=schema_path_only[0],
        schema_path_only_recall=schema_path_only[1],
        schema_path_only_f1=schema_path_only[2],
        schema_type_precision=schema_type[0],
        schema_type_recall=schema_type[1],
        schema_type_f1=schema_type[2],
        schema_required_precision=schema_required[0],
        schema_required_recall=schema_required[1],
        schema_required_f1=schema_required[2],
        input_count=float(input_count),
        input_present_count=float(input_present_count),
        input_exact_match_count=float(input_exact_match_count),
        input_exact_match_rate=(
            float(input_exact_match_count / input_count) if input_count else 0.0
        ),
        input_leaf_precision_macro=input_leaf_precision_macro,
        input_leaf_recall_macro=input_leaf_recall_macro,
        input_leaf_f1_macro=input_leaf_f1_macro,
        finish_called=float(
            generation_success and metadata.get("termination_reason") == "success",
        ),
        first_ttp_passed=float(metadata.get("first_ttp_passed") is True),
    )
    for name in (
        "elapsed_seconds",
        "agent_rounds",
        "schema_agent_rounds",
        "ttp_agent_rounds",
        "tool_call_starts",
        "tool_result_errors",
        "schema_submissions",
        "ttp_submissions",
        "ttp_test_calls",
        "schema_no_tool_responses",
        "ttp_no_tool_responses",
        "schema_no_tool_retries",
        "ttp_no_tool_retries",
        "model_retries_observed",
    ):
        value = metadata.get(name, 0)
        if isinstance(value, int | float) and not isinstance(value, bool):
            scores[name] = float(value)
    return scores


def score_executor_output_details(output: Any, target: Any) -> dict[str, Any]:
    """Return numeric trial scores plus safe per-input diagnostics.

    The existing :func:`score_executor_output` remains the Laminar evaluator
    contract and returns numeric values only.  This richer projection is for
    post-run reporting; it intentionally excludes records, schemas, captures,
    templates, and model text.
    """

    scores = score_executor_output(output, target)
    actual_records: Sequence[Any] = ()
    expected_records: Sequence[Any] = ()
    raw_result = (
        output.get("generation_result") if isinstance(output, Mapping) else None
    )
    artifact = raw_result.get("artifact") if isinstance(raw_result, Mapping) else None
    if isinstance(artifact, Mapping) and isinstance(artifact.get("records"), list):
        actual_records = artifact["records"]
    if isinstance(target, Mapping) and isinstance(target.get("records"), list):
        expected_records = target["records"]
    diagnostics = score_records_by_input(actual_records, expected_records)
    codes: list[str] = []
    if isinstance(raw_result, Mapping) and isinstance(raw_result.get("issues"), list):
        codes.extend(
            issue["code"]
            for issue in raw_result["issues"]
            if isinstance(issue, Mapping) and isinstance(issue.get("code"), str)
        )
    acceptance = (
        output.get("independent_acceptance") if isinstance(output, Mapping) else None
    )
    if isinstance(acceptance, Mapping) and isinstance(
        acceptance.get("issue_codes"), list
    ):
        codes.extend(
            code for code in acceptance["issue_codes"] if isinstance(code, str)
        )
    taxonomy = issue_taxonomy(codes)
    return {
        "scores": scores,
        "inputs": diagnostics,
        "extra_actual_input_count": max(0, len(actual_records) - len(expected_records)),
        "issue_taxonomy": taxonomy,
    }


def safe_trial_facts(
    output: Mapping[str, Any],
    scores: Mapping[str, float],
) -> dict[str, Any]:
    """Project a full executor result into a local, non-sensitive summary."""

    result = output.get("generation_result")
    exception_type = output.get("exception_type")
    if not isinstance(result, Mapping):
        return {
            "candidate_pass": False,
            "failure_category": "runner",
            "exception_type": str(exception_type or "unknown"),
            "termination_reason": "exception",
            "fault_domain": None,
            "issue_codes": [],
            "issue_taxonomy": issue_taxonomy(()),
            "last_attempt_present": False,
            "metrics": dict(scores),
        }
    metadata = result.get("metadata")
    if not isinstance(metadata, Mapping):
        metadata = {}
    raw_issues = result.get("issues")
    issue_codes = (
        [
            str(issue.get("code"))
            for issue in raw_issues
            if isinstance(issue, Mapping) and isinstance(issue.get("code"), str)
        ]
        if isinstance(raw_issues, list)
        else []
    )
    acceptance = output.get("independent_acceptance")
    acceptance_codes = (
        acceptance.get("issue_codes", []) if isinstance(acceptance, Mapping) else []
    )
    for code in acceptance_codes:
        if isinstance(code, str) and code not in issue_codes:
            issue_codes.append(code)
    if scores.get("candidate_pass") == 1.0:
        category = None
    elif result.get("status") != "success":
        category = "generation"
        if any(code.startswith("schema.") for code in issue_codes):
            category = "schema"
        elif any(code.startswith("ttp.") for code in issue_codes):
            category = "ttp"
        elif any(code.startswith("model.") for code in issue_codes):
            category = "model"
    elif scores.get("independent_acceptance") != 1.0:
        category = "acceptance"
    elif scores.get("records_exact_match") != 1.0:
        category = "records"
    else:
        category = "schema_contract"
    return {
        "candidate_pass": scores.get("candidate_pass") == 1.0,
        "failure_category": category,
        "exception_type": None,
        "termination_reason": metadata.get("termination_reason"),
        "fault_domain": metadata.get("fault_domain"),
        "issue_codes": issue_codes,
        "issue_taxonomy": issue_taxonomy(issue_codes),
        "last_attempt_present": result.get("last_attempt") is not None,
        "metrics": dict(scores),
    }
