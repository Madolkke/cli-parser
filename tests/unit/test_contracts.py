from __future__ import annotations

import ssl

import pytest
from pydantic import ValidationError

from cli_parser_agent.config import (
    INSECURE_SKIP_TLS_VERIFY_ENV,
    GenerationPolicy,
    TtpGeneratorSettings,
    tls_ssl_context,
    tls_verification_enabled,
)
from cli_parser_agent.ttp_generation.contracts import (
    ArtifactBundle,
    FieldEvidence,
    GenerationMetadata,
    GenerationRequest,
    GenerationResult,
    LastAttempt,
    SchemaSubmission,
    ValidationIssue,
)


def test_generation_request_accepts_one_to_five_nonempty_outputs() -> None:
    assert GenerationRequest(command_outputs=["ok"]).command_outputs == ["ok"]
    assert len(GenerationRequest(command_outputs=["x"] * 5).command_outputs) == 5


@pytest.mark.parametrize("outputs", [[], [""], [" \t\r\n"], ["x"] * 6])
def test_generation_request_rejects_invalid_cardinality_or_empty_text(
    outputs: list[str],
) -> None:
    with pytest.raises(ValidationError):
        GenerationRequest(command_outputs=outputs)


def test_generation_request_applies_utf8_byte_limit() -> None:
    GenerationRequest(command_outputs=["\u754c" * (1024 * 1024 // 3)])
    with pytest.raises(ValidationError, match="exceeds"):
        GenerationRequest(command_outputs=["\u754c" * (1024 * 1024 // 3 + 1)])


def test_generation_request_rejects_unencodable_surrogate() -> None:
    with pytest.raises(ValidationError, match="UTF-8"):
        GenerationRequest(command_outputs=["\ud800"])


def test_schema_payloads_require_an_object_root() -> None:
    evidence = FieldEvidence(path="/interfaces/*/name", output_index=0, excerpt="eth0")
    submission = SchemaSubmission(
        result_schema={"type": "object"},
        evidence=[evidence],
    )
    assert submission.evidence == [evidence]

    with pytest.raises(ValidationError, match="root type"):
        ArtifactBundle(
            ttp_template="{{ value }}",
            result_schema={"type": "array"},
            records=[{}],
        )


@pytest.mark.parametrize(
    "path",
    ["interfaces/*/name", "/interfaces/-/name", "/*/value"],
)
def test_field_evidence_rejects_ambiguous_paths(path: str) -> None:
    with pytest.raises(ValidationError):
        FieldEvidence(path=path, output_index=0, excerpt="value")


def test_field_evidence_allows_a_scalar_array_leaf() -> None:
    evidence = FieldEvidence(path="/values/*", output_index=0, excerpt="one")

    assert evidence.path == "/values/*"


def _metadata(count: int = 1) -> GenerationMetadata:
    return GenerationMetadata(model_name="model", command_output_count=count)


def _artifact(record_count: int = 1) -> ArtifactBundle:
    return ArtifactBundle(
        ttp_template="{{ value }}",
        result_schema={"type": "object"},
        records=[{} for _ in range(record_count)],
    )


def test_success_result_enforces_validated_artifact_and_record_mapping() -> None:
    result = GenerationResult(
        status="success",
        artifact=_artifact(2),
        metadata=_metadata(2),
    )
    assert result.status == "success"

    with pytest.raises(ValidationError, match="one-to-one"):
        GenerationResult(
            status="success",
            artifact=_artifact(1),
            metadata=_metadata(2),
        )

    with pytest.raises(ValidationError, match="error issues"):
        GenerationResult(
            status="success",
            artifact=_artifact(),
            metadata=_metadata(),
            issues=[ValidationIssue(code="parse.failed", message="failed")],
        )


def test_failed_result_cannot_publish_artifact_and_retains_attempt() -> None:
    result = GenerationResult(
        status="failed",
        artifact=None,
        metadata=_metadata(),
        issues=[ValidationIssue(code="budget.exhausted", message="budget exhausted")],
        last_attempt=LastAttempt(ttp_template="{{ value }}"),
    )
    assert result.last_attempt is not None
    assert result.last_attempt.validated is False

    with pytest.raises(ValidationError, match="validated artifact"):
        GenerationResult(
            status="failed",
            artifact=_artifact(),
            metadata=_metadata(),
            issues=[ValidationIssue(code="generation.failed", message="failed")],
        )

    with pytest.raises(ValidationError):
        LastAttempt(ttp_template="candidate", validated=True)


def test_failed_result_requires_an_error_issue() -> None:
    with pytest.raises(ValidationError, match="error issue"):
        GenerationResult(
            status="failed",
            metadata=_metadata(),
            issues=[
                ValidationIssue(
                    code="candidate.warning",
                    message="warning",
                    severity="warning",
                ),
            ],
        )


def test_generation_metadata_supports_an_optional_laminar_trace_id() -> None:
    assert _metadata().laminar_trace_id is None

    metadata = GenerationMetadata(
        model_name="model",
        command_output_count=1,
        laminar_trace_id="01234567-89ab-cdef-0123-456789abcdef",
    )

    assert metadata.laminar_trace_id == "01234567-89ab-cdef-0123-456789abcdef"
    assert (
        metadata.model_dump(mode="json")["laminar_trace_id"]
        == "01234567-89ab-cdef-0123-456789abcdef"
    )

    with pytest.raises(ValidationError):
        GenerationMetadata(
            model_name="model",
            command_output_count=1,
            laminar_trace_id="",
        )


def test_generation_metadata_tracks_isolated_phase_counts() -> None:
    metadata = GenerationMetadata(
        model_name="model",
        command_output_count=2,
        input_char_count=1_000,
        schema_sampled_char_count=700,
        ttp_sampled_char_count=600,
        agent_rounds=5,
        schema_agent_rounds=2,
        ttp_agent_rounds=3,
    )

    assert metadata.agent_rounds == 5
    assert metadata.schema_agent_rounds == 2
    assert metadata.ttp_agent_rounds == 3
    assert metadata.schema_sampled_char_count == 700
    assert metadata.ttp_sampled_char_count == 600
    assert "sampled_char_count" not in metadata.model_dump(mode="json")


@pytest.mark.parametrize(
    "values",
    [
        {"input_char_count": 10, "schema_sampled_char_count": 11},
        {"input_char_count": 10, "ttp_sampled_char_count": 11},
        {"agent_rounds": 2, "schema_agent_rounds": 1, "ttp_agent_rounds": 0},
    ],
)
def test_generation_metadata_rejects_inconsistent_phase_counts(
    values: dict[str, int],
) -> None:
    with pytest.raises(ValidationError):
        GenerationMetadata(
            model_name="model",
            command_output_count=1,
            **values,
        )


def test_settings_from_env_requires_credentials_and_uses_model_defaults() -> None:
    settings = TtpGeneratorSettings.from_env(
        {
            "OPENAI_API_KEY": "secret",
            "OPENAI_MODEL": "test-model",
            "OPENAI_BASE_URL": "https://example.test/v1/",
        },
    )
    assert settings.api_key.get_secret_value() == "secret"
    assert settings.model_name == "test-model"
    assert settings.base_url == "https://example.test/v1"
    assert settings.stream is False
    assert settings.temperature == 0
    assert settings.parallel_tool_calls is False
    assert settings.thinking_enable is None
    assert settings.reasoning_effort is None
    assert settings.max_tokens == 8192
    assert settings.context_size == 128000
    assert settings.model_max_retries == 2
    assert settings.model_timeout_seconds == 60
    assert settings.verify_tls is True

    with pytest.raises(ValidationError):
        TtpGeneratorSettings.from_env({})


def test_settings_from_env_reads_all_model_overrides() -> None:
    settings = TtpGeneratorSettings.from_env(
        {
            "OPENAI_API_KEY": "secret",
            "OPENAI_MODEL": "test-model",
            "CLI_PARSER_MODEL_STREAM": "true",
            "CLI_PARSER_MODEL_TEMPERATURE": "0.7",
            "CLI_PARSER_MODEL_PARALLEL_TOOL_CALLS": "true",
            "CLI_PARSER_MODEL_THINKING_ENABLE": "true",
            "CLI_PARSER_MODEL_REASONING_EFFORT": "high",
            "CLI_PARSER_MODEL_MAX_TOKENS": "4096",
            "CLI_PARSER_MODEL_CONTEXT_SIZE": "8192",
            "CLI_PARSER_MODEL_MAX_RETRIES": "4",
            "CLI_PARSER_MODEL_TIMEOUT_SECONDS": "90",
        },
    )

    assert settings.stream is True
    assert settings.temperature == 0.7
    assert settings.parallel_tool_calls is True
    assert settings.thinking_enable is True
    assert settings.reasoning_effort == "high"
    assert settings.max_tokens == 4096
    assert settings.context_size == 8192
    assert settings.model_max_retries == 4
    assert settings.model_timeout_seconds == 90


@pytest.mark.parametrize("value", ["invalid", "HIGH"])
def test_settings_reject_invalid_reasoning_effort(value: str) -> None:
    with pytest.raises(ValidationError):
        TtpGeneratorSettings(
            api_key="secret",
            model_name="test-model",
            reasoning_effort=value,
        )


@pytest.mark.parametrize("value", ["maybe", "2"])
def test_settings_reject_invalid_thinking_enable(value: str) -> None:
    with pytest.raises(ValidationError):
        TtpGeneratorSettings.from_env(
            {
                "OPENAI_API_KEY": "secret",
                "OPENAI_MODEL": "test-model",
                "CLI_PARSER_MODEL_THINKING_ENABLE": value,
            },
        )


@pytest.mark.parametrize("value", ["1", "true", "yes", "on"])
def test_insecure_tls_environment_opt_in_disables_verification(value: str) -> None:
    settings = TtpGeneratorSettings.from_env(
        {
            "OPENAI_API_KEY": "secret",
            "OPENAI_MODEL": "test-model",
            INSECURE_SKIP_TLS_VERIFY_ENV: value,
        },
    )

    assert settings.verify_tls is False
    context = tls_ssl_context(verify_tls=settings.verify_tls)
    assert context is not None
    assert context.check_hostname is False
    assert context.verify_mode is ssl.CERT_NONE


@pytest.mark.parametrize("value", ["0", "false", "no", "off", " "])
def test_tls_environment_keeps_verification_enabled(value: str) -> None:
    assert tls_verification_enabled({INSECURE_SKIP_TLS_VERIFY_ENV: value}) is True


def test_tls_environment_rejects_ambiguous_values() -> None:
    with pytest.raises(ValueError, match=INSECURE_SKIP_TLS_VERIFY_ENV):
        tls_verification_enabled({INSECURE_SKIP_TLS_VERIFY_ENV: "disable"})


def test_generation_policy_has_bounded_defaults() -> None:
    policy = GenerationPolicy()
    assert policy.total_timeout_seconds == 360
    assert policy.max_agent_rounds == 13
    assert policy.max_ttp_submissions == 9
    assert policy.model_input_char_budget == 240_000
    assert policy.ttp_validation_timeout_seconds == 20
    assert policy.max_schema_evidence == 256

    with pytest.raises(ValidationError, match="cannot exceed"):
        GenerationPolicy(total_timeout_seconds=1, ttp_validation_timeout_seconds=2)


def test_generation_policy_from_env_reads_all_policy_overrides() -> None:
    policy = GenerationPolicy.from_env(
        {
            "CLI_PARSER_GENERATION_TIMEOUT_SECONDS": "600",
            "CLI_PARSER_MAX_AGENT_ITERS": "16",
            "CLI_PARSER_MAX_TEMPLATE_SUBMISSIONS": "10",
            "CLI_PARSER_MAX_SCHEMA_NO_TOOL_RETRIES": "2",
            "CLI_PARSER_MAX_TTP_NO_TOOL_RETRIES": "1",
            "CLI_PARSER_TTP_VALIDATION_TIMEOUT_SECONDS": "30",
            "CLI_PARSER_MODEL_INPUT_CHAR_BUDGET": "120000",
            "CLI_PARSER_MAX_TTP_TEMPLATE_BYTES": "32000",
            "CLI_PARSER_MAX_TTP_GROUP_DEPTH": "8",
            "CLI_PARSER_MAX_TTP_REGEX_CHARS": "1024",
            "CLI_PARSER_MAX_TTP_ARGUMENT_CHARS": "2048",
            "CLI_PARSER_MAX_PARSE_RESULT_BYTES": "4194304",
            "CLI_PARSER_MAX_SCHEMA_BYTES": "32000",
            "CLI_PARSER_MAX_SCHEMA_DEPTH": "8",
            "CLI_PARSER_MAX_SCHEMA_PROPERTIES": "128",
            "CLI_PARSER_MAX_SCHEMA_EVIDENCE": "64",
            "CLI_PARSER_MAX_EVIDENCE_EXCERPT_CHARS": "2048",
        },
    )
    assert policy.total_timeout_seconds == 600
    assert policy.max_agent_rounds == 16
    assert policy.max_ttp_submissions == 10
    assert policy.max_schema_no_tool_retries == 2
    assert policy.max_ttp_no_tool_retries == 1
    assert policy.ttp_validation_timeout_seconds == 30
    assert policy.model_input_char_budget == 120_000
    assert policy.max_ttp_template_bytes == 32_000
    assert policy.max_ttp_group_depth == 8
    assert policy.max_ttp_regex_chars == 1_024
    assert policy.max_ttp_argument_chars == 2_048
    assert policy.max_parse_result_bytes == 4_194_304
    assert policy.max_schema_bytes == 32_000
    assert policy.max_schema_depth == 8
    assert policy.max_schema_properties == 128
    assert policy.max_schema_evidence == 64
    assert policy.max_evidence_excerpt_chars == 2_048

    with pytest.raises(ValidationError):
        GenerationPolicy.from_env({"CLI_PARSER_MAX_AGENT_ITERS": "not-an-int"})

    for value in ("0", "257", "not-an-int"):
        with pytest.raises(ValidationError):
            GenerationPolicy.from_env(
                {"CLI_PARSER_MAX_SCHEMA_EVIDENCE": value},
            )


@pytest.mark.parametrize(
    ("field", "unsafe_value"),
    [
        ("model_input_char_budget", 240_001),
        ("max_ttp_template_bytes", 64 * 1024 + 1),
        ("max_ttp_group_depth", 17),
        ("max_ttp_regex_chars", 2_049),
        ("max_ttp_argument_chars", 4_097),
        ("max_parse_result_bytes", 8 * 1024 * 1024 + 1),
        ("max_schema_bytes", 64 * 1024 + 1),
        ("max_schema_depth", 17),
        ("max_schema_properties", 257),
        ("max_schema_evidence", 257),
        ("max_evidence_excerpt_chars", 4_097),
    ],
)
def test_generation_policy_can_tighten_but_not_raise_safety_ceilings(
    field: str,
    unsafe_value: int,
) -> None:
    with pytest.raises(ValidationError):
        GenerationPolicy.model_validate({field: unsafe_value})


@pytest.mark.parametrize("value", [float("nan"), float("inf"), -float("inf")])
def test_settings_and_policy_reject_non_finite_numbers(value: float) -> None:
    with pytest.raises(ValidationError):
        GenerationPolicy(total_timeout_seconds=value)
    with pytest.raises(ValidationError):
        TtpGeneratorSettings(
            api_key="secret",
            model_name="model",
            model_timeout_seconds=value,
        )
