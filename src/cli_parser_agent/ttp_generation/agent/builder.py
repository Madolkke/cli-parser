"""Construction of one isolated AgentScope agent per generation request."""

from __future__ import annotations

import logging
from typing import Any, Protocol

from agentscope.agent import Agent, ReActConfig
from agentscope.credential import OpenAICredential
from agentscope.message import SystemMsg, UserMsg
from agentscope.model import OpenAIChatModel
from agentscope.state import AgentState
from agentscope.tool import Toolkit

from .middleware import GenerationPhaseMiddleware
from .prompt import SYSTEM_PROMPT, build_task_prompt
from .tools import (
    SUBMIT_SCHEMA_TOOL_NAME,
    GenerationSession,
    build_submission_tools,
)


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


def build_agent(
    *,
    settings: SettingsLike,
    policy: PolicyLike,
    session: GenerationSession,
) -> Agent:
    """Build a fresh AgentScope ``Agent`` and ``AgentState`` for a request."""

    if policy.max_agent_rounds < 1:
        raise ValueError("policy.max_agent_rounds must be positive")
    session.apply_policy(policy)
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

    state = AgentState()
    toolkit = Toolkit(tools=build_submission_tools(session))
    return Agent(
        name="ttp_generator",
        system_prompt=SYSTEM_PROMPT,
        model=model,
        toolkit=toolkit,
        middlewares=[GenerationPhaseMiddleware(session)],
        state=state,
        react_config=ReActConfig(
            max_iters=policy.max_agent_rounds,
            interruption_raise_cancelled_error=True,
        ),
    )


def build_task_message(command_outputs: list[str] | tuple[str, ...]) -> UserMsg:
    """Convert sampled outputs into the sole AgentScope input message."""

    return UserMsg(
        name="user",
        content=build_task_prompt(command_outputs),
    )


async def estimate_initial_model_tokens(agent: Agent, message: UserMsg) -> int:
    """Use the configured AgentScope model's exact initial token estimator."""

    available_tools = await agent.toolkit.get_tool_schemas(
        agent.state.tool_context.activated_groups,
    )
    tools = [
        schema
        for schema in available_tools
        if schema.get("function", {}).get("name") == SUBMIT_SCHEMA_TOOL_NAME
    ]
    if len(tools) != 1:
        raise RuntimeError(
            "Exactly one submit_result_schema tool schema is required for "
            "initial token estimation.",
        )
    return await agent.model.count_tokens(
        [SystemMsg(name="system", content=SYSTEM_PROMPT), message],
        tools,
    )


# Descriptive alias retained as the primary domain-oriented spelling.
build_ttp_generator_agent = build_agent


__all__ = [
    "PolicyLike",
    "SettingsLike",
    "build_agent",
    "build_task_message",
    "build_ttp_generator_agent",
    "estimate_initial_model_tokens",
]
