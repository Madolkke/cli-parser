"""Preserve Schema reasoning in provider history without changing ordinary rows.

AgentScope can accumulate several tool rounds in one assistant ``Msg``. A
reasoning block belongs to the assistant segment before the next tool result or
hint, not to every assistant row derived from that message. The upstream
formatter remains responsible for text, tool arguments, results and multimodal
content; this adapter adds only the provider's ``reasoning_content`` field.
"""

from __future__ import annotations

from typing import Any

from agentscope.formatter import OpenAIChatFormatter
from agentscope.message import (
    ContentBlock,
    HintBlock,
    Msg,
    ThinkingBlock,
    ToolResultBlock,
)


class SchemaReasoningOpenAIFormatter(OpenAIChatFormatter):
    """Private formatter for providers accepting assistant reasoning history."""

    async def format(self, msgs: list[Msg]) -> list[dict[str, Any]]:
        self.assert_list_of_msgs(msgs)
        rows: list[dict[str, Any]] = []
        for msg in msgs:
            if msg.role != "assistant" or not msg.has_content_blocks("thinking"):
                rows.extend(await super().format([msg]))
                continue

            segment: list[ContentBlock] = []
            for block in msg.get_content_blocks():
                if isinstance(block, (ToolResultBlock, HintBlock)):
                    rows.extend(await self._format_reasoning_segment(msg, segment))
                    segment = []
                    # Both block types flush upstream assistant content and
                    # may emit multiple rows. Never attach reasoning to them.
                    boundary = msg.model_copy(update={"content": [block]})
                    rows.extend(await super().format([boundary]))
                else:
                    segment.append(block)
            rows.extend(await self._format_reasoning_segment(msg, segment))
        return rows

    async def _format_reasoning_segment(
        self, msg: Msg, blocks: list[ContentBlock]
    ) -> list[dict[str, Any]]:
        if not blocks:
            return []
        segment = msg.model_copy(update={"content": list(blocks)})
        rows = await super().format([segment])
        reasoning_blocks = [
            block.thinking for block in blocks if isinstance(block, ThinkingBlock)
        ]
        if not reasoning_blocks:
            return rows
        reasoning = "".join(reasoning_blocks)
        if not rows:
            rows = [{"role": "assistant", "name": msg.name, "content": ""}]
        # The segment contains neither of the upstream row-flushing blocks,
        # so its ordinary content yields at most one assistant row.
        rows[0]["reasoning_content"] = reasoning
        return rows
