"""Synchronous, fail-open progress events for generation debugging."""

from __future__ import annotations

import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any, TypeAlias

from agentscope.event import AgentEvent, CustomEvent

ProgressObserver: TypeAlias = Callable[[AgentEvent], None]
"""A synchronous observer for copied AgentScope and project events."""


@dataclass(slots=True)
class ProgressEmitter:
    """Stamp and synchronously forward events without affecting generation."""

    request_id: str
    observer: ProgressObserver | None = None
    started_monotonic: float = field(default_factory=time.monotonic)
    _sequence: int = field(default=0, init=False)
    _disabled: bool = field(default=False, init=False)

    @property
    def enabled(self) -> bool:
        """Whether an observer can still receive events."""

        return self.observer is not None and not self._disabled

    def emit(
        self,
        event: AgentEvent,
        *,
        phase: str,
        sensitive: bool,
    ) -> None:
        """Forward a deep event copy with request-scoped metadata."""

        if not self.enabled:
            return

        try:
            self._sequence += 1
            metadata = dict(event.metadata or {})
            metadata.update(
                {
                    "request_id": self.request_id,
                    "sequence": self._sequence,
                    "elapsed_seconds": max(
                        0.0,
                        time.monotonic() - self.started_monotonic,
                    ),
                    "phase": phase,
                    "sensitive": sensitive,
                },
            )
            copied = event.model_copy(
                deep=True,
                update={"metadata": metadata},
            )
            assert self.observer is not None
            self.observer(copied)
        except BaseException:
            # Debug event preparation and observers must never influence
            # generation. Disable the channel after its first error and avoid
            # logging the exception or any event content.
            self._disabled = True

    def custom(
        self,
        name: str,
        value: Mapping[str, Any] | None = None,
        *,
        phase: str,
        sensitive: bool,
    ) -> None:
        """Emit one namespaced project event when observation is enabled."""

        if not self.enabled:
            return
        self.emit(
            CustomEvent(name=name, value=dict(value or {})),
            phase=phase,
            sensitive=sensitive,
        )


__all__ = ["ProgressEmitter", "ProgressObserver"]
