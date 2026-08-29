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

During the TTP phase, the model receives one separately labelled parse-result block
per command output, with an explicit `input_index`. The public result keeps its
`artifact.records` list and its one-record-per-input mapping unchanged.

### Proposing a schema for review

`propose_schema()` runs only the Schema phase and returns the frozen proposal
so you can review or edit the field names
before a template is generated:

```python
from cli_parser_agent import GenerationRequest, TtpGenerator

proposal = await TtpGenerator.from_env().propose_schema(
    GenerationRequest(command_outputs=["Interface  Status\nGi0        up"]),
)
if proposal.status == "success":
    print(proposal.proposal.result_schema)
```

It returns a `SchemaProposalResult` rather than a `GenerationResult`: a
successful proposal has no template and no records, so it cannot satisfy
`ArtifactBundle`. Pair it with `generate_from_schema()` below to get a
propose → review → generate workflow.

`SchemaSubmission`, `SchemaProposal`, and `ArtifactBundle` do not contain an
`assumptions` field. This is a breaking contract change: payloads from older
versions that still include the field are rejected by the Pydantic contracts.
Existing WebUI run files are not migrated; they remain readable as local raw
history and can still be used as the source of a Schema rerun.

### Running the TTP phase alone

`generate_from_schema()` freezes a caller-supplied result schema and runs only the
TTP phase, returning the same `GenerationResult`. Use it to pin a known-good schema
so template generation can be verified on its own:

```python
from cli_parser_agent import TemplateRequest, TtpGenerator

result = await TtpGenerator.from_env().generate_from_schema(
    TemplateRequest(
        command_outputs=["Interface  Status\nGi0        up"],
        result_schema={
            "type": "object",
            "properties": {
                "interfaces": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "name": {"type": "string"},
                            "status": {"type": "string"},
                        },
                        "required": ["name", "status"],
                        "additionalProperties": False,
                    },
                },
            },
            "required": ["interfaces"],
            "additionalProperties": False,
        },
    ),
)
```

The schema must satisfy the same closed Draft 2020-12 subset the Schema phase
produces. The Schema phase and caller-supplied mode use the same schema-only
validation; every other check — TTP allowlist, isolated parsing, and record
re-validation against the schema — is unchanged.
ASCII `snake_case` Python keywords such as `as`, `class`, and `for` are valid
field names and are preserved in TTP records. The scalar property name `ignore`
is reserved by TTP and is rejected during schema validation; object and array
containers named `ignore` remain valid.

## Zero-argument development run

设置 `CLI_PARSER_ONCE_INPUT_FILES`（以当前平台路径分隔符分开的 `1-5` 个
严格 UTF-8 输出文件）和模型环境变量后运行：

```powershell
uv run --env-file .env python scripts/run_agent_once.py
```

The script loads the configured command-output files and writes the complete result under `.artifacts/agent-once/`. It prints the Laminar trace ID when tracing is enabled and flushes pending spans before exit.

To run only the TTP phase against a schema file, set `CLI_PARSER_ONCE_INPUT_FILES`
and `CLI_PARSER_TTP_ONCE_SCHEMA_FILE` (one JSON schema document), then run:

```powershell
uv run --env-file .env python scripts/run_ttp_phase_once.py
```

Results are written under `.artifacts/ttp-phase-once/`.

For repeatable TTP evaluation, every case is an independent four-part test set
under `evals/test_sets/<case-id>/`: `inputs/001.txt` through `005.txt`,
`schema.json`, `template.ttp`, and `expected.json`. The root manifest only
indexes cases, suites, tags, and SHA-256 values. The standard template is an
offline executable baseline; the Agent is scored only against the standard
Schema and expected records.

```powershell
uv run python scripts/run_test_sets.py list --manifest evals/test_sets/manifest.json
uv run python scripts/run_test_sets.py preflight --manifest evals/test_sets/manifest.json
uv run python scripts/run_test_sets.py run --manifest evals/test_sets/manifest.json --mode baseline --suite semantic-pilot
uv run --env-file .env python scripts/run_test_sets.py run --manifest evals/test_sets/manifest.json --mode ttp-only --suite semantic-pilot --trials 1 --concurrency 1
```

`list`, `preflight`, and `baseline` are offline. `ttp-only` calls only
`generate_from_schema()` and writes per-trial results under
`.artifacts/test-set-evaluation/`. The evaluation entry point does not run the
full two-stage Schema Agent; Schema quality is checked by the canonical Schema
and its deterministic preflight. See [四件套评测](docs/ttp-template-evaluation.md).

当前 `evals/test_sets/` 测试数据已清空，等待重新导入经过人工核对的标准四件套。
重新提供数据后，必须先完成 preflight 和 baseline，再加入对应 suite 并进行 Agent 评分。

## Local WebUI

设置模型环境变量后启动本地界面：

```powershell
uv run --env-file .env python scripts/run_webui.py
```

默认服务于 `http://127.0.0.1:8080`，可用 `CLI_PARSER_WEBUI_HOST`、
`CLI_PARSER_WEBUI_PORT` 和 `CLI_PARSER_WEBUI_DATA_ROOT` 覆盖。

界面提供两条路径：

- **完整生成** — 等价于命令行的两阶段流程。
- **先提案 Schema** — 先产出 Schema 供你确认或编辑，再据此生成模板。字段命名
  直接决定最终 records 的键名，这一步的人工介入可以避免模型自造命名带来的偏差。

生成在后台执行，进度经 SSE 实时推送，可随时取消。每次运行保存在被 Git 忽略的
`data/runs/<UTC 时间戳>/` 下（`meta.json`、`inputs.json`、`schema.json`、
`result.json`、`events.jsonl`、`config.json`），历史列表就是目录扫描，删除即删目录。
已结束的任务若有已保存 Schema，或成功结果中有冻结 Schema，可在详情页创建独立的
“以此 Schema 重新生成”任务。它只运行 TTP 阶段，复制来源输入与 Schema，在 metadata
中记录来源任务；来源任务的结果与事件记录不会被覆盖。

新建任务和 Schema 重新生成都可以展开“运行参数”面板，覆盖本次使用的模型配置和
`GenerationPolicy`。空白项继承 WebUI 启动时从 `.env` 读取的基线；参数只在任务启动前
生效。`extra_body` 继续只从环境配置读取，`parallel_tool_calls` 固定为 `false`。
实际生效配置保存到该运行的 `config.json`，用于复现；在本地单用户约束下，用户明确提供
的 API Key 也会以明文写入这个 Git 忽略文件。历史接口、SSE、普通日志和事件投影只返回
脱敏配置，不返回 Key。

WebUI 的 SSE 使用浏览器默认 `message` 事件，业务事件类型由 JSON 正文的 `type`
字段分发；断线可通过 `Last-Event-ID` 或 `after_sequence` 继续重放。Thinking、
模型文本和工具增量在服务端按 50ms 或 4 KiB 合并后再分配 sequence、落盘和广播，
因此文本完整且顺序可重建，但不保留供应商逐 token 的边界。

同一时刻只允许一次生成在跑。这是单用户本地工具：只绑回环地址、无鉴权、无并发
隔离，不是部署形态。

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
