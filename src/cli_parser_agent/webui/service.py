"""Service boundary between the WebUI and a generation backend."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from typing import Any, Protocol

from .contracts import RunMode, WebUIProgressEvent

ProgressObserver = Callable[[WebUIProgressEvent], None]


class GenerationService(Protocol):
    """The only application service the WebUI needs to know about."""

    def validate_inputs(self, command_outputs: Sequence[str]) -> list[str]:
        """Validate and normalize inputs before a run directory is created."""

    async def run(
        self,
        mode: RunMode,
        command_outputs: Sequence[str],
        *,
        observer: ProgressObserver | None = None,
    ) -> dict[str, Any]:
        """Run either the complete or Schema proposal workflow."""

    async def run_from_schema(
        self,
        command_outputs: Sequence[str],
        result_schema: dict[str, Any],
        *,
        observer: ProgressObserver | None = None,
    ) -> dict[str, Any]:
        """Run the template workflow from a saved Schema."""

    def validate_schema(self, result_schema: dict[str, Any]) -> list[dict[str, Any]]:
        """Validate a Schema and return JSON-compatible issue objects."""


__all__ = ["GenerationService", "ProgressObserver"]
