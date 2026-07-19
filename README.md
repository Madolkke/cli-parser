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
```

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
`.artifacts/agent-once/<run-id>/result.json`.
