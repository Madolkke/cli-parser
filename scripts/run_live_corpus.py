"""Run the real command-output corpus against a configured live model.

The ``list`` and ``preflight`` commands are deliberately local-only. Model-facing
imports and environment reads happen exclusively in the ``run`` command.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import re
import sys
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any

MANIFEST_VERSION = 1
RUNNER_VERSION = 1
EXPECTED_CASE_COUNT = 13
EXPECTED_SAMPLE_COUNT = 40
EXPECTED_SMOKE_CASE_COUNT = 5
EXPECTED_SMOKE_SAMPLE_COUNT = 12
MAX_SAMPLE_BYTES = 1024 * 1024
CORPUS_ROOT = Path(__file__).resolve().parents[1] / "testdata" / "real_command_outputs"
MANIFEST_PATH = CORPUS_ROOT / "corpus.json"
DEFAULT_ARTIFACT_ROOT = (
    Path(__file__).resolve().parents[1] / ".artifacts" / ("live-corpus")
)

_ID_RE = re.compile(r"^[a-z][a-z0-9]*(?:[._-][a-z0-9]+)*$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_ANSI_RE = re.compile(r"\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07]*(?:\x07|\x1b\\))")
_PAGER_RE = re.compile(
    r"(?:--+\s*(?:more|\(\s*more\s*\))\s*--+|\bpress\s+(?:any\s+key|"
    r"enter|space)\s+to\s+continue\b)",
    re.IGNORECASE,
)
_HUAWEI_PROMPT = (
    r"(?:<[A-Za-z0-9_.:/@~-]{1,160}>|"
    r"\[[~*]?[A-Za-z0-9_.:/@~-]{1,160}\])"
)
_PROMPT_PREFIX = (
    rf"(?:{_HUAWEI_PROMPT}|(?:\[[^\]\r\n]{{1,160}}\]|"
    r"[A-Za-z0-9_.:/@~-]+(?:\([^\r\n)]{1,80}\))?)\s*[#>$])"
)
_PROMPT_ONLY_RE = re.compile(rf"^{_PROMPT_PREFIX}\s*$")
_CREDENTIAL_PATTERNS = (
    re.compile(r"-----BEGIN (?:OPENSSH |RSA |EC |DSA )?PRIVATE KEY-----"),
    re.compile(r"\bAuthorization\s*:\s*(?:Basic|Bearer)\s+\S+", re.IGNORECASE),
    re.compile(
        r"\b(?:api[_ -]?key|access[_ -]?token|auth[_ -]?token)\s*[:=]\s*\S+",
        re.IGNORECASE,
    ),
    re.compile(r"\b(?:password|passwd|pre-shared-key)\s*[:=]\s*\S+", re.IGNORECASE),
    re.compile(
        r"^\s*(?:password|passwd)\s+(?:\d+\s+)?\S+\s*$",
        re.IGNORECASE | re.MULTILINE,
    ),
    re.compile(
        r"^\s*username\s+\S+(?:\s+\S+){0,8}\s+"
        r"(?:password|secret)\s+(?:\d+\s+)?\S+\s*$",
        re.IGNORECASE | re.MULTILINE,
    ),
    re.compile(
        r"^\s*set\s+(?:password|passwd|private-key|psksecret|secret)\s+"
        r"(?:ENC\s+)?\S+\s*$",
        re.IGNORECASE | re.MULTILINE,
    ),
    re.compile(
        r"^\s*local-user\s+\S+\s+password\s+"
        r"(?:irreversible-cipher|cipher|simple)\s+\S+\s*$",
        re.IGNORECASE | re.MULTILINE,
    ),
    re.compile(
        r"^\s*neighbor\s+\S+\s+password\s+"
        r"(?:(?:\d+|cipher|simple)\s+)?\S+\s*$",
        re.IGNORECASE | re.MULTILINE,
    ),
    re.compile(
        r"^\s*(?:encrypted-password|snmp-agent\s+community\s+\S+)\s+\S+",
        re.IGNORECASE | re.MULTILINE,
    ),
    re.compile(r"^\s*(?:enable\s+)?secret\s+\S+", re.IGNORECASE | re.MULTILINE),
    re.compile(r"^\s*snmp-server\s+community\s+\S+", re.IGNORECASE | re.MULTILINE),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"\bhttps?://[^\s/:]+:[^\s/@]+@", re.IGNORECASE),
)


class CorpusError(ValueError):
    """A safe, user-facing corpus or selection error."""


@dataclass(frozen=True, slots=True)
class CorpusSample:
    path: str
    sha256: str
    absolute_path: Path


@dataclass(frozen=True, slots=True)
class CorpusCase:
    id: str
    source: str
    platform: str
    command: str
    shapes: tuple[str, ...]
    suites: tuple[str, ...]
    samples: tuple[CorpusSample, ...]


@dataclass(frozen=True, slots=True)
class CorpusManifest:
    version: int
    sha256: str
    cases: tuple[CorpusCase, ...]


def _utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise CorpusError(f"manifest contains duplicate object key: {key}")
        result[key] = value
    return result


def _read_manifest_json(path: Path) -> tuple[dict[str, Any], bytes]:
    try:
        payload = path.read_bytes()
    except OSError as error:
        raise CorpusError("corpus manifest could not be read") from error
    if payload.startswith(b"\xef\xbb\xbf"):
        raise CorpusError("corpus manifest must not contain a UTF-8 BOM")
    try:
        text = payload.decode("utf-8", errors="strict")
        value = json.loads(text, object_pairs_hook=_reject_duplicate_keys)
    except CorpusError:
        raise
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise CorpusError("corpus manifest is not valid UTF-8 JSON") from error
    if not isinstance(value, dict):
        raise CorpusError("corpus manifest root must be an object")
    return value, payload


def _require_exact_keys(
    value: dict[str, Any],
    expected: set[str],
    location: str,
) -> None:
    missing = expected - value.keys()
    extra = value.keys() - expected
    if missing:
        raise CorpusError(f"{location} is missing keys: {', '.join(sorted(missing))}")
    if extra:
        names = ", ".join(sorted(extra))
        raise CorpusError(f"{location} has unsupported keys: {names}")


def _require_string(value: Any, location: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise CorpusError(f"{location} must be a non-empty string")
    return value


def _require_string_list(value: Any, location: str) -> tuple[str, ...]:
    if (
        not isinstance(value, list)
        or not value
        or any(not isinstance(item, str) or not item.strip() for item in value)
    ):
        raise CorpusError(f"{location} must be a non-empty array of strings")
    items = tuple(value)
    if len({item.casefold() for item in items}) != len(items):
        raise CorpusError(f"{location} must not contain duplicate values")
    return items


def _safe_relative_path(value: Any, location: str) -> tuple[str, PurePosixPath]:
    text = _require_string(value, location)
    if "\\" in text or ":" in text:
        raise CorpusError(f"{location} must use a relative POSIX path")
    relative = PurePosixPath(text)
    if (
        relative.is_absolute()
        or not relative.parts
        or any(part in {"", ".", ".."} for part in relative.parts)
    ):
        raise CorpusError(f"{location} must be a traversal-free relative path")
    return text, relative


def _resolve_corpus_path(relative: PurePosixPath, location: str) -> Path:
    root = CORPUS_ROOT.resolve()
    candidate = root.joinpath(*relative.parts).resolve()
    if not candidate.is_relative_to(root):
        raise CorpusError(f"{location} resolves outside the corpus directory")
    return candidate


def _path_is_below(path: PurePosixPath, prefix: PurePosixPath) -> bool:
    path_parts = tuple(part.casefold() for part in path.parts)
    prefix_parts = tuple(part.casefold() for part in prefix.parts)
    return len(path_parts) > len(prefix_parts) and (
        path_parts[: len(prefix_parts)] == prefix_parts
    )


def _validate_license_file(path_text: Any, location: str) -> str:
    text, relative = _safe_relative_path(path_text, location)
    path = _resolve_corpus_path(relative, location)
    if not path.is_file():
        raise CorpusError(f"{location} does not identify a license file")
    try:
        payload = path.read_bytes()
        decoded = payload.decode("utf-8", errors="strict")
    except (OSError, UnicodeDecodeError) as error:
        raise CorpusError(f"{location} must be a readable UTF-8 file") from error
    if payload.startswith(b"\xef\xbb\xbf") or not decoded.strip():
        raise CorpusError(f"{location} must be non-empty UTF-8 without a BOM")
    return text


def _validate_sources(value: Any) -> dict[str, PurePosixPath]:
    if not isinstance(value, dict) or not value:
        raise CorpusError("manifest.sources must be a non-empty object")
    prefixes: dict[str, PurePosixPath] = {}
    license_paths: set[str] = set()
    for source_name, raw_source in value.items():
        location = f"manifest.sources.{source_name}"
        if not isinstance(source_name, str) or not _ID_RE.fullmatch(source_name):
            raise CorpusError("manifest source names must be lowercase identifiers")
        if not isinstance(raw_source, dict):
            raise CorpusError(f"{location} must be an object")
        _require_exact_keys(
            raw_source,
            {
                "repository",
                "tag",
                "commit",
                "license",
                "license_file",
                "path_prefix",
            },
            location,
        )
        repository = _require_string(raw_source["repository"], f"{location}.repository")
        if not repository.startswith("https://"):
            raise CorpusError(f"{location}.repository must be an HTTPS URL")
        _require_string(raw_source["tag"], f"{location}.tag")
        commit = _require_string(raw_source["commit"], f"{location}.commit")
        if not re.fullmatch(r"[0-9a-f]{40}", commit):
            raise CorpusError(f"{location}.commit must be a lowercase Git commit SHA")
        _require_string(raw_source["license"], f"{location}.license")
        license_path = _validate_license_file(
            raw_source["license_file"],
            f"{location}.license_file",
        )
        folded_license_path = license_path.casefold()
        if folded_license_path in license_paths:
            raise CorpusError("each source must identify a distinct license file")
        license_paths.add(folded_license_path)
        _, prefix = _safe_relative_path(
            raw_source["path_prefix"],
            f"{location}.path_prefix",
        )
        prefix_path = _resolve_corpus_path(prefix, f"{location}.path_prefix")
        if not prefix_path.is_dir():
            raise CorpusError(f"{location}.path_prefix does not identify a directory")
        folded_prefix = prefix.as_posix().casefold()
        if any(
            folded_prefix == existing.as_posix().casefold()
            for existing in prefixes.values()
        ):
            raise CorpusError("source path prefixes must be unique")
        prefixes[source_name] = prefix
    return prefixes


def _sample_text_issues(text: str, command: str) -> list[str]:
    issues: list[str] = []
    if not text.strip():
        issues.append("is empty or whitespace-only")
    if "\r" in text:
        issues.append("contains CR characters instead of LF-only newlines")
    if "\x00" in text:
        issues.append("contains NUL characters")
    if any(ord(character) < 32 and character not in {"\t", "\n"} for character in text):
        issues.append("contains disallowed C0 control characters")
    if _ANSI_RE.search(text) or "\x1b" in text:
        issues.append("contains ANSI terminal escapes")
    if _PAGER_RE.search(text):
        issues.append("contains a terminal pager marker")

    command_text = " ".join(command.split())
    command_re = re.compile(
        rf"^{_PROMPT_PREFIX}\s*{re.escape(command_text)}\s*$",
        re.IGNORECASE,
    )
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        normalised = " ".join(stripped.split())
        if normalised.casefold() == command_text.casefold() or command_re.fullmatch(
            normalised,
        ):
            issues.append("contains the command text or a command echo")
            break
        if _PROMPT_ONLY_RE.fullmatch(stripped):
            issues.append("contains a device or shell prompt")
            break
    if any(pattern.search(text) for pattern in _CREDENTIAL_PATTERNS):
        issues.append("matches a high-risk credential pattern")
    return issues


def _validate_sample(
    raw_sample: Any,
    *,
    case_location: str,
    source_prefix: PurePosixPath,
) -> CorpusSample:
    if not isinstance(raw_sample, dict):
        raise CorpusError(f"{case_location} must be an object")
    _require_exact_keys(raw_sample, {"path", "sha256"}, case_location)
    path_text, relative = _safe_relative_path(
        raw_sample["path"],
        f"{case_location}.path",
    )
    if relative.suffix.casefold() not in {".raw", ".txt"}:
        raise CorpusError(f"{case_location}.path must identify a text output file")
    if not _path_is_below(relative, source_prefix):
        raise CorpusError(f"{case_location}.path is outside its source path prefix")
    sha256 = _require_string(raw_sample["sha256"], f"{case_location}.sha256")
    if not _SHA256_RE.fullmatch(sha256):
        raise CorpusError(f"{case_location}.sha256 must be a lowercase SHA-256 digest")
    absolute_path = _resolve_corpus_path(relative, f"{case_location}.path")
    if not absolute_path.is_file():
        raise CorpusError(f"{case_location}.path does not identify a regular file")
    return CorpusSample(path=path_text, sha256=sha256, absolute_path=absolute_path)


def _validate_sample_file(sample: CorpusSample, command: str, location: str) -> None:
    try:
        payload = sample.absolute_path.read_bytes()
    except OSError as error:
        raise CorpusError(f"{location} could not be read") from error
    if len(payload) > MAX_SAMPLE_BYTES:
        raise CorpusError(f"{location} exceeds {MAX_SAMPLE_BYTES} bytes")
    if payload.startswith(b"\xef\xbb\xbf"):
        raise CorpusError(f"{location} contains a UTF-8 BOM")
    try:
        text = payload.decode("utf-8", errors="strict")
    except UnicodeDecodeError as error:
        raise CorpusError(f"{location} is not strict UTF-8") from error
    digest = hashlib.sha256(payload).hexdigest()
    if digest != sample.sha256:
        raise CorpusError(f"{location} SHA-256 does not match the manifest")
    issues = _sample_text_issues(text, command)
    if issues:
        raise CorpusError(f"{location} {issues[0]}")


def load_and_preflight_corpus() -> CorpusManifest:
    """Load the manifest and validate all corpus metadata and files."""

    raw, manifest_bytes = _read_manifest_json(MANIFEST_PATH)
    _require_exact_keys(
        raw,
        {
            "version",
            "expected_case_count",
            "expected_sample_count",
            "sources",
            "cases",
        },
        "manifest",
    )
    if type(raw["version"]) is not int or raw["version"] != MANIFEST_VERSION:
        raise CorpusError(f"manifest.version must be {MANIFEST_VERSION}")
    for key, expected in (
        ("expected_case_count", EXPECTED_CASE_COUNT),
        ("expected_sample_count", EXPECTED_SAMPLE_COUNT),
    ):
        if type(raw[key]) is not int or raw[key] != expected:
            raise CorpusError(f"manifest.{key} must be {expected}")
    prefixes = _validate_sources(raw["sources"])
    raw_cases = raw["cases"]
    if not isinstance(raw_cases, list):
        raise CorpusError("manifest.cases must be an array")
    if len(raw_cases) != EXPECTED_CASE_COUNT:
        raise CorpusError(f"manifest must contain {EXPECTED_CASE_COUNT} cases")

    cases: list[CorpusCase] = []
    case_ids: set[str] = set()
    sample_paths: set[str] = set()
    for case_index, raw_case in enumerate(raw_cases):
        location = f"manifest.cases[{case_index}]"
        if not isinstance(raw_case, dict):
            raise CorpusError(f"{location} must be an object")
        _require_exact_keys(
            raw_case,
            {"id", "source", "platform", "command", "shapes", "suites", "samples"},
            location,
        )
        case_id = _require_string(raw_case["id"], f"{location}.id")
        if not _ID_RE.fullmatch(case_id):
            raise CorpusError(f"{location}.id must be a lowercase dotted identifier")
        folded_case_id = case_id.casefold()
        if folded_case_id in case_ids:
            raise CorpusError("manifest case IDs must be unique")
        case_ids.add(folded_case_id)
        source = _require_string(raw_case["source"], f"{location}.source")
        if source not in prefixes:
            raise CorpusError(f"{location}.source identifies an unknown source")
        platform = _require_string(raw_case["platform"], f"{location}.platform")
        if not _ID_RE.fullmatch(platform):
            raise CorpusError(f"{location}.platform must be a lowercase identifier")
        command = _require_string(raw_case["command"], f"{location}.command")
        if "\r" in command or "\n" in command:
            raise CorpusError(f"{location}.command must be a single line")
        shapes = _require_string_list(raw_case["shapes"], f"{location}.shapes")
        suites = _require_string_list(raw_case["suites"], f"{location}.suites")
        if not set(suites).issubset({"smoke", "all"}) or "all" not in suites:
            raise CorpusError(
                f"{location}.suites must contain 'all' and may also contain 'smoke'",
            )
        raw_samples = raw_case["samples"]
        if not isinstance(raw_samples, list) or not 1 <= len(raw_samples) <= 5:
            raise CorpusError(f"{location}.samples must contain 1 to 5 entries")
        samples: list[CorpusSample] = []
        for sample_index, raw_sample in enumerate(raw_samples):
            sample_location = f"{location}.samples[{sample_index}]"
            sample = _validate_sample(
                raw_sample,
                case_location=sample_location,
                source_prefix=prefixes[source],
            )
            folded_path = sample.path.casefold()
            if folded_path in sample_paths:
                raise CorpusError("manifest sample paths must be unique")
            sample_paths.add(folded_path)
            _validate_sample_file(sample, command, sample_location)
            samples.append(sample)
        cases.append(
            CorpusCase(
                id=case_id,
                source=source,
                platform=platform,
                command=command,
                shapes=shapes,
                suites=suites,
                samples=tuple(samples),
            ),
        )

    if len(sample_paths) != EXPECTED_SAMPLE_COUNT:
        raise CorpusError(f"manifest must contain {EXPECTED_SAMPLE_COUNT} samples")
    discovered_paths = {
        path.relative_to(CORPUS_ROOT).as_posix().casefold()
        for prefix in prefixes.values()
        for path in _resolve_corpus_path(prefix, "manifest source path").rglob("*")
        if path.is_file()
    }
    if discovered_paths != sample_paths:
        raise CorpusError(
            "source directories must contain exactly the files listed in manifest",
        )
    smoke_cases = [case for case in cases if "smoke" in case.suites]
    smoke_samples = sum(len(case.samples) for case in smoke_cases)
    if (
        len(smoke_cases) != EXPECTED_SMOKE_CASE_COUNT
        or smoke_samples != EXPECTED_SMOKE_SAMPLE_COUNT
    ):
        raise CorpusError(
            "smoke suite must contain exactly "
            f"{EXPECTED_SMOKE_CASE_COUNT} cases and "
            f"{EXPECTED_SMOKE_SAMPLE_COUNT} samples",
        )
    return CorpusManifest(
        version=MANIFEST_VERSION,
        sha256=hashlib.sha256(manifest_bytes).hexdigest(),
        cases=tuple(cases),
    )


def _select_cases(
    manifest: CorpusManifest,
    *,
    suite: str,
    requested_ids: list[str] | None,
    source: str | None,
    platform: str | None,
    max_cases: int | None,
) -> list[CorpusCase]:
    if requested_ids:
        known = {case.id for case in manifest.cases}
        unknown = [case_id for case_id in requested_ids if case_id not in known]
        if unknown:
            raise CorpusError(f"unknown case ID: {unknown[0]}")
        if len(set(requested_ids)) != len(requested_ids):
            raise CorpusError("--case must not contain duplicate IDs")
        requested = set(requested_ids)
        selected = [case for case in manifest.cases if case.id in requested]
    elif suite == "all":
        selected = list(manifest.cases)
    else:
        selected = [case for case in manifest.cases if suite in case.suites]
    if source is not None:
        selected = [case for case in selected if case.source == source]
    if platform is not None:
        selected = [case for case in selected if case.platform == platform]
    if max_cases is not None:
        selected = selected[:max_cases]
    if not selected:
        raise CorpusError("case selection is empty")
    return selected


def _positive_int(value: str) -> int:
    try:
        parsed = int(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("must be a positive integer") from error
    if parsed < 1:
        raise argparse.ArgumentTypeError("must be a positive integer")
    return parsed


def _concurrency(value: str) -> int:
    parsed = _positive_int(value)
    if parsed > 4:
        raise argparse.ArgumentTypeError("must be between 1 and 4")
    return parsed


def _add_selection_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--suite", choices=("smoke", "all"), default="smoke")


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Validate and run the real command-output corpus.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    list_parser = subparsers.add_parser("list", help="list corpus cases")
    _add_selection_arguments(list_parser)
    subparsers.add_parser("preflight", help="validate the local corpus")

    run_parser = subparsers.add_parser("run", help="run cases against a live model")
    _add_selection_arguments(run_parser)
    run_parser.add_argument(
        "--case",
        dest="case_ids",
        action="append",
        metavar="ID",
        help="run this case (repeatable; overrides --suite)",
    )
    run_parser.add_argument("--source", help="filter by manifest source name")
    run_parser.add_argument("--platform", help="filter by platform name")
    run_parser.add_argument("--max-cases", type=_positive_int)
    run_parser.add_argument("--concurrency", type=_concurrency, default=1)
    destination = run_parser.add_mutually_exclusive_group()
    destination.add_argument("--output-dir", type=Path)
    destination.add_argument("--resume", type=Path)
    return parser


def _command_list(args: argparse.Namespace) -> int:
    manifest = load_and_preflight_corpus()
    cases = _select_cases(
        manifest,
        suite=args.suite,
        requested_ids=None,
        source=None,
        platform=None,
        max_cases=None,
    )
    print("case_id\tsource\tplatform\tsamples\tsuites\tcommand")
    for case in cases:
        print(
            f"{case.id}\t{case.source}\t{case.platform}\t{len(case.samples)}\t"
            f"{','.join(case.suites)}\t{case.command}",
        )
    print(f"cases={len(cases)} samples={sum(len(case.samples) for case in cases)}")
    return 0


def _command_preflight() -> int:
    manifest = load_and_preflight_corpus()
    print(
        "preflight ok: "
        f"cases={len(manifest.cases)} "
        f"samples={sum(len(case.samples) for case in manifest.cases)}",
    )
    return 0


def _json_dump(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        indent=2,
        sort_keys=True,
        allow_nan=False,
    )
    temporary.write_text(f"{encoded}\n", encoding="utf-8", newline="\n")
    os.replace(temporary, path)


def _allocate_default_output_dir() -> Path:
    DEFAULT_ARTIFACT_ROOT.mkdir(parents=True, exist_ok=True)
    stem = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    for suffix in range(1000):
        name = stem if suffix == 0 else f"{stem}-{suffix:02d}"
        candidate = DEFAULT_ARTIFACT_ROOT / name
        try:
            candidate.mkdir()
        except FileExistsError:
            continue
        return candidate
    raise CorpusError("could not allocate a unique live-corpus output directory")


def _prepare_output_dir(
    output_dir: Path | None,
    resume: Path | None,
) -> tuple[Path, bool]:
    if resume is not None:
        resolved = resume.expanduser().resolve()
        if not resolved.is_dir() or not (resolved / "run.json").is_file():
            raise CorpusError("--resume must identify an existing live-corpus run")
        try:
            run_data = json.loads((resolved / "run.json").read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
            raise CorpusError("resume run metadata is unreadable") from error
        if (
            not isinstance(run_data, dict)
            or run_data.get("runner_version") != RUNNER_VERSION
        ):
            raise CorpusError("resume directory has incompatible run metadata")
        return resolved, True
    if output_dir is None:
        return _allocate_default_output_dir(), False
    resolved = output_dir.expanduser().resolve()
    if resolved.exists() and not resolved.is_dir():
        raise CorpusError("--output-dir must identify a directory")
    resolved.mkdir(parents=True, exist_ok=True)
    if any(resolved.iterdir()):
        raise CorpusError(
            "--output-dir must be empty; use --resume for an existing run",
        )
    return resolved, False


def _read_command_outputs(case: CorpusCase) -> list[str]:
    outputs: list[str] = []
    for sample in case.samples:
        try:
            payload = sample.absolute_path.read_bytes()
            if hashlib.sha256(payload).hexdigest() != sample.sha256:
                raise CorpusError(
                    f"validated sample changed after preflight: {sample.path}",
                )
            outputs.append(payload.decode("utf-8", errors="strict"))
        except (OSError, UnicodeDecodeError) as error:
            raise CorpusError(
                f"validated sample became unreadable: {sample.path}",
            ) from error
    return outputs


def _serialise_issues(issues: list[Any]) -> list[dict[str, Any]]:
    return [issue.model_dump(mode="json") for issue in issues]


def _issue_category(
    issues: list[Any],
    default: str = "generation",
    *,
    acceptance_stage_category: str = "acceptance",
) -> str:
    for issue in issues:
        stage = getattr(issue, "stage", None)
        code = getattr(issue, "code", "")
        if stage == "model" or code.startswith("model."):
            return "model"
    for issue in issues:
        stage = getattr(issue, "stage", None)
        code = getattr(issue, "code", "")
        if stage == "acceptance":
            return acceptance_stage_category
        if stage == "schema" or code.startswith("schema."):
            return "schema"
        if stage == "ttp" or code.startswith("ttp."):
            return "ttp"
    return default


def _independent_acceptance(
    artifact: Any,
    command_outputs: list[str],
) -> dict[str, Any]:
    from cli_parser_agent.ttp_generation.validation import (
        validate_result_schema,
        validate_ttp_template,
    )

    schema_issues = validate_result_schema(artifact.result_schema)
    if schema_issues:
        return {
            "valid": False,
            "category": "schema",
            "schema_issues": _serialise_issues(schema_issues),
            "ttp_issues": [],
            "record_count_matches": False,
            "records_match": False,
        }
    validation = validate_ttp_template(
        artifact.ttp_template,
        command_outputs,
        artifact.result_schema,
    )
    record_count_matches = len(validation.records) == len(command_outputs)
    records_match = validation.records == artifact.records
    valid = validation.valid and record_count_matches and records_match
    if not validation.valid:
        category = _issue_category(validation.issues, default="ttp")
    elif not record_count_matches or not records_match:
        category = "acceptance"
    else:
        category = None
    return {
        "valid": valid,
        "category": category,
        "schema_issues": [],
        "ttp_issues": _serialise_issues(validation.issues),
        "record_count_matches": record_count_matches,
        "records_match": records_match,
    }


def _case_artifact_path(output_dir: Path, case_id: str) -> Path:
    return output_dir / "cases" / f"{case_id}.json"


def _load_previous_accepted_case(
    output_dir: Path,
    case: CorpusCase,
    corpus_sha256: str,
    prompt_version: str,
) -> tuple[dict[str, Any], Any] | None:
    from cli_parser_agent import GenerationResult

    path = _case_artifact_path(output_dir, case.id)
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    acceptance = data.get("independent_acceptance")
    generation = data.get("generation_result")
    previously_accepted = bool(
        data.get("case_id") == case.id
        and data.get("corpus_sha256") == corpus_sha256
        and data.get("status") == "success"
        and isinstance(generation, dict)
        and generation.get("status") == "success"
        and isinstance(generation.get("metadata"), dict)
        and generation["metadata"].get("prompt_version") == prompt_version
        and isinstance(acceptance, dict)
        and acceptance.get("valid") is True
        and acceptance.get("record_count_matches") is True
        and acceptance.get("records_match") is True
    )
    if not previously_accepted:
        return None
    try:
        result = GenerationResult.model_validate(generation)
    except Exception:
        return None
    if result.status != "success" or result.artifact is None:
        return None
    return data, result


async def _revalidate_previous_case(
    output_dir: Path,
    case: CorpusCase,
    corpus_sha256: str,
    prompt_version: str,
) -> bool:
    loaded = _load_previous_accepted_case(
        output_dir,
        case,
        corpus_sha256,
        prompt_version,
    )
    if loaded is None:
        return False
    data, result = loaded
    command_outputs = _read_command_outputs(case)
    acceptance = await asyncio.to_thread(
        _independent_acceptance,
        result.artifact,
        command_outputs,
    )
    if not acceptance["valid"]:
        return False
    data["independent_acceptance"] = acceptance
    data["resume_revalidated_at"] = _utc_now()
    _json_dump(_case_artifact_path(output_dir, case.id), data)
    return True


async def _execute_case(
    *,
    case: CorpusCase,
    generator: Any,
    semaphore: asyncio.Semaphore,
    output_dir: Path,
    corpus_sha256: str,
) -> dict[str, Any]:
    from cli_parser_agent import GenerationRequest

    payload: dict[str, Any] = {
        "case_id": case.id,
        "source": case.source,
        "platform": case.platform,
        "command": case.command,
        "sample_paths": [sample.path for sample in case.samples],
        "corpus_sha256": corpus_sha256,
    }
    async with semaphore:
        started_at = _utc_now()
        started = time.monotonic()
        payload["started_at"] = started_at
        try:
            command_outputs = _read_command_outputs(case)
            result = await generator.generate(
                GenerationRequest(command_outputs=command_outputs),
            )
            result_data = result.model_dump(mode="json")
            payload["generation_result"] = result_data
            if result.status != "success" or result.artifact is None:
                payload.update(
                    status="failed",
                    category=_issue_category(
                        result.issues,
                        acceptance_stage_category="ttp",
                    ),
                    independent_acceptance=None,
                )
            else:
                acceptance = await asyncio.to_thread(
                    _independent_acceptance,
                    result.artifact,
                    command_outputs,
                )
                payload["independent_acceptance"] = acceptance
                if acceptance["valid"]:
                    payload.update(status="success", category=None)
                else:
                    payload.update(
                        status="failed",
                        category=acceptance["category"] or "acceptance",
                    )
        except (asyncio.CancelledError, CorpusError):
            raise
        except Exception as error:
            payload.update(
                status="failed",
                category="generation",
                generation_result=None,
                independent_acceptance=None,
                error={
                    "code": "runner.case_failed",
                    "message": (
                        "case execution stopped because an internal component failed"
                    ),
                    "exception_type": type(error).__name__,
                },
            )
        payload["finished_at"] = _utc_now()
        payload["elapsed_seconds"] = round(
            max(0.0, time.monotonic() - started),
            6,
        )
    _json_dump(_case_artifact_path(output_dir, case.id), payload)
    return payload


def _build_summary(
    *,
    selected: list[CorpusCase],
    completed: list[dict[str, Any]],
    skipped: list[str],
    started_at: str,
    output_dir: Path,
) -> dict[str, Any]:
    failed = [item for item in completed if item.get("status") != "success"]
    categories: dict[str, int] = {}
    for item in failed:
        category = str(item.get("category") or "generation")
        categories[category] = categories.get(category, 0) + 1
    succeeded = sum(item.get("status") == "success" for item in completed)
    return {
        "runner_version": RUNNER_VERSION,
        "started_at": started_at,
        "finished_at": _utc_now(),
        "output_dir": str(output_dir),
        "selected_case_count": len(selected),
        "executed_case_count": len(completed),
        "skipped_case_count": len(skipped),
        "successful_case_count": succeeded + len(skipped),
        "failed_case_count": len(failed),
        "failed_by_category": categories,
        "skipped_case_ids": skipped,
        "failed_case_ids": [str(item["case_id"]) for item in failed],
        "status": "success" if not failed else "failed",
    }


async def _run_selected_cases(
    *,
    args: argparse.Namespace,
    manifest: CorpusManifest,
    selected: list[CorpusCase],
    output_dir: Path,
    is_resume: bool,
    generator: Any,
    prompt_version: str,
) -> int:
    started_at = _utc_now()
    skipped: list[str] = []
    if is_resume:
        for case in selected:
            if await _revalidate_previous_case(
                output_dir,
                case,
                manifest.sha256,
                prompt_version,
            ):
                skipped.append(case.id)
                print(f"{case.id}: skipped (resume revalidated)", flush=True)
    pending = [case for case in selected if case.id not in set(skipped)]
    run_metadata = {
        "runner_version": RUNNER_VERSION,
        "corpus_version": manifest.version,
        "corpus_sha256": manifest.sha256,
        "prompt_version": prompt_version,
        "started_at": started_at,
        "selection": {
            "suite": args.suite,
            "case_ids": args.case_ids or [],
            "source": args.source,
            "platform": args.platform,
            "max_cases": args.max_cases,
            "concurrency": args.concurrency,
            "selected_case_ids": [case.id for case in selected],
        },
        "resumed": is_resume,
    }
    _json_dump(output_dir / "run.json", run_metadata)

    semaphore = asyncio.Semaphore(args.concurrency)
    tasks = [
        asyncio.create_task(
            _execute_case(
                case=case,
                generator=generator,
                semaphore=semaphore,
                output_dir=output_dir,
                corpus_sha256=manifest.sha256,
            ),
        )
        for case in pending
    ]
    completed: list[dict[str, Any]] = []
    try:
        for task in asyncio.as_completed(tasks):
            result = await task
            completed.append(result)
            print(f"{result['case_id']}: {result['status']}", flush=True)
    except BaseException:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        raise

    order = {case.id: index for index, case in enumerate(selected)}
    completed.sort(key=lambda item: order[str(item["case_id"])])
    summary = _build_summary(
        selected=selected,
        completed=completed,
        skipped=skipped,
        started_at=started_at,
        output_dir=output_dir,
    )
    _json_dump(output_dir / "summary.json", summary)
    print(
        f"summary: successful={summary['successful_case_count']} "
        f"failed={summary['failed_case_count']} "
        f"skipped={summary['skipped_case_count']}",
    )
    print(f"artifacts: {output_dir}")
    return 0 if summary["status"] == "success" else 1


def _command_run(args: argparse.Namespace) -> int:
    from cli_parser_agent import TtpGenerator
    from cli_parser_agent.ttp_generation.agent import PROMPT_VERSION

    try:
        manifest = load_and_preflight_corpus()
        selected = _select_cases(
            manifest,
            suite=args.suite,
            requested_ids=args.case_ids,
            source=args.source,
            platform=args.platform,
            max_cases=args.max_cases,
        )
        try:
            generator = TtpGenerator.from_env()
        except Exception as error:
            raise CorpusError(
                "live model configuration is missing or invalid "
                f"({type(error).__name__})",
            ) from None
        output_dir, is_resume = _prepare_output_dir(args.output_dir, args.resume)
        print(
            f"selected cases={len(selected)} samples="
            f"{sum(len(case.samples) for case in selected)} "
            f"concurrency={args.concurrency}",
        )
        return asyncio.run(
            _run_selected_cases(
                args=args,
                manifest=manifest,
                selected=selected,
                output_dir=output_dir,
                is_resume=is_resume,
                generator=generator,
                prompt_version=PROMPT_VERSION,
            ),
        )
    finally:
        _flush_laminar()


def _flush_laminar() -> None:
    from lmnr import Laminar

    if not Laminar.is_initialized():
        return
    try:
        flushed = Laminar.flush()
    except Exception as error:
        print(
            f"warning: Laminar flush failed ({type(error).__name__})",
            file=sys.stderr,
        )
        return
    if not flushed:
        print("warning: Laminar flush did not complete", file=sys.stderr)


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "list":
            return _command_list(args)
        if args.command == "preflight":
            return _command_preflight()
        return _command_run(args)
    except CorpusError as error:
        print(f"error: {error}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("interrupted", file=sys.stderr)
        return 130
    except Exception as error:
        print(
            f"error: live corpus runner stopped unexpectedly ({type(error).__name__})",
            file=sys.stderr,
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
