"""Deterministic validation boundary for generated schemas and TTP templates."""

from .capture import (
    MAX_CAPTURE_BYTES,
    MAX_SCALAR_VALUE_CHARS,
    ParseCapture,
    build_parse_capture,
)
from .json_schema import (
    validate_records_against_schema,
    validate_result_schema,
)
from .ttp import (
    MAX_TTP_TEMPLATE_BYTES,
    MAX_TTP_TEST_INPUT_BYTES,
    TtpParseResult,
    TtpValidationResult,
    inspect_ttp_template,
    parse_ttp_template,
    validate_ttp_template,
)

__all__ = [
    "MAX_CAPTURE_BYTES",
    "MAX_SCALAR_VALUE_CHARS",
    "MAX_TTP_TEMPLATE_BYTES",
    "MAX_TTP_TEST_INPUT_BYTES",
    "ParseCapture",
    "TtpParseResult",
    "TtpValidationResult",
    "build_parse_capture",
    "inspect_ttp_template",
    "parse_ttp_template",
    "validate_records_against_schema",
    "validate_result_schema",
    "validate_ttp_template",
]
