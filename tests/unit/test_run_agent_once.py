"""Deterministic tests for the zero-argument live Agent runner."""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType

import pytest

SCRIPT_PATH = Path(__file__).resolve().parents[2] / "scripts" / "run_agent_once.py"


def _load_script() -> ModuleType:
    spec = importlib.util.spec_from_file_location("run_agent_once", SCRIPT_PATH)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_default_command_outputs_are_valid_and_ordered() -> None:
    script = _load_script()

    outputs, metadata = script._load_command_outputs()

    assert len(outputs) == 3
    assert all(output.strip() for output in outputs)
    assert [Path(item["path"]).name for item in metadata] == [
        "cisco_ios_show_interfaces_status.raw",
        "cisco_ios_show_interfaces_status2.raw",
        "cisco_ios_show_interfaces_status_pvlan.raw",
    ]
    assert all(len(item["sha256"]) == 64 for item in metadata)


def test_command_output_loader_rejects_non_utf8(tmp_path: Path) -> None:
    script = _load_script()
    source = tmp_path / "invalid.raw"
    source.write_bytes(b"\xff")

    with pytest.raises(script.ScriptConfigurationError, match="strict UTF-8"):
        script._load_command_outputs((source,))


def test_api_key_prefers_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    script = _load_script()
    monkeypatch.setenv(script.API_KEY_ENVIRONMENT_VARIABLE, "test-key")

    assert script._resolve_api_key() == "test-key"
