"""Local single-user WebUI for one-off TTP generation runs.

This surface is a development/operator tool, not a deployed product service:
it binds the loopback interface, has no authentication, and stores runs as
plain files under ``data/``.
"""

from __future__ import annotations

from .store import RunStore, RunStoreError

__all__ = ["RunStore", "RunStoreError"]
