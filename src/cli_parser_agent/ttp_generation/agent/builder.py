"""Construction of isolated AgentScope agents for each generation phase."""

from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence
from typing import Any, Protocol

from agentscope.agent import Agent, ReActConfig
from agentscope.credential import OpenAICredential
from agentscope.message import SystemMsg, UserMsg
from agentscope.model import OpenAIChatModel
from agentscope.state import AgentState
from agentscope.tool import Toolkit

from .middleware import LosslessContextMiddleware
from .prompt import (
    SCHEMA_SYSTEM_PROMPT,
    TTP_SYSTEM_PROMPT,
    build_schema_task_prompt,
    build_ttp_task_prompt,
)
from .session import GenerationPhase, GenerationSession
from .tools import (
    SUBMIT_SCHEMA_TOOL_NAME,
    SUBMIT_TEMPLATE_TOOL_NAME,
    build_submission_tools,
)

_PHASE_AGENT_NAMES: dict[GenerationPhase, str] = {
    "schema": "ttp_schema_generator",
    "ttp": "ttp_template_generator",
}
_PHASE_SYSTEM_PROMPTS: dict[GenerationPhase, str] = {
    "schema": SCHEMA_SYSTEM_PROMPT,
    "ttp": TTP_SYSTEM_PROMPT,
}
_PHASE_TOOL_NAMES: dict[GenerationPhase, str] = {
    "schema": SUBMIT_SCHEMA_TOOL_NAME,
    "ttp": SUBMIT_TEMPLATE_TOOL_NAME,
}


class _SafeAgentScopeLogFilter(logging.Filter):
    """Remove provider exception text from AgentScope retry warnings."""

    def filter(self, record: logging.LogRecord) -> bool:
        message = record.msg
        if isinstance(message, str) and message.startswith(
            "Attempt %d failed for model %s: %s.",
        ):
            record.msg = "Model request failed; retrying without response details."
            record.args = ()
        return True


def _install_safe_agentscope_log_filter() -> None:
    logger = logging.getLogger("as")
    if not any(isinstance(item, _SafeAgentScopeLogFilter) for item in logger.filters):
        logger.addFilter(_SafeAgentScopeLogFilter())


class SettingsLike(Protocol):
    """Configuration fields consumed by the AgentScope adapter."""

    api_key: Any
    model_name: str
    base_url: str | None
    stream: bool
    temperature: float
    parallel_tool_calls: bool
    max_tokens: int
    context_size: int
    model_max_retries: int
    model_timeout_seconds: float


class PolicyLike(Protocol):
    """Generation policy fields consumed by the AgentScope adapter."""

    total_timeout_seconds: float
    max_agent_rounds: int
    max_ttp_submissions: int
    max_schema_no_tool_retries: int
    max_ttp_no_tool_retries: int


def _plain_secret(value: Any) -> str:
    getter = getattr(value, "get_secret_value", None)
    return getter() if callable(getter) else str(value)


def _validate_phase(phase: GenerationPhase) -> GenerationPhase:
    if phase not in _PHASE_AGENT_NAMES:
        raise ValueError(f"unsupported generation phase: {phase!r}")
    return phase


def build_agent(
    *,
    settings: SettingsLike,
    policy: PolicyLike,
    session: GenerationSession,
    phase: GenerationPhase,
) -> Agent:
    """Build a fresh model, agent state, and one-tool toolkit for a phase."""

    phase = _validate_phase(phase)
    if policy.max_agent_rounds < 1:
        raise ValueError("policy.max_agent_rounds must be positive")
    _install_safe_agentscope_log_filter()

    credential = OpenAICredential(
        api_key=_plain_secret(settings.api_key),
        base_url=settings.base_url,
    )
    parameters = OpenAIChatModel.Parameters(
        max_tokens=settings.max_tokens,
        temperature=settings.temperature,
        parallel_tool_calls=settings.parallel_tool_calls,
    )
    model = OpenAIChatModel(
        credential=credential,
        model=settings.model_name,
        parameters=parameters,
        stream=settings.stream,
        max_retries=settings.model_max_retries,
        context_size=settings.context_size,
        client_kwargs={"timeout": settings.model_timeout_seconds},
    )

    return Agent(
        name=_PHASE_AGENT_NAMES[phase],
        system_prompt=_PHASE_SYSTEM_PROMPTS[phase],
        model=model,
        toolkit=Toolkit(tools=build_submission_tools(session, phase)),
        middlewares=[LosslessContextMiddleware()],
        state=AgentState(),
        react_config=ReActConfig(
            max_iters=policy.max_agent_rounds,
            interruption_raise_cancelled_error=True,
        ),
    )


def build_schema_task_message(command_outputs: Sequence[str]) -> UserMsg:
    """Build the sole initial user message for the Schema agent."""

    return UserMsg(
        name="user",
        content=build_schema_task_prompt(command_outputs),
    )


def build_ttp_task_message(
    command_outputs: Sequence[str],
    frozen_result_schema: Mapping[str, Any],
) -> UserMsg:
    """Build the sole initial user message for the TTP agent."""

    return UserMsg(
        name="user",
        content=build_ttp_task_prompt(command_outputs, frozen_result_schema),
    )


async def estimate_initial_model_tokens(
    agent: Agent,
    message: UserMsg,
    phase: GenerationPhase,
) -> int:
    """Count one isolated phase's system, task, and sole tool schema."""

    phase = _validate_phase(phase)
    tools = await agent.toolkit.get_tool_schemas(
        agent.state.tool_context.activated_groups,
    )
    if len(tools) != 1:
        raise RuntimeError(
            "Exactly one submission tool schema is required for token estimation.",
        )
    tool_name = tools[0].get("function", {}).get("name")
    if tool_name != _PHASE_TOOL_NAMES[phase]:
        raise RuntimeError(
            "The submission tool schema does not match the requested phase.",
        )
    return await agent.model.count_tokens(
        [
            SystemMsg(
                name="system",
                content=_PHASE_SYSTEM_PROMPTS[phase],
            ),
            message,
        ],
        tools,
    )


__all__ = [
    "PolicyLike",
    "SettingsLike",
    "build_agent",
    "build_schema_task_message",
    "build_ttp_task_message",
    "estimate_initial_model_tokens",
]
