"""AgentScope adapter for the request-scoped TTP generator."""

from .builder import (
    build_agent,
    build_schema_task_message,
    build_ttp_task_message,
    estimate_initial_model_tokens,
)
from .middleware import LosslessContextMiddleware
from .prompt import (
    PROMPT_VERSION,
    SCHEMA_NO_TOOL_RETRY_PROMPT,
    SCHEMA_SYSTEM_PROMPT,
    TTP_NO_TOOL_RETRY_PROMPT,
    TTP_SYSTEM_PROMPT,
    build_schema_task_prompt,
    build_ttp_task_prompt,
)
from .runner import AgentRunOutcome, run_generation_phase
from .session import (
    GenerationPhase,
    GenerationSession,
    SchemaCandidate,
    SchemaValidator,
    TemplateCandidate,
    TemplateValidator,
    ValidatorOutcome,
    ValidatorOutcomeAttributes,
    ValidatorOutcomeLike,
)
from .tools import (
    FINISH_GENERATION_TOOL_NAME,
    SUBMIT_SCHEMA_TOOL_NAME,
    SUBMIT_TEMPLATE_TOOL_NAME,
    FinishGenerationTool,
    SubmitResultSchemaTool,
    SubmitTtpTemplateTool,
    build_submission_tools,
)

__all__ = [
    "GenerationPhase",
    "GenerationSession",
    "FinishGenerationTool",
    "FINISH_GENERATION_TOOL_NAME",
    "LosslessContextMiddleware",
    "AgentRunOutcome",
    "PROMPT_VERSION",
    "SCHEMA_NO_TOOL_RETRY_PROMPT",
    "SUBMIT_SCHEMA_TOOL_NAME",
    "SUBMIT_TEMPLATE_TOOL_NAME",
    "SCHEMA_SYSTEM_PROMPT",
    "TTP_NO_TOOL_RETRY_PROMPT",
    "TTP_SYSTEM_PROMPT",
    "SchemaCandidate",
    "SchemaValidator",
    "SubmitResultSchemaTool",
    "SubmitTtpTemplateTool",
    "TemplateCandidate",
    "TemplateValidator",
    "ValidatorOutcome",
    "ValidatorOutcomeAttributes",
    "ValidatorOutcomeLike",
    "build_agent",
    "build_schema_task_message",
    "build_schema_task_prompt",
    "build_submission_tools",
    "build_ttp_task_message",
    "build_ttp_task_prompt",
    "estimate_initial_model_tokens",
    "run_generation_phase",
]
