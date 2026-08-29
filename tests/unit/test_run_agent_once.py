"""Deterministic tests for the zero-argument live Agent runner."""

from __future__ import annotations

import importlib.util
import os
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest
from lmnr import Laminar

SCRIPT_PATH = Path(__file__).resolve().parents[2] / "scripts" / "run_agent_once.py"
PROJECT_ROOT = SCRIPT_PATH.parents[1]


def _load_script() -> ModuleType:
    spec = importlib.util.spec_from_file_location("run_agent_once", SCRIPT_PATH)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _configuration_environment(
    input_paths: tuple[Path, ...] | None = None,
) -> dict[str, str]:
    if input_paths is None:
        input_paths = (PROJECT_ROOT / "test-input.txt",)
    return {
        "OPENAI_API_KEY": "test-key",
        "OPENAI_MODEL": "test-model",
        "CLI_PARSER_ONCE_INPUT_FILES": os.pathsep.join(
            str(path)
            for path in input_paths
        ),
    }


def test_environment_command_outputs_are_valid_and_ordered(tmp_path: Path) -> None:
    script = _load_script()
    input_paths = tuple(
        tmp_path / f"{index:03}.txt"
        for index in range(1, 4)
    )
    for index, path in enumerate(input_paths, start=1):
        path.write_text(f"sample output {index}\n", encoding="utf-8")

    _, _, paths, _ = script._configuration(
        _configuration_environment(input_paths),
    )

    outputs, metadata = script._load_command_outputs(paths)

    assert len(outputs) == 3
    assert all(output.strip() for output in outputs)
    assert [Path(item["path"]).name for item in metadata] == [
        "001.txt",
        "002.txt",
        "003.txt",
    ]
    assert all(len(item["sha256"]) == 64 for item in metadata)


def test_command_output_loader_rejects_non_utf8(tmp_path: Path) -> None:
    script = _load_script()
    source = tmp_path / "invalid.raw"
    source.write_bytes(b"\xff")

    with pytest.raises(script.ScriptConfigurationError, match="strict UTF-8"):
        script._load_command_outputs((source,))


def test_configuration_reads_model_settings_from_environment() -> None:
    script = _load_script()
    settings, _, _, _ = script._configuration(_configuration_environment())

    assert settings.api_key.get_secret_value() == "test-key"
    assert settings.model_name == "test-model"

@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (None, None),
        ("https://model.invalid/v1", "https://model.invalid/v1"),
        (
            "https://user:password@model.invalid:8443/v1?api_key=secret#debug",
            "https://model.invalid:8443/v1",
        ),
        ("https://[::1]:8000/v1?token=secret", "https://[::1]:8000/v1"),
        ("https://model.invalid:invalid/v1", "[REDACTED]"),
    ],
)
def test_base_url_sanitization(value: str | None, expected: str | None) -> None:
    script = _load_script()

    assert script._run_support.sanitize_base_url(value) == expected


def test_result_summary_prints_laminar_trace_id(
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    script = _load_script()
    metadata = SimpleNamespace(
        laminar_trace_id="01234567-89ab-cdef-0123-456789abcdef",
        termination_reason="success",
        elapsed_seconds=1.25,
        agent_rounds=2,
        schema_agent_rounds=1,
        ttp_agent_rounds=1,
        schema_sampled_char_count=120,
        ttp_sampled_char_count=100,
        tool_call_starts=2,
        tool_result_errors=0,
        schema_submissions=1,
        ttp_submissions=1,
        schema_no_tool_responses=0,
        ttp_no_tool_responses=0,
        schema_no_tool_retries=0,
        ttp_no_tool_retries=0,
        first_ttp_passed=True,
    )
    result = SimpleNamespace(status="success", metadata=metadata, issues=[])

    script._print_result_summary(result, tmp_path / "result.json")

    output = capsys.readouterr().out
    assert "laminar_trace_id: 01234567-89ab-cdef-0123-456789abcdef" in output
    assert "agent_rounds: 2" in output
    assert "schema_agent_rounds: 1" in output
    assert "ttp_agent_rounds: 1" in output
    assert "schema_sampled_char_count: 120" in output
    assert "ttp_sampled_char_count: 100" in output


@pytest.mark.parametrize("initialized", [False, True])
def test_flush_laminar_only_flushes_an_initialized_sdk(
    monkeypatch: pytest.MonkeyPatch,
    initialized: bool,
) -> None:
    script = _load_script()
    flush_calls: list[None] = []
    monkeypatch.setattr(Laminar, "is_initialized", lambda: initialized)
    monkeypatch.setattr(Laminar, "flush", lambda: flush_calls.append(None) or True)

    script._flush_laminar()

    assert len(flush_calls) == int(initialized)


@pytest.mark.parametrize("raises", [False, True])
def test_flush_laminar_failure_only_warns(
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


def test_main_flushes_laminar_on_the_exit_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    script = _load_script()
    flush_calls: list[None] = []

    async def run() -> int:
        return 1

    monkeypatch.setattr(script, "_run", run)
    monkeypatch.setattr(script, "_flush_laminar", lambda: flush_calls.append(None))

    assert script.main() == 1
    assert flush_calls == [None]
