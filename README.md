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

`generate()` also accepts an optional keyword-only `observer` that synchronously
receives AgentScope events and project progress events for the current request. This
is a complete-debugging interface rather than a stable business-result contract;
normal callers should omit it. The observer must remain fast and non-blocking. Its
recommended implementation is `queue.put_nowait(event)`, with rendering and artifact
writing handled by a separate consumer.

## Zero-argument development run

Edit the configuration constants at the top of `scripts/run_agent_once.py`, then run:

```powershell
uv run python scripts/run_agent_once.py
```

The script loads the configured command-output files and writes the complete result under `.artifacts/agent-once/`. It prints the Laminar trace ID when tracing is enabled and flushes pending spans before exit.

## Read-only Textual TUI

Edit the configuration constants at the top of `scripts/run_agent_tui.py`, then run it
from an interactive terminal:

```powershell
uv run python scripts/run_agent_tui.py
```

The TUI observes one `generate()` call without changing its prompts, tools, decisions,
configured policy, or result. It enables streaming only for this development run and
shows the phase timeline, model Thinking/text, tool calls and results, Schema, TTP,
capture, issues, and final validation status.

Keyboard controls:

- `Up` / `Down`: select the previous or next timeline block.
- `Space`: collapse or expand the selected Thinking block.
- `PageUp` / `PageDown`: scroll the selected block's details.
- `End`: resume following the newest event after navigating upward.
- `Ctrl+C`: cancel an in-progress generation and wait for cleanup.
- `Enter`: exit only after generation, artifact writing, and Laminar flush finish.

The complete UTF-8 event transcript is written to
`.artifacts/agent-tui/<run-id>/events.jsonl`; the run status and optional
`GenerationResult` are written to `result.json`, together with the script version,
timestamps, model, input-file metadata, transcript path, and bounded failure type.
These ignored local artifacts may
contain complete command outputs, model Thinking/text, tool arguments, templates,
capture, and validation feedback. Model and Laminar API keys are always excluded.
The script refuses to start without interactive stdin and stdout. It is a read-only
development tool, not a product CLI.

Exit codes are `0` for a successful generation, `1` for generation/TUI/artifact
failure, `2` for configuration or non-interactive-terminal errors, and `130` when
the user cancels an in-progress run.

## Documentation

- [Architecture and exact constraints](docs/architecture.md)
- [Agent architecture and runtime walkthrough](docs/agent-architecture-and-runtime.md)
- [Live corpus test plan](docs/live-corpus-test-plan.md)
