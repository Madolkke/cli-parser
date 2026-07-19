"""Deterministic tests for the standalone live-corpus runner."""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from types import ModuleType

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
