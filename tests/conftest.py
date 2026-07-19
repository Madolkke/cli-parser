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
