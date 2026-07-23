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
    "SchemaSubmission",
    "TtpGenerator",
    "ValidationIssue",
]
