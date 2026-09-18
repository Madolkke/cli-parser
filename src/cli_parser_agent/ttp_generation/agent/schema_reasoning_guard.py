"""Bound retained Schema reasoning without invoking native summarization."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any

from agentscope.agent import Agent
from agentscope.message import ThinkingBlock
from agentscope.middleware import MiddlewareBase

from ..progress import ProgressEmitter
from .session import GenerationSession


class SchemaReasoningContextLimit(RuntimeError):
    """Retained reasoning cannot safely be sent in the available context."""

    def __init__(self) -> None:
        super().__init__("Schema reasoning history exceeds its context safety limit.")


class SchemaReasoningHistoryGuard(MiddlewareBase):
    """Keep complete reasoning history or stop before compression/model I/O."""

    def __init__(
        self, session: GenerationSession, progress: ProgressEmitter | None = None
    ) -> None:
        self.session = session
        self.progress = progress

    async def on_compress_context(
        self,
        agent: Agent,
        input_kwargs: dict,
        next_handler: Callable[..., Awaitable[None]],
    ) -> None:
        if not getattr(agent.model, "schema_reasoning_history_enabled", False):
            await next_handler()
            return
        blocks = [
            block
            for message in agent.state.context
            if message.role == "assistant"
            for block in message.get_content_blocks()
            if isinstance(block, ThinkingBlock) and block.thinking
        ]
        if not blocks:
            await next_handler()
            return

        context_config = input_kwargs.get("context_config") or agent.context_config
        limit = int(
            min(
                0.5 * agent.model.context_size,
                context_config.trigger_ratio * agent.model.context_size,
                agent.model.context_size - (agent.model.parameters.max_tokens or 0),
            )
        )
        facts = {
            "round_index": self.session.agent_rounds,
            "reasoning_blocks": len(blocks),
            "reasoning_chars": sum(len(block.thinking) for block in blocks),
            "estimated_tokens": None,
            "context_limit_tokens": limit,
            "runtime_policy": "schema-reasoning-history-v1",
        }
        if agent.state.summary:
            self._emit("context_limit", facts)
            raise SchemaReasoningContextLimit()

        # Apply the same request deadline to both preparation and estimation.
        # An exception cannot fall through to compression or a provider call.
        remaining = (
            self.session.remaining_seconds()
            if self.session.deadline_monotonic is not None
            else None
        )
        if remaining is not None and remaining <= 0:
            raise TimeoutError("Schema reasoning context deadline reached.")
        try:
            async with asyncio.timeout(remaining):
                kwargs = await agent._prepare_model_input()
                estimated = await agent.model.count_tokens(**kwargs)
        except (asyncio.CancelledError, TimeoutError):
            raise
        except Exception:
            self._emit("context_limit", facts)
            raise SchemaReasoningContextLimit() from None
        if (
            self.session.deadline_monotonic is not None
            and self.session.remaining_seconds() <= 0
        ):
            raise TimeoutError("Schema reasoning context deadline reached.")
        if (
            not isinstance(estimated, int)
            or isinstance(estimated, bool)
            or estimated < 0
        ):
            self._emit("context_limit", facts)
            raise SchemaReasoningContextLimit()
        facts["estimated_tokens"] = estimated
        if estimated >= limit:
            self._emit("context_limit", facts)
            raise SchemaReasoningContextLimit()
        self._emit("context_checked", facts)
        # Do not delegate: native compression may invoke a separate model and
        # replace reasoning. The checked full history is already below all
        # relevant thresholds, so there is nothing safe or useful to compress.

    def _emit(self, status: str, facts: dict[str, Any]) -> None:
        if self.progress is not None:
            self.progress.custom(
                "cli_parser.schema.reasoning_history",
                {"status": status, **facts},
                phase="schema",
                sensitive=False,
            )
