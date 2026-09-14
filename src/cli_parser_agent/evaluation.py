"""Strict local contracts and deterministic scoring for Agent evaluations."""

from __future__ import annotations

import json
import math
import re
import tomllib
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from itertools import combinations
from pathlib import Path, PurePosixPath
from typing import Any, Literal, cast

from cli_parser_agent import GenerationPolicy, GenerationResult
from cli_parser_agent.ttp_generation.validation import (
    parse_ttp_template,
    validate_records_against_schema,
    validate_result_schema,
    validate_ttp_template,
)

TEST_SET_MAX_INPUTS = 5
TEST_SET_TEMPLATE_MAX_BYTES = 64 * 1024
SEMANTIC_PILOT_SUITE = "semantic-pilot"
DATASET_REGISTRY_VERSION = 2
MAX_INPUT_BYTES = 1024 * 1024
SUPPORTED_NODE_TYPES = frozenset(
    {"object", "array", "string", "integer", "number", "boolean"},
)

_ID_RE = re.compile(r"^[a-z][a-z0-9]*(?:[._-][a-z0-9]+)*$")
_TAG_RE = re.compile(r"^[a-z][a-z0-9]*(?:[-_][a-z0-9]+)*$")
_FIELD_RE = re.compile(r"^[a-z][a-z0-9_]*$")

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
EXECUTION_FACT_NAMES = (
    "schema_frozen",
    "entered_ttp",
    "valid_ttp_candidate",
    "finish_called",
    "finish_succeeded",
    "final_acceptance_started",
    "final_acceptance_passed",
)


def project_execution_facts(value: Any) -> dict[str, bool]:
    """Keep observed booleans only; absent facts remain unknown."""

    if not isinstance(value, Mapping):
        return {}
    return {
        name: value[name]
        for name in EXECUTION_FACT_NAMES
        if isinstance(value.get(name), bool)
    }


def _execution_scores(output: Any) -> dict[str, float]:
    facts = project_execution_facts(
        output.get("execution_facts") if isinstance(output, Mapping) else None,
    )
    return {name: float(value) for name, value in facts.items()}


class HarnessError(ValueError):
    """A bounded, safe-to-display evaluation definition error."""


@dataclass(frozen=True, slots=True)
class EvaluationInput:
    path: str
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
    original_input_indices: tuple[int, ...] = ()


@dataclass(frozen=True, slots=True)
class DatasetFileSpec:
    """A file declared by the TOML dataset registry."""

    file: str


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


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise HarnessError(f"JSON contains duplicate object key: {key}")
        result[key] = value
    return result


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


def _load_test_set_text(
    path: Path,
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
    # TTP anchors rows with (?=\n|\r\n) so CRLF still matches, but a greedy
    # capture swallows the trailing CR while goldens hold CR-free values, which
    # scores correct templates as failures. .gitattributes keeps the corpus LF;
    # this is the backstop for inputs that arrive with CR anyway.
    return text.replace("\r\n", "\n")


def _load_test_set_json(
    path: Path,
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


def _dataset_file_spec(value: Any, label: str) -> DatasetFileSpec:
    if not isinstance(value, Mapping):
        raise HarnessError(f"{label} must be an object")
    _require_exact_keys(value, {"file"}, label)
    file_name = _require_string(value["file"], f"{label}.file")
    return DatasetFileSpec(file=file_name)


def _dataset_path_spec(
    dataset_path: Path,
    spec: DatasetFileSpec,
    expected_file: str,
    label: str,
) -> Path:
    path_text, path = _safe_repo_path(dataset_path, spec.file, f"{label}.file")
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
            label,
            max_bytes=MAX_INPUT_BYTES,
        )
        inputs.append(
            EvaluationInput(
                path=f"test_sets/{entry.name}/inputs/{index:03d}.txt",
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
            () if entry.default_input_index is None else (entry.default_input_index,)
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
            _safe_repo_path(
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
        f"dataset {entry.name}.schema.json",
        max_bytes=256 * 1024,
    )
    if not isinstance(schema, dict) or validate_result_schema(schema):
        raise HarnessError(f"dataset {entry.name}.schema.json is not supported")
    template = _load_test_set_text(
        entry.absolute_path / "template.ttp",
        f"dataset {entry.name}.template.ttp",
        max_bytes=TEST_SET_TEMPLATE_MAX_BYTES,
    )
    expected, _ = _load_test_set_json(
        entry.absolute_path / "expected.json",
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
        elif isinstance(item, (list, tuple)):
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
        *EXECUTION_FACT_NAMES,
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
        "model.attempt",
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
    }
    zero_metrics.update(_execution_scores(output))
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
    if isinstance(metadata.get("first_ttp_passed"), bool):
        metrics["first_ttp_passed"] = float(metadata["first_ttp_passed"])
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
    )
    for name in (
        "elapsed_seconds",
        "agent_rounds",
        "ttp_agent_rounds",
        "tool_call_starts",
        "tool_result_errors",
        "ttp_submissions",
        "ttp_test_calls",
        "ttp_test_calls_refused",
        "ttp_no_tool_responses",
        "ttp_no_tool_retries",
        "model_retries_observed",
        "model_attempts_observed",
        "stream_first_delta_seconds",
        "stream_model_call_elapsed_seconds",
        "stream_chunk_count",
        "stream_tool_call_delta_count",
        "stream_usage_seen",
        "input_tokens_total",
        "output_tokens_total",
        "input_tokens_last",
        "model_calls_observed",
        "ttp_history_compaction_events",
        "ttp_history_compacted_interactions",
        "ttp_history_compacted_input_chars",
        "ttp_history_compacted_result_chars",
        "ttp_history_compaction_skips",
    ):
        value = metadata.get(name)
        if (
            isinstance(value, int | float)
            and not isinstance(value, bool)
            and math.isfinite(value)
        ):
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


def _span_timestamp(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int | float):
        return float(value) if math.isfinite(value) else None
    if isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value)
        except ValueError:
            return None
        if parsed.tzinfo is not None:
            return parsed.timestamp()
    return None


def project_candidate_trajectory(
    spans: Sequence[Mapping[str, Any]],
    *,
    expected_records: Sequence[Any] | None = None,
) -> dict[str, Any]:
    """Summarize all candidate submissions in a Trace without raw content."""

    candidates: list[dict[str, Any]] = []
    schema_candidates: list[dict[str, Any]] = []
    finish_results: list[bool | None] = []
    successful_finish_starts: list[float | None] = []
    accepted_submission_ends: dict[int, float | None] = {}
    for ordinal, span in enumerate(spans):
        if not isinstance(span, Mapping):
            continue
        name = str(span.get("name", ""))
        payload = _candidate_payload(span)
        if name in {"finish_generation", "generation.finish_generation"}:
            succeeded = payload.get("generation_finished")
            accepted = payload.get("accepted")
            if accepted is False or succeeded is False:
                succeeded = False
            elif accepted is not True or succeeded is not True:
                succeeded = None
            finish_results.append(succeeded)
            if succeeded:
                successful_finish_starts.append(_span_timestamp(span.get("start_time")))
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
        if quality["accepted"]:
            accepted_submission_ends[quality["submission_index"]] = _span_timestamp(
                span.get("end_time"),
            )
    candidates.sort(key=lambda item: int(item["submission_index"]))
    schema_candidates.sort(key=lambda item: int(item["submission_index"]))
    accepted_indices = [
        int(item["submission_index"]) for item in candidates if item["accepted"]
    ]
    first_accepted = accepted_indices[0] if accepted_indices else None
    finish_succeeded = (
        True
        if any(value is True for value in finish_results)
        else False
        if finish_results and all(value is False for value in finish_results)
        else None
    )
    finish_after_first_accepted: bool | None = None
    first_accepted_end = accepted_submission_ends.get(first_accepted)
    if (
        first_accepted_end is not None
        and successful_finish_starts
        and all(value is not None for value in successful_finish_starts)
    ):
        finish_after_first_accepted = first_accepted_end < min(
            value for value in successful_finish_starts if value is not None
        )
    elif finish_succeeded is False:
        finish_after_first_accepted = False
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
        "finish_called": True if finish_results else None,
        "finish_succeeded": finish_succeeded,
        "finish_after_first_accepted": finish_after_first_accepted,
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
    }
    zero.update(_execution_scores(output))
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
    if isinstance(metadata.get("first_ttp_passed"), bool):
        scores["first_ttp_passed"] = float(metadata["first_ttp_passed"])
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
        "model_attempts_observed",
        "stream_first_delta_seconds",
        "stream_model_call_elapsed_seconds",
        "stream_chunk_count",
        "stream_tool_call_delta_count",
        "stream_usage_seen",
    ):
        value = metadata.get(name)
        if (
            isinstance(value, int | float)
            and not isinstance(value, bool)
            and math.isfinite(value)
        ):
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
            "exception_type": (
                exception_type
                if isinstance(exception_type, str)
                and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,127}", exception_type)
                else "unknown"
            ),
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
            if isinstance(issue, Mapping)
            and isinstance(issue.get("code"), str)
            and len(issue["code"]) <= 128
            and _SAFE_ISSUE_CODE_RE.fullmatch(issue["code"]) is not None
        ]
        if isinstance(raw_issues, list)
        else []
    )
    acceptance = output.get("independent_acceptance")
    acceptance_codes = (
        acceptance.get("issue_codes", []) if isinstance(acceptance, Mapping) else []
    )
    for code in acceptance_codes:
        if (
            isinstance(code, str)
            and len(code) <= 128
            and _SAFE_ISSUE_CODE_RE.fullmatch(code) is not None
            and code not in issue_codes
        ):
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


def schema_proposal_metrics(
    schema: Mapping[str, Any], reference: Mapping[str, Any]
) -> dict[str, Any]:
    """Return numeric description and reference differences, never schema content."""
    actual = schema_signature(schema)
    expected = schema_signature(reference)
    properties = [
        node for path, node in actual.items() if path != "/" and not path.endswith("/*")
    ]
    leaves = [node for node in actual.values() if node.type not in {"object", "array"}]
    described = 0

    def visit(node):
        nonlocal described
        for child in node.get("properties", {}).values():
            described += int(
                isinstance(child.get("description"), str)
                and bool(child["description"].strip())
            )
            visit(child)
        if "items" in node:
            visit(node["items"])

    visit(schema)
    common = actual.keys() & expected.keys()
    return {
        "property_count": len(properties),
        "leaf_count": len(leaves),
        "max_depth": max(
            ((0 if path == "/" else path.count("/")) for path in actual), default=0
        ),
        "type_counts": dict(Counter(node.type for node in actual.values())),
        "required_count": sum(node.required for node in properties),
        "description_count": described,
        "description_coverage": described / len(properties) if properties else None,
        "reference_path_missing_count": len(expected.keys() - actual.keys()),
        "reference_path_added_count": len(actual.keys() - expected.keys()),
        "reference_type_difference_count": sum(
            actual[p].type != expected[p].type for p in common
        ),
        "reference_required_difference_count": sum(
            actual[p].required != expected[p].required for p in common
        ),
    }


def schema_repeat_consistency(schemas: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Compare successful proposals in memory; return no paths or signatures."""
    signatures = [
        tuple(sorted((p, n.type, n.required) for p, n in schema_signature(s).items()))
        for s in schemas
    ]
    counts = Counter(signatures)
    pairs = len(signatures) * (len(signatures) - 1) // 2
    equal_pairs = sum(n * (n - 1) // 2 for n in counts.values())
    return {
        "valid_proposals": len(signatures),
        "structure_variants": len(counts),
        "dominant_structure_share": max(counts.values()) / len(signatures)
        if signatures
        else None,
        "pair_count": pairs,
        "equal_pair_count": equal_pairs,
        "pairwise_consistency": equal_pairs / pairs if pairs else None,
    }


SCHEMA_METRICS_VERSION = 2
_SCHEMA_CONSTRAINTS = frozenset(
    {
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
    }
)


def _schema_value_key(value):
    """In-memory JSON equality key, preserving the boolean/number distinction."""
    if isinstance(value, bool):
        return ("boolean", value)
    if isinstance(value, (int, float)):
        return ("number", value)
    if value is None:
        return ("null",)
    if isinstance(value, str):
        return ("string", value)
    if isinstance(value, list):
        return ("array", tuple(_schema_value_key(v) for v in value))
    return (
        "object",
        tuple(sorted((k, _schema_value_key(v)) for k, v in value.items())),
    )


def _schema_node_views(schema):
    views = {}

    def visit(node, path):
        constraints = tuple(
            sorted(
                (
                    k,
                    frozenset(_schema_value_key(v) for v in value)
                    if k == "enum"
                    else _schema_value_key(value),
                )
                for k, value in node.items()
                if k in _SCHEMA_CONSTRAINTS
            )
        )
        annotations = tuple((k, node.get(k)) for k in ("title", "description"))
        views[path] = (constraints, annotations)
        for name, child in node.get("properties", {}).items():
            visit(child, f"/{name}" if path == "/" else f"{path}/{name}")
        if "items" in node:
            visit(node["items"], f"{path}/*")

    visit(schema, "/")
    return views


def schema_pair_metrics(left, right):
    """Compare in memory; output only counts and decisions, never node contents."""
    a, b = schema_signature(left), schema_signature(right)
    av, bv = _schema_node_views(left), _schema_node_views(right)
    common = a.keys() & b.keys()
    ap, bp = set(a) - {"/"}, set(b) - {"/"}
    properties = {p for p in common if p != "/" and not p.endswith("/*")}
    same_types = {p for p in common if a[p].type == b[p].type}
    type_changed = {p for p in common if a[p].type != b[p].type}
    container_changed = {
        p
        for p in type_changed
        if a[p].type in {"object", "array"} or b[p].type in {"object", "array"}
    }
    required_changed = sum(a[p].required != b[p].required for p in properties)
    constraints_changed = sum(av[p][0] != bv[p][0] for p in same_types)
    structure_equal = a == b
    return {
        "structure_equal": structure_equal,
        "contract_equal": structure_equal and constraints_changed == 0,
        "path_set_equal": ap == bp,
        "path_intersection_count": len(ap & bp),
        "path_union_count": len(ap | bp),
        "path_difference_count": len(ap ^ bp),
        "path_overlap": len(ap & bp) / len(ap | bp) if ap | bp else 1.0,
        "type_comparable_nodes": len(common),
        "container_type_difference_count": len(container_changed),
        "scalar_type_difference_count": len(type_changed - container_changed),
        "required_comparable_nodes": len(properties),
        "required_difference_count": required_changed,
        "constraint_comparable_nodes": len(same_types),
        "constraint_difference_count": constraints_changed,
        "annotation_comparable_nodes": len(common),
        "annotation_difference_count": sum(av[p][1] != bv[p][1] for p in common),
    }


def schema_contract_consistency(proposals):
    """Compare trial/schema pairs in stable order without serializing schemas."""
    proposals = sorted(proposals, key=lambda item: item[0])
    pairs = [
        {"left_trial_id": aid, "right_trial_id": bid, **schema_pair_metrics(a, b)}
        for (aid, a), (bid, b) in combinations(proposals, 2)
    ]
    signatures = []
    for _, schema in proposals:
        nodes = schema_signature(schema)
        views = _schema_node_views(schema)
        signatures.append(
            tuple(
                sorted((p, n.type, n.required, views[p][0]) for p, n in nodes.items())
            )
        )
    counts = Counter(signatures)
    equal = sum(p["contract_equal"] for p in pairs)
    difference_names = (
        "path",
        "container_type",
        "scalar_type",
        "required",
        "constraint",
        "annotation",
    )
    return {
        "schema_metrics_version": SCHEMA_METRICS_VERSION,
        "valid_trial_ids": [trial_id for trial_id, _ in proposals],
        "contract_variants": len(counts),
        "dominant_contract_share": max(counts.values()) / len(proposals)
        if proposals
        else None,
        "pair_count": len(pairs),
        "contract_equal_pair_count": equal,
        "pairwise_contract_consistency": equal / len(pairs) if pairs else None,
        "mean_path_overlap": sum(p["path_overlap"] for p in pairs) / len(pairs)
        if pairs
        else None,
        "differences": {
            name: {
                "affected_pairs": sum(p[name + "_difference_count"] > 0 for p in pairs),
                "node_count": sum(p[name + "_difference_count"] for p in pairs),
                "comparable_nodes": sum(
                    p[
                        "path_union_count"
                        if name == "path"
                        else "type_comparable_nodes"
                        if name in {"container_type", "scalar_type"}
                        else name + "_comparable_nodes"
                    ]
                    for p in pairs
                ),
            }
            for name in difference_names
        },
        "pairs": pairs,
    }


def schema_consistency_overview(cases):
    eligible = [c for c in cases.values() if c["pair_count"]]
    pairs = sum(c["pair_count"] for c in eligible)
    return {
        "comparable_cases": len(eligible),
        "pair_count": pairs,
        "macro_contract_consistency": sum(
            c["pairwise_contract_consistency"] for c in eligible
        )
        / len(eligible)
        if eligible
        else None,
        "weighted_contract_consistency": sum(
            c["contract_equal_pair_count"] for c in eligible
        )
        / pairs
        if pairs
        else None,
        "macro_path_overlap": sum(c["mean_path_overlap"] for c in eligible)
        / len(eligible)
        if eligible
        else None,
        "weighted_path_overlap": sum(
            c["mean_path_overlap"] * c["pair_count"] for c in eligible
        )
        / pairs
        if pairs
        else None,
        "differences": {
            name: {
                "affected_pairs": sum(
                    c["differences"][name]["affected_pairs"] for c in eligible
                ),
                "node_count": sum(
                    c["differences"][name]["node_count"] for c in eligible
                ),
                "comparable_nodes": sum(
                    c["differences"][name]["comparable_nodes"] for c in eligible
                ),
                "macro_affected_pair_rate": sum(
                    c["differences"][name]["affected_pairs"] / c["pair_count"]
                    for c in eligible
                )
                / len(eligible)
                if eligible
                else None,
                "weighted_affected_pair_rate": sum(
                    c["differences"][name]["affected_pairs"] for c in eligible
                )
                / pairs
                if pairs
                else None,
            }
            for name in (
                "path",
                "container_type",
                "scalar_type",
                "required",
                "constraint",
                "annotation",
            )
        },
    }


_SCHEMA_REVIEW_DIMENSIONS = frozenset(
    {
        "naming",
        "coverage",
        "decomposition",
        "structure",
        "types_required",
        "value_boundaries",
        "overconstraint",
    }
)
_SCHEMA_REVIEW_CATEGORIES = frozenset(
    {
        "synonym_naming",
        "structure_placement",
        "type",
        "required",
        "constraint",
        "split_merge",
        "coverage",
        "annotation_wording",
        "empty_slot",
        "placeholder",
        "unit",
        "value_boundary",
        "field_meaning",
        "unresolved_mapping",
    }
)


def summarize_schema_review(summary, review):
    """Validate a bounded human review and compute conservative joint rates."""
    from uuid import UUID

    def require(condition):
        if not condition:
            raise HarnessError("invalid schema review structure or reference")

    def keys(node, expected):
        require(isinstance(node, dict) and set(node) == set(expected))

    def valid_uuid(value):
        if value is None:
            return
        try:
            require(isinstance(value, str) and str(UUID(value)) == value)
        except (ValueError, AttributeError):
            raise HarnessError("invalid schema review identifier") from None

    def common(row):
        require(isinstance(row["categories"], list))
        require(
            all(
                isinstance(x, str) and x in _SCHEMA_REVIEW_CATEGORIES
                for x in row["categories"]
            )
        )
        require(len(row["categories"]) == len(set(row["categories"])))
        paths = row["paths"]
        require(isinstance(paths, list) and len(paths) <= 24)
        require(
            all(
                isinstance(p, str)
                and len(p) <= 2048
                and all(len(segment) <= 120 for segment in p.split("/"))
                and re.fullmatch(
                    r"/(?:[a-z][a-z0-9]*(?:_[a-z0-9]+)*|\*)(?:/(?:[a-z][a-z0-9]*(?:_[a-z0-9]+)*|\*))*|/",
                    p,
                )
                for p in paths
            )
        )
        require(len(paths) == len(set(paths)))

    keys(review, {"review_version", "run_id", "trials", "pairs"})
    require(type(review["review_version"]) is int and review["review_version"] == 1)
    require(isinstance(summary, dict) and summary.get("mode") == "schema-only")
    run_id = summary.get("run_id")
    if run_id is None and summary.get("trials"):
        run_id = summary["trials"][0]["trial_id"].split("/")[0]
    require(isinstance(run_id, str) and review["run_id"] == run_id)
    require(isinstance(review["trials"], list) and isinstance(review["pairs"], list))
    trials = {t["trial_id"]: t for t in summary["trials"]}
    require(len(trials) == len(summary["trials"]))
    require(len(review["trials"]) <= len(trials))
    groups = {}
    for tid, trial in trials.items():
        require(tid.startswith(run_id + "/"))
        groups.setdefault(trial["case_id"], []).append(tid)
    planned_per_case = summary.get("planned_trials_per_case")
    if planned_per_case is not None:
        require(type(planned_per_case) is int and 1 <= planned_per_case <= 10)
        require(all(len(ids) <= planned_per_case for ids in groups.values()))
    expected_counts = {
        case: planned_per_case or len(ids) for case, ids in groups.items()
    }
    planned_trials = sum(expected_counts.values())
    planned_pairs = sum(n * (n - 1) // 2 for n in expected_counts.values())
    require(len(review["pairs"]) <= planned_pairs)
    trial_reviews = {}
    for row in review["trials"]:
        keys(
            row,
            {
                "trial_id",
                "case_id",
                "trace_id",
                "span_ids",
                "dimensions",
                "overall",
                "categories",
                "paths",
            },
        )
        tid = row["trial_id"]
        require(isinstance(tid, str) and tid in trials and tid not in trial_reviews)
        t = trials[tid]
        require(row["case_id"] == t["case_id"] and row["trace_id"] == t.get("trace_id"))
        valid_uuid(row["trace_id"])
        require(isinstance(row["span_ids"], list) and len(row["span_ids"]) <= 64)
        for span in row["span_ids"]:
            require(isinstance(span, str))
            valid_uuid(span)
        require(len(set(row["span_ids"])) == len(row["span_ids"]))
        keys(row["dimensions"], _SCHEMA_REVIEW_DIMENSIONS)
        require(
            all(
                isinstance(v, str)
                and v in {"passed", "issue", "insufficient_evidence", "not_applicable"}
                for v in row["dimensions"].values()
            )
        )
        require(
            isinstance(row["overall"], str)
            and row["overall"] in {"acceptable", "needs_revision", "unjudgeable"}
        )
        common(row)
        if row["overall"] == "acceptable":
            require(
                t["generation_success"] is True and t["proposal_revalidated"] is True
            )
            require(
                row["trace_id"] is not None
                and "issue" not in row["dimensions"].values()
                and "insufficient_evidence" not in row["dimensions"].values()
            )
        if row["overall"] == "needs_revision":
            require("issue" in row["dimensions"].values())
        trial_reviews[tid] = row

    automatic = {}
    for case, metrics in summary.get("contract_consistency", {}).items():
        for pair in metrics["pairs"]:
            ids = tuple(sorted((pair["left_trial_id"], pair["right_trial_id"])))
            require(
                ids[0] != ids[1]
                and ids not in automatic
                and all(t in trials and trials[t]["case_id"] == case for t in ids)
            )
            automatic[ids] = pair
    pair_reviews = {}
    for row in review["pairs"]:
        keys(
            row,
            {
                "case_id",
                "left_trial_id",
                "right_trial_id",
                "annotation_semantics",
                "judgment",
                "categories",
                "paths",
            },
        )
        require(
            isinstance(row["left_trial_id"], str)
            and isinstance(row["right_trial_id"], str)
        )
        ids = tuple(sorted((row["left_trial_id"], row["right_trial_id"])))
        require(ids[0] != ids[1] and ids not in pair_reviews)
        if summary.get("schema_metrics_version") == SCHEMA_METRICS_VERSION:
            require(ids in automatic)
        require(
            all(
                t in trials
                and trials[t]["case_id"] == row["case_id"]
                and trials[t]["generation_success"] is True
                and trials[t]["proposal_revalidated"] is True
                for t in ids
            )
        )
        require(
            isinstance(row["annotation_semantics"], str)
            and row["annotation_semantics"] in {"equivalent", "different", "unknown"}
        )
        require(
            isinstance(row["judgment"], str)
            and row["judgment"]
            in {"both_reasonable", "at_least_one_issue", "insufficient_evidence"}
        )
        common(row)
        for tid in ids:
            known = trial_reviews.get(tid)
            if known and row["judgment"] == "both_reasonable":
                require(known["overall"] == "acceptable")
        if all(t in trial_reviews for t in ids):
            labels = [trial_reviews[t]["overall"] for t in ids]
            if row["judgment"] == "at_least_one_issue":
                require("needs_revision" in labels)
        pair_reviews[ids] = row

    acceptable = {t for t, r in trial_reviews.items() if r["overall"] == "acceptable"}
    reasonable_pairs = {
        tuple(sorted(pair))
        for ids in groups.values()
        for pair in combinations(ids, 2)
        if all(t in acceptable for t in pair)
    }
    confirmed = {
        ids
        for ids in reasonable_pairs
        if ids in automatic
        and automatic[ids]["contract_equal"] is True
        and ids in pair_reviews
        and pair_reviews[ids]["annotation_semantics"] == "equivalent"
        and pair_reviews[ids]["judgment"] == "both_reasonable"
    }
    valid_pairs = {
        tuple(sorted(pair))
        for ids in groups.values()
        for pair in combinations(ids, 2)
        if all(
            trials[t]["generation_success"] is True
            and trials[t]["proposal_revalidated"] is True
            for t in pair
        )
    }
    metrics_available = summary.get("schema_metrics_version") == SCHEMA_METRICS_VERSION
    complete_cases = {
        case: (
            len(ids) == expected_counts[case]
            and len(ids) >= 2
            and all(t in acceptable for t in ids)
            and all(tuple(sorted(pair)) in confirmed for pair in combinations(ids, 2))
        )
        if metrics_available
        else None
        for case, ids in groups.items()
    }
    counts = Counter(r["overall"] for r in trial_reviews.values())
    return {
        "review_version": 1,
        "schema_metrics_version": summary.get("schema_metrics_version"),
        "run_id": run_id,
        "parseability": "not_tested",
        "planned_trials": planned_trials,
        "planned_pairs": planned_pairs,
        "generation_failed_trials": sum(
            not t["generation_success"] for t in trials.values()
        ),
        "revalidation_failed_trials": sum(
            t["proposal_revalidated"] is False for t in trials.values()
        ),
        "reviewed_trials": len(trial_reviews),
        "missing_trial_reviews": planned_trials - len(trial_reviews),
        "acceptable_trials": counts["acceptable"],
        "needs_revision_trials": counts["needs_revision"],
        "unjudgeable_trials": counts["unjudgeable"],
        "reasonable_proposal_rate": counts["acceptable"] / planned_trials
        if planned_trials
        else None,
        "valid_pairs": len(valid_pairs),
        "reviewed_pairs": len(pair_reviews),
        "missing_pair_reviews": len(valid_pairs - pair_reviews.keys()),
        "unknown_annotation_pairs": sum(
            r["annotation_semantics"] == "unknown" for r in pair_reviews.values()
        ),
        "reasonable_pairs": len(reasonable_pairs),
        "confirmed_reasonable_consistent_pairs": len(confirmed)
        if metrics_available
        else None,
        "reasonable_pair_consistency": len(confirmed) / len(reasonable_pairs)
        if metrics_available and reasonable_pairs
        else None,
        "planned_pair_confirmed_rate": len(confirmed) / planned_pairs
        if metrics_available and planned_pairs
        else None,
        "all_repeats_reasonable_consistent": complete_cases,
        "review_complete": len(trial_reviews) == planned_trials
        and valid_pairs <= pair_reviews.keys(),
        "trials": list(trial_reviews.values()),
        "pairs": list(pair_reviews.values()),
    }
