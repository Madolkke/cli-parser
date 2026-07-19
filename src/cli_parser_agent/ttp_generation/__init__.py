"""TTP generation use case and framework-independent contracts."""

from .contracts import (
    ArtifactBundle,
    FieldEvidence,
    GenerationMetadata,
    GenerationRequest,
    GenerationResult,
    LastAttempt,
    Metadata,
    SchemaSubmission,
    ValidationIssue,
)
from .generator import TtpGenerator

__all__ = [
    "ArtifactBundle",
    "FieldEvidence",
    "GenerationMetadata",
    "GenerationRequest",
    "GenerationResult",
    "LastAttempt",
    "Metadata",
    "SchemaSubmission",
    "TtpGenerator",
    "ValidationIssue",
]
