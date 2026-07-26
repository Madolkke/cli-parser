"""Strict local contracts and deterministic scoring for Agent evaluations."""

from __future__ import annotations

import hashlib
import json
import re
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Literal

from cli_parser_agent import GenerationPolicy, GenerationResult
from cli_parser_agent.ttp_generation.validation import (
    validate_result_schema,
    validate_ttp_template,
)

MANIFEST_VERSION = 1
MAX_INPUTS = 5
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


def _collect_record_structure(
    value: Any,
    path: str,
    *,
    required: bool,
    collected: dict[str, SchemaNode],
) -> None:
    node = SchemaNode(path=path, type=_json_type(value), required=required)
    previous = collected.get(path)
    if previous is not None and previous != node:
        raise HarnessError(f"target records disagree about structure at {path}")
    collected[path] = node
    if isinstance(value, dict):
        if not value:
            raise HarnessError(f"target contains an empty object at {path}")
        for key, child in value.items():
            if not _FIELD_RE.fullmatch(key):
                raise HarnessError(f"target field is not ASCII snake_case: {key}")
            child_path = f"/{key}" if path == "/" else f"{path}/{key}"
            _collect_record_structure(
                child,
                child_path,
                required=True,
                collected=collected,
            )
    elif isinstance(value, list):
        if not value:
            raise HarnessError(f"target contains an empty array at {path}")
        for child in value:
            _collect_record_structure(
                child,
                f"{path}/*",
                required=False,
                collected=collected,
            )


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
        elif parent.type != "object" or not node.required:
            raise HarnessError(f"object property contract is invalid at {path}")

    observed: dict[str, SchemaNode] = {}
    for record in records:
        _collect_record_structure(record, "/", required=False, collected=observed)
    if observed != declared:
        missing = sorted(observed.keys() - declared.keys())
        extra = sorted(declared.keys() - observed.keys())
        mismatched = sorted(
            path
            for path in observed.keys() & declared.keys()
            if observed[path] != declared[path]
        )
        details = []
        if missing:
            details.append(f"missing={','.join(missing)}")
        if extra:
            details.append(f"extra={','.join(extra)}")
        if mismatched:
            details.append(f"mismatched={','.join(mismatched)}")
        raise HarnessError(
            "schema contract does not match expected records"
            + (f" ({'; '.join(details)})" if details else ""),
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
        if not isinstance(raw_inputs, list) or not 1 <= len(raw_inputs) <= MAX_INPUTS:
            raise HarnessError(f"{label}.inputs must contain 1 to {MAX_INPUTS} items")
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
            if not isinstance(properties, Mapping) or not isinstance(
                required_names,
                list,
            ):
                raise HarnessError(f"generated object schema is incomplete at {path}")
            required_set = set(required_names)
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
    f1 = (
        2 * precision * recall / (precision + recall)
        if precision + recall
        else 0.0
    )
    return precision, recall, f1


def _schema_counter(nodes: Mapping[str, SchemaNode]) -> Counter[tuple[str, str, bool]]:
    return Counter((path, node.type, node.required) for path, node in nodes.items())


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
        "schema_no_tool_responses": 0.0,
        "ttp_no_tool_responses": 0.0,
        "schema_no_tool_retries": 0.0,
        "ttp_no_tool_retries": 0.0,
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
    schema_contract_match = actual_nodes == expected_nodes

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
        "schema_no_tool_responses",
        "ttp_no_tool_responses",
        "schema_no_tool_retries",
        "ttp_no_tool_retries",
    ):
        value = metadata.get(name, 0)
        if isinstance(value, int | float) and not isinstance(value, bool):
            scores[name] = float(value)
    return scores


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
            "issue_codes": [],
            "last_attempt_present": False,
            "metrics": dict(scores),
        }
    metadata = result.get("metadata")
    if not isinstance(metadata, Mapping):
        metadata = {}
    raw_issues = result.get("issues")
    issue_codes = [
        str(issue.get("code"))
        for issue in raw_issues
        if isinstance(issue, Mapping) and isinstance(issue.get("code"), str)
    ] if isinstance(raw_issues, list) else []
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
        "issue_codes": issue_codes,
        "last_attempt_present": result.get("last_attempt") is not None,
        "metrics": dict(scores),
    }
