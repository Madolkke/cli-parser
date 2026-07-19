"""Phase control for the schema-then-template generation protocol."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from agentscope.middleware import MiddlewareBase

from .tools import GenerationSession


class GenerationPhaseMiddleware(MiddlewareBase):
    """Expose one phase tool without sending ``tool_choice`` to the provider."""

    def __init__(self, session: GenerationSession) -> None:
        self.session = session

    async def on_model_call(
        self,
        agent: Any,
        input_kwargs: dict[str, Any],
        next_handler: Callable[..., Awaitable[Any]],
    ) -> Any:
        """Filter schemas at the API boundary and omit wire ``tool_choice``."""

        del agent
        expected_tool = self.session.current_phase_tool_name()
        available_tools = input_kwargs.get("tools")
        if not isinstance(available_tools, list):
            raise RuntimeError("Model tool schemas must be provided as a list.")

        if expected_tool is None:
            filtered_tools: list[dict[str, Any]] = []
        else:
            filtered_tools = [
                schema
                for schema in available_tools
                if isinstance(schema, dict)
                and schema.get("function", {}).get("name") == expected_tool
            ]
            if len(filtered_tools) != 1:
                raise RuntimeError(
                    "Exactly one current-phase submission tool schema is required.",
                )

        return await next_handler(
            **{
                **input_kwargs,
                "tools": filtered_tools,
                "tool_choice": None,
            },
        )

    async def on_compress_context(
        self,
        agent: Any,
        input_kwargs: dict[str, Any],
        next_handler: Callable[..., Awaitable[None]],
    ) -> None:
        """Disable lossy summarization of command outputs and field evidence."""

        # Initial input is fitted below AgentScope's context threshold. If
        # later rounds outgrow the model context, a structured model failure
        # is safer than replacing exact source data with a generated summary.
        return None


__all__ = ["GenerationPhaseMiddleware"]
