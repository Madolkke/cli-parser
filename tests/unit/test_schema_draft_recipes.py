"""Independent example diagnostics retained after the v47 experiment."""

import json
import re
from copy import deepcopy

from cli_parser_agent.ttp_generation.agent.schema_draft_prompt import (
    SCHEMA_DRAFT_SYSTEM_PROMPT,
)
from cli_parser_agent.ttp_generation.schema_draft import compile_schema_draft
from cli_parser_agent.ttp_generation.schema_draft_sources import prepare_draft_sources
from cli_parser_agent.ttp_generation.validation import validate_records_against_schema


def test_actual_prompt_example_compiles_and_describes_multiple_inputs():
    text = re.search(
        r"<draft_example_input>\n(.*?)</draft_example_input>",
        SCHEMA_DRAFT_SYSTEM_PROMPT,
        re.S,
    ).group(1)
    arguments = json.loads(
        re.search(
            r"<draft_example_arguments_json>\n(.*?)</draft_example_arguments_json>",
            SCHEMA_DRAFT_SYSTEM_PROMPT,
            re.S,
        ).group(1)
    )
    outcome = compile_schema_draft(
        arguments["draft"], prepare_draft_sources([text], originals=[text])
    )
    assert outcome.accepted, outcome.issues
    schema = outcome.schema
    assert set(schema["properties"]) == {"batch", "assets", "total"}
    assert schema["required"] == ["batch", "assets", "total"]
    entity = schema["properties"]["assets"]["items"]
    assert set(entity["properties"]) == {"asset", "result", "checks", "tail"}
    assert entity["required"] == ["asset"]
    check = entity["properties"]["checks"]["items"]
    assert set(check["properties"]) == {"check", "outcome"}
    assert check["required"] == ["check", "outcome"]
    assert all(
        node["additionalProperties"] is False for node in (schema, entity, check)
    )
    records = [
        {
            "batch": "winter",
            "assets": [
                {
                    "asset": "Cedar",
                    "result": "ready",
                    "checks": [
                        {"check": "voltage", "outcome": "!pending"},
                        {"check": "status", "outcome": ""},
                    ],
                    "tail": "one",
                },
                {"asset": "Birch"},
                {
                    "asset": "Elm",
                    "result": "!hold",
                    "checks": [{"check": "route", "outcome": "pass"}],
                    "tail": "three",
                },
                {"asset": "Aspen", "result": "", "tail": "four"},
            ],
            "total": 4,
        },
        {
            "batch": "summer",
            "assets": [{"asset": "Spruce", "result": "ready"}],
            "total": 1,
        },
    ]
    assert validate_records_against_schema(records, schema) == []
    misplaced = deepcopy(records[:1])
    misplaced[0]["checks"] = misplaced[0]["assets"][0].pop("checks")
    assert validate_records_against_schema(misplaced, schema)
    missing_summary = deepcopy(records[:1])
    del missing_summary[0]["total"]
    assert validate_records_against_schema(missing_summary, schema)
    assert "submit_result_schema" not in SCHEMA_DRAFT_SYSTEM_PROMPT
    assert "result_schema" not in SCHEMA_DRAFT_SYSTEM_PROMPT
    assert "SchemaPlan" not in SCHEMA_DRAFT_SYSTEM_PROMPT
