# CLI Parser Agent

Generate a validated [TTP](https://ttp.readthedocs.io/) template and a JSON
Schema from one or more outputs of the same command. Inputs are parsed as data;
the project never executes commands.

## Installation

```powershell
uv sync
```

## Environment

Set the required environment variables:

```powershell
$env:OPENAI_API_KEY = "..."
$env:OPENAI_MODEL = "..."
$env:OPENAI_BASE_URL = "https://api.deepseek.com" # optional

# Optional: enable complete Laminar debug traces
$env:LMNR_PROJECT_API_KEY = "..."
$env:LMNR_BASE_URL = "https://..." # optional, for self-hosted Laminar
# Self-hosted Docker defaults:
$env:LMNR_HTTP_PORT = "8000"
$env:LMNR_GRPC_PORT = "8001"
```

When `LMNR_PROJECT_API_KEY` is set, `TtpGenerator` automatically enables
Laminar tracing for each `generate()` call. A standalone call creates a trace
rooted at `ttp.generate`; when an upstream Agent span is already active,
`ttp.generate` joins that trace as a child. OpenAI-compatible model spans and
Schema/TTP submission tool spans inherit the same trace context.
Rejected TTP submissions include a bounded capture of their actual parse
result, so the model and Laminar transcript can show what the candidate matched.
Command outputs, model replies, evidence, templates, validation feedback, and
results can therefore be uploaded to Laminar. API keys are never added to trace
inputs or metadata, and ordinary application logs remain redacted.

For the standard self-hosted Docker deployment, set `LMNR_BASE_URL` to the host
without a port (for example, `http://127.0.0.1`) and set
`LMNR_HTTP_PORT=8000` plus `LMNR_GRPC_PORT=8001`. The port variables must be
ASCII decimal integers from `1` to `65535`.

The trace ID is available as `result.metadata.laminar_trace_id`. This project
uses only the Laminar Python SDK for tracing; it does not require `lmnr-cli`,
Debugger sessions, or replay.

Applications that need to initialize tracing before constructing a generator
can call `initialize_laminar_from_env()`. It returns `False` when no Laminar Key
is configured and `True` when Laminar is already initialized or initialization
succeeds.

## Python API

```python
import asyncio

from cli_parser_agent import GenerationRequest, TtpGenerator


async def main() -> None:
    generator = TtpGenerator.from_env()
    result = await generator.generate(
        GenerationRequest(
            command_outputs=[
                "Interface  Status\nGi0        up\nGi1        down",
                "Interface  Status\nGi0        down\nGi1        up",
            ],
        ),
    )

    if result.metadata.laminar_trace_id:
        print("Laminar trace:", result.metadata.laminar_trace_id)

    if result.status == "success":
        print(result.artifact.ttp_template)
        print(result.artifact.result_schema)
        print(result.artifact.records)
    else:
        print(result.issues)


if __name__ == "__main__":
    asyncio.run(main())
```

## Zero-argument development run

Edit the configuration constants at the top of
`scripts/run_agent_once.py`, then run it without arguments:

```powershell
uv run python scripts/run_agent_once.py
```

The script reads `OPENAI_API_KEY` or requests it with hidden terminal input.
It loads the configured command-output files and writes the complete result to
`.artifacts/agent-once/<run-id>/result.json`. When Laminar is enabled, it also
prints `laminar_trace_id` and flushes pending spans before the process exits.
A flush failure is reported as a warning and does not replace the generation
exit status.
