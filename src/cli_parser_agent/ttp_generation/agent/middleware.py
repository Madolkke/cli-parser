"""Context-safety middleware shared by both isolated phases."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from agentscope.middleware import MiddlewareBase


class LosslessContextMiddleware(MiddlewareBase):
    """Disable lossy summarization of source data and validator feedback."""

    async def on_compress_context(
        self,
        agent: Any,
        input_kwargs: dict[str, Any],
        next_handler: Callable[..., Awaitable[None]],
    ) -> None:
        """Fail at the model boundary instead of replacing exact context."""

        # Initial input is fitted below AgentScope's context threshold. If
        # later rounds outgrow the model context, generated compression could
        # corrupt exact source data or deterministic validator feedback.
        return None


__all__ = ["LosslessContextMiddleware"]
