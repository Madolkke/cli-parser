"""Shared pytest configuration."""

from __future__ import annotations

import os

import pytest


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    """Skip live tests unless the caller explicitly enables network usage."""
    if os.getenv("RUN_LIVE_TESTS") == "1":
        return

    marker = pytest.mark.skip(
        reason="set RUN_LIVE_TESTS=1 to run real-model tests",
    )
    for item in items:
        if "live" in item.keywords:
            item.add_marker(marker)


@pytest.fixture(autouse=True)
def disable_laminar_environment_for_offline_tests(
    request: pytest.FixtureRequest,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Keep deterministic tests offline even if the developer shell has a key."""

    if "live" in request.keywords:
        return
    monkeypatch.delenv("LMNR_PROJECT_API_KEY", raising=False)
    monkeypatch.delenv("LMNR_BASE_URL", raising=False)
    monkeypatch.delenv("LMNR_HTTP_PORT", raising=False)
    monkeypatch.delenv("LMNR_GRPC_PORT", raising=False)
