"""Tests for the zero-argument WebUI launcher configuration."""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

SCRIPT_PATH = Path(__file__).resolve().parents[2] / "scripts" / "run_webui.py"
SPEC = importlib.util.spec_from_file_location("run_webui", SCRIPT_PATH)
assert SPEC is not None and SPEC.loader is not None
SCRIPT = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(SCRIPT)


@pytest.mark.parametrize("value", ["1", "true", "yes", "on", "TRUE"])
def test_webui_stream_is_enabled_by_explicit_truthy_values(value: str) -> None:
    assert SCRIPT._webui_stream_enabled({"CLI_PARSER_WEBUI_STREAM": value}) is True


@pytest.mark.parametrize("value", ["0", "false", "no", "off", "FALSE"])
def test_webui_stream_can_be_disabled(value: str) -> None:
    assert SCRIPT._webui_stream_enabled({"CLI_PARSER_WEBUI_STREAM": value}) is False


def test_webui_stream_defaults_to_enabled() -> None:
    assert SCRIPT._webui_stream_enabled({}) is True


def test_webui_stream_rejects_ambiguous_values() -> None:
    with pytest.raises(SCRIPT.ScriptConfigurationError):
        SCRIPT._webui_stream_enabled({"CLI_PARSER_WEBUI_STREAM": "maybe"})
