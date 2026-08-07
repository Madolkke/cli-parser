"""Strict local contracts and deterministic scoring for Agent evaluations."""

from __future__ import annotations

import hashlib
import json
import math
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
        actual_shape = _value_shape_counts(actual) if actual_present else {
            key: 0 for key in _value_shape_counts({})
        }
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
                "actual_empty_container_count": actual_shape[
                    "empty_container_count"
                ],
            },
        )
    return diagnostics


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
    issue_codes = [
        issue.get("code")
        for issue in issue_values
        if (
            isinstance(issue, Mapping)
            and isinstance(issue.get("code"), str)
            and len(issue["code"]) <= 128
            and _SAFE_ISSUE_CODE_RE.fullmatch(issue["code"]) is not None
        )
    ] if isinstance(issue_values, list) else []
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
    """Project a Schema submission without retaining schema/evidence data."""

    payload = _candidate_payload(span)
    issue_values = payload.get("issues")
    issue_codes = [
        issue.get("code")
        for issue in issue_values
        if (
            isinstance(issue, Mapping)
            and isinstance(issue.get("code"), str)
            and len(issue["code"]) <= 128
            and _SAFE_ISSUE_CODE_RE.fullmatch(issue["code"]) is not None
        )
    ] if isinstance(issue_values, list) else []
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
        if name in {
            "submit_result_schema",
            "schema.submit_result_schema",
        } or "schema_submission" in payload:
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
        int(item["submission_index"])
        for item in candidates
        if item["accepted"]
    ]
    first_accepted = accepted_indices[0] if accepted_indices else None
    return {
        "schema_submission_count": len(schema_candidates),
        "schema_accepted_count": sum(
            item["accepted"] for item in schema_candidates
        ),
        "schema_candidates": schema_candidates,
        "submission_count": len(candidates),
        "accepted_count": len(accepted_indices),
        "candidate_available_count": sum(
            item["candidate_available"] for item in candidates
        ),
        "first_accepted_submission": first_accepted,
        "last_accepted_submission": accepted_indices[-1]
        if accepted_indices
        else None,
        "accepted_indices": accepted_indices,
        "finish_called": finish_called,
        "finish_after_first_accepted": bool(
            finish_called and first_accepted is not None
        ),
        "issue_domains": dict(
            sorted(
                Counter(
                    domain
                    for item in candidates
                    for domain in item["issue_domains"]
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
            review_key = (
                f"schema:{index}" if phase == "schema" else str(index)
            )
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
        key: value
        for key, value in reviews.items()
        if key != "submissions"
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
        "fault_domain": metadata.get("fault_domain"),
        "issue_codes": issue_codes,
        "issue_taxonomy": issue_taxonomy(issue_codes),
        "last_attempt_present": result.get("last_attempt") is not None,
        "metrics": dict(scores),
    }
