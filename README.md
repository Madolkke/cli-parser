# CLI Parser Agent

Generate a validated [TTP](https://ttp.readthedocs.io/) template and JSON Schema from command output. The runtime remains compatible with `1-5` outputs of the same command, while the official evaluation assets use one raw output per case. Inputs are treated only as data; the project never executes commands.

## Installation

```powershell
uv sync
```

## Environment

复制 [`.env.example`](.env.example) 为私有 `.env`，设置所需值后通过 `uv`
注入：

```powershell
uv run --env-file .env python scripts/run_agent_once.py
```

该示例列出了全部可配置环境变量及默认值/语义，包括模型请求参数、生成
预算、安全上限、Laminar 连接和各开发入口的输入/产物位置。普通 Python
调用也可直接由父进程导出的环境变量读取这些设置。

`CLI_PARSER_INSECURE_SKIP_TLS_VERIFY=1` is an explicit compatibility switch for
an OpenAI-compatible HTTPS endpoint with an untrusted internal certificate. It
defaults to certificate validation and accepts `1`, `true`, `yes`, or `on` to
disable it. The switch also applies to the evaluation runner's Laminar SQL HTTP
requests; for Laminar telemetry, use an `http://` self-hosted `LMNR_BASE_URL`
when the internal deployment does not have a trusted certificate.

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

设置 `CLI_PARSER_ONCE_INPUT_FILES`（以当前平台路径分隔符分开的 `1-5` 个
严格 UTF-8 输出文件）和模型环境变量后运行：

```powershell
uv run --env-file .env python scripts/run_agent_once.py
```

The script loads the configured command-output files and writes the complete result under `.artifacts/agent-once/`. It prints the Laminar trace ID when tracing is enabled and flushes pending spans before exit.

## Read-only Textual TUI

设置 `CLI_PARSER_TUI_INPUT_FILES` 和模型环境变量后，从交互式终端运行：

```powershell
uv run --env-file .env python scripts/run_agent_tui.py
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
