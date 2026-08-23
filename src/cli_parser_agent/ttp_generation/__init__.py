"""TTP generation use case and framework-independent contracts."""

from .contracts import (
    ArtifactBundle,
    FieldEvidence,
    GenerationMetadata,
    GenerationRequest,
    GenerationResult,
    LastAttempt,
    Metadata,
    SchemaProposal,
    SchemaProposalResult,
    SchemaSubmission,
    TemplateRequest,
    ValidationIssue,
)
from .generator import TtpGenerator
from .progress import ProgressObserver

__all__ = [
    "ArtifactBundle",
    "FieldEvidence",
    "GenerationMetadata",
    "GenerationRequest",
    "GenerationResult",
    "LastAttempt",
    "Metadata",
    "ProgressObserver",
    "SchemaProposal",
    "SchemaProposalResult",
    "SchemaSubmission",
    "TemplateRequest",
    "TtpGenerator",
    "ValidationIssue",
]
