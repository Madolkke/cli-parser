# CLI Parser Agent

Generate a validated [TTP](https://ttp.readthedocs.io/) template and JSON Schema from `1-5` outputs of the same command. Inputs are treated only as data; the project never executes commands.

## Installation

```powershell
uv sync
```

## Environment

```powershell
$env:OPENAI_API_KEY = "..."
$env:OPENAI_MODEL = "..."
$env:OPENAI_BASE_URL = "https://api.deepseek.com" # optional

# Optional complete Laminar debug traces
$env:LMNR_PROJECT_API_KEY = "..."
$env:LMNR_BASE_URL = "http://127.0.0.1" # optional, for self-hosting
$env:LMNR_HTTP_PORT = "8000"             # optional
$env:LMNR_GRPC_PORT = "8001"             # optional
```

## Python API

```python
import asyncio

from cli_parser_agent import GenerationRequest, TtpGenerator


async def main() -> None:
    result = await TtpGenerator.from_env().generate(
        GenerationRequest(
            command_outputs=[
                "Interface  Status\nGi0        up\nGi1        down",
                "Interface  Status\nGi0        down\nGi1        up",
            ],
        ),
    )
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

Edit the configuration constants at the top of `scripts/run_agent_once.py`, then run:

```powershell
uv run python scripts/run_agent_once.py
```

The script loads the configured command-output files and writes the complete result under `.artifacts/agent-once/`. It prints the Laminar trace ID when tracing is enabled and flushes pending spans before exit.

## Documentation

- [Architecture and exact constraints](docs/architecture.md)
- [Agent architecture and runtime walkthrough](docs/agent-architecture-and-runtime.md)
- [Live corpus test plan](docs/live-corpus-test-plan.md)
