"""AgentScope adapter for the request-scoped TTP generator."""

from .builder import (
    build_agent,
    build_task_message,
    build_ttp_generator_agent,
    estimate_initial_model_tokens,
)
from .middleware import GenerationPhaseMiddleware
from .prompt import (
    PROMPT_VERSION,
    SCHEMA_NO_TOOL_RETRY_PROMPT,
    SYSTEM_PROMPT,
    TTP_NO_TOOL_RETRY_PROMPT,
    build_task_prompt,
)
from .runner import AgentRunOutcome, run_generation_agent
from .tools import (
    SUBMIT_SCHEMA_TOOL_NAME,
    SUBMIT_TEMPLATE_TOOL_NAME,
    GenerationSession,
    SchemaCandidate,
    SchemaValidator,
    SubmitResultSchemaTool,
    SubmitTtpTemplateTool,
    TemplateCandidate,
    TemplateValidator,
    ValidatorOutcome,
    build_submission_tools,
)

__all__ = [
    "GenerationPhaseMiddleware",
    "GenerationSession",
    "AgentRunOutcome",
    "PROMPT_VERSION",
    "SCHEMA_NO_TOOL_RETRY_PROMPT",
    "SUBMIT_SCHEMA_TOOL_NAME",
    "SUBMIT_TEMPLATE_TOOL_NAME",
    "SYSTEM_PROMPT",
    "TTP_NO_TOOL_RETRY_PROMPT",
    "SchemaCandidate",
    "SchemaValidator",
    "SubmitResultSchemaTool",
    "SubmitTtpTemplateTool",
    "TemplateCandidate",
    "TemplateValidator",
    "ValidatorOutcome",
    "build_agent",
    "build_submission_tools",
    "build_task_message",
    "build_task_prompt",
    "build_ttp_generator_agent",
    "estimate_initial_model_tokens",
    "run_generation_agent",
]
