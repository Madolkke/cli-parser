"""The runtime contract description follows the validator declarations."""

from cli_parser_agent.ttp_generation.agent.prompt import SCHEMA_SYSTEM_PROMPT
from cli_parser_agent.ttp_generation.validation import json_schema


def test_schema_capabilities_share_the_validator_source():
    guidance = json_schema.schema_capabilities_guidance()
    assert guidance in SCHEMA_SYSTEM_PROMPT
    for kind, keywords in json_schema._KEYWORDS_BY_TYPE.items():
        assert kind in guidance
        assert all(key in guidance for key in keywords)
    assert all(key in guidance for key in json_schema._ALWAYS_FORBIDDEN)
    assert "arguments" in guidance
    assert "result_schema" in guidance
