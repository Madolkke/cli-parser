"""Deterministic tests for the standalone live-corpus runner."""

from __future__ import annotations

import importlib.util
import json
import sys
from argparse import Namespace
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
from lmnr import Laminar

SCRIPT_PATH = Path(__file__).resolve().parents[2] / "scripts" / "run_live_corpus.py"


def _load_script() -> ModuleType:
    spec = importlib.util.spec_from_file_location("run_live_corpus", SCRIPT_PATH)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _generation_result(prompt_version: str) -> dict[str, object]:
    schema = {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "type": "object",
        "properties": {"value": {"type": "string"}},
        "required": ["value"],
        "additionalProperties": False,
    }
    return {
        "status": "success",
        "artifact": {
            "ttp_template": "Value: {{ value }}",
            "result_schema": schema,
            "records": [{"value": "one"}],
            "assumptions": [],
        },
        "issues": [],
        "metadata": {
            "model_name": "test-model",
            "prompt_version": prompt_version,
            "command_output_count": 1,
        },
        "last_attempt": None,
    }


def test_resume_rejects_success_from_a_different_prompt_version(
    tmp_path: Path,
) -> None:
    script = _load_script()
    case = script.CorpusCase(
        id="test.case",
        source="test",
        platform="test",
        command="show test",
        shapes=("line",),
        suites=("smoke",),
        samples=(),
    )
    corpus_sha256 = "a" * 64
    case_path = tmp_path / "cases" / f"{case.id}.json"
    case_path.parent.mkdir()
    case_path.write_text(
        json.dumps(
            {
                "case_id": case.id,
                "corpus_sha256": corpus_sha256,
                "status": "success",
                "generation_result": _generation_result("old-prompt"),
                "independent_acceptance": {
                    "valid": True,
                    "record_count_matches": True,
                    "records_match": True,
                },
            },
        ),
        encoding="utf-8",
    )

    assert (
        script._load_previous_accepted_case(
            tmp_path,
            case,
            corpus_sha256,
            "current-prompt",
        )
        is None
    )
    assert (
        script._load_previous_accepted_case(
            tmp_path,
            case,
            corpus_sha256,
            "old-prompt",
        )
        is not None
    )


@pytest.mark.parametrize(
    "argv",
    [
        ["list", "--suite", "smoke"],
        ["preflight"],
    ],
)
def test_non_run_commands_do_not_initialize_or_flush_laminar(
    monkeypatch: pytest.MonkeyPatch,
    argv: list[str],
) -> None:
    script = _load_script()
    monkeypatch.setattr(
        Laminar,
        "initialize",
        lambda **_: pytest.fail("Laminar must not initialize for list/preflight"),
    )
    monkeypatch.setattr(
        Laminar,
        "flush",
        lambda: pytest.fail("Laminar must not flush for list/preflight"),
    )
    monkeypatch.setattr(
        script,
        "_command_run",
        lambda _: pytest.fail("list/preflight must not enter the live run path"),
    )

    assert script.main(argv) == 0


@pytest.mark.parametrize("initialized", [False, True])
def test_live_run_flushes_an_initialized_sdk_on_failure(
    monkeypatch: pytest.MonkeyPatch,
    initialized: bool,
) -> None:
    script = _load_script()
    flush_calls: list[None] = []
    monkeypatch.setattr(Laminar, "is_initialized", lambda: initialized)
    monkeypatch.setattr(Laminar, "flush", lambda: flush_calls.append(None) or True)

    def fail_preflight() -> Any:
        raise script.CorpusError("invalid corpus")

    monkeypatch.setattr(script, "load_and_preflight_corpus", fail_preflight)
    args = Namespace(
        suite="smoke",
        case_ids=[],
        source=None,
        platform=None,
        max_cases=None,
        concurrency=1,
        output_dir=None,
        resume=None,
    )

    with pytest.raises(script.CorpusError, match="invalid corpus"):
        script._command_run(args)

    assert len(flush_calls) == int(initialized)


@pytest.mark.parametrize("raises", [False, True])
def test_live_runner_flush_failure_only_warns(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    raises: bool,
) -> None:
    script = _load_script()
    monkeypatch.setattr(Laminar, "is_initialized", lambda: True)

    def flush() -> bool:
        if raises:
            raise RuntimeError("private exporter details")
        return False

    monkeypatch.setattr(Laminar, "flush", flush)

    script._flush_laminar()

    error = capsys.readouterr().err
    assert "warning: Laminar flush" in error
    assert "private exporter details" not in error
