# CLI Parser Agent 首版架构

<!-- markdownlint-disable MD013 -->

- 状态：首版实现完成，已完成单例 live 闭环；完整语料验收待完成
- 日期：2026-07-21
- AgentScope 基线：2.0.4

## 1. 目标与边界

项目提供一个可组合的异步 Python Agent：调用方提交 `1-5` 份同一命令的纯输出，Agent 在一次外部请求内生成一份可解析所有完整输入的安全 TTP 模板，以及描述每份解析结果的 JSON Schema。

首版只实现这一条生成用例。它不提供 CLI、HTTP 服务、多 Agent 编排、持久化、`evals/` 或 `examples/`。公共 API 不假定调用方是人、CLI 或 Agent，因此未来上下游 Agent 可以直接复用同一契约。

确定的业务语义如下：

- 每份输入是非空白的命令输出数据，UTF-8 编码不超过 `1 MiB`；项目不执行命令，也不验证命令身份。
- 一个请求生成一个共享 TTP 模板，而不是为每份输入分别生成模板。
- Schema 使用 Draft 2020-12，描述一个根 object record，允许嵌套对象和数组。
- TTP 对每份完整输入必须恰好产生一个根 object；`records[i]` 对应 `command_outputs[i]`。
- 模型自行保守处理单样例或变量位置不明确的情况，并把不可验证的推断记录在 `assumptions`。

## 2. AgentScope 设计

### 2.1 框架边界与请求隔离

AgentScope 的 `Agent.reply_stream(...)` 是异步事件接口，但 `Msg` 和 Event 只用于内部推理。应用层输入输出由 Pydantic 2 契约定义，不从 Agent 的自由文本回复提取产物。

每个 `generate` 调用创建新的 Agent、`AgentState`、Toolkit 和 generation session。会话对象是唯一候选产物通道，保存冻结 Schema、提交次数、最近模板、结构化问题和通过工具校验的 records；不同请求之间不共享状态。

### 2.2 两阶段单工具协议与语义重试

阶段适配层根据请求状态限制每次模型调用可见的工具集合，但发送给 OpenAI 兼容 HTTP API 的请求完全省略 `tool_choice`：

1. Schema 未冻结时，只暴露 `submit_result_schema`。无效提交返回结构化问题，模型可修正后重提。
2. 第一个通过元模式、白名单和字段证据校验的 Schema 被深拷贝并永久冻结。
3. Schema 冻结后，只暴露 `submit_ttp_template`。该工具只接收模板文本，不能替换 Schema。
4. 如果一轮模型调用正常完成但没有产生工具调用，runner 不解析其 assistant 文本，只追加一条固定中文提醒并在当前阶段重试。提醒不引用或摘要模型回复；Schema 和 TTP 阶段分别受独立重试上限约束。
5. 模板在所有完整输入上通过校验，或任一轮次、零工具重试、模板提交及总时长预算耗尽后，runner 通过 AgentScope 支持的中断清理路径结束当前 reply，移除清理阶段新增的消息和 usage，再返回结构化结果。

这种协议避免同时生成两份相互漂移的产物，也不依赖自由文本 JSON 提取。工具仍在执行边界复核阶段、冻结状态和预算；零工具回复只触发有界语义提醒，不能成为候选产物。

### 2.3 模型与预算

首版使用 AgentScope 2.0.* 的 OpenAI 兼容模型，必需环境变量为 `OPENAI_API_KEY` 和 `OPENAI_MODEL`，`OPENAI_BASE_URL` 可选。默认模型参数为 `stream=False`、`temperature=0`、`parallel_tool_calls=False`、`max_tokens=8192`、`context_size=128000`。

模型构造层不识别供应商主机名，不发送 `thinking.type=disabled` 或其他供应商专用覆盖，也不根据异常正文推断模型是否支持工具。HTTP 请求携带当前阶段唯一的工具 Schema，但省略 `tool_choice`；正常完成却未调用工具属于可观测的协议行为，而不是供应商能力结论。

默认执行限制是总时长 `300` 秒、AgentScope `12` 轮、最多 `8` 次模板提交、Schema 阶段最多 `3` 次零工具重试、TTP 阶段最多 `3` 次零工具重试，以及每次 TTP 隔离解析 `20` 秒；最先达到的限制终止请求。两个零工具上限可通过 `GenerationPolicy.max_schema_no_tool_retries` / `max_ttp_no_tool_retries` 程序化设置，或分别由 `CLI_PARSER_MAX_SCHEMA_NO_TOOL_RETRIES` / `CLI_PARSER_MAX_TTP_NO_TOOL_RETRIES` 从环境读取；均允许设为 `0`。零工具回复及其重试计入总轮次和总时长。

模型侧命令输出总预算为 `240,000` 字符，按输入均分，超限样例在完整行边界保留约 `75%` 头部和 `25%` 尾部；随后还按最终提示序列化长度和 AgentScope 初始 token 估算收紧，避免 JSON 控制字符/多字节文本膨胀超过上下文。请求级 middleware 禁止 AgentScope 用摘要替换这些证据；若后续轮次确实超出模型上下文，返回结构化模型失败。确定性验收始终读取全文。

### 2.4 可选 Laminar 调试 Trace

`LMNR_PROJECT_API_KEY` 非空时，`TtpGenerator` 自动初始化 Laminar，并且只启用 OpenAI instrumentation；`LMNR_BASE_URL` 可选用于自托管实例，自托管 HTTP/gRPC 端口分别通过 `LMNR_HTTP_PORT` / `LMNR_GRPC_PORT` 显式传给 SDK。端口必须是 `1..65535` 的 ASCII 十进制整数。未配置 Key 时 tracing 完全禁用，初始化错误作为配置错误直接传播，已由调用方初始化的 Laminar 不会被覆盖。

独立调用 `generate` 时，`ttp.generate` 创建 Trace 根；若调用方已有上游 Agent span，则 `ttp.generate` 继承当前上下文并加入同一 Trace，不覆盖上游 Trace metadata。AgentScope 的 OpenAI 兼容请求形成 LLM 子 span，`submit_result_schema` 和 `submit_ttp_template` 形成 TOOL 子 span。由本生成器创建的 Trace 记录请求 ID、模型、prompt 版本、输入数量、提交次数、终止原因和状态；`GenerationMetadata.laminar_trace_id` 允许调用方定位同一次运行。根 span 与 TOOL span 采用显式生命周期管理，使正常返回、结构化失败、异常和协作式取消都能结束并导出；强制杀进程仍不保证上传。

Laminar 是显式启用的完整调试通道，可以采集命令输出、模型回复、Thinking、evidence、模板、解析结果和验证反馈。TTP 候选只要完成隔离解析，即使无匹配或 Schema 不一致，也会在请求内工具反馈中返回最多 `32 KiB` 的完整 records 或结构化 preview；该 capture 同时进入 TOOL span，但不进入失败的公共结果。模型与 Laminar API Key 始终排除；普通日志、异常和公共 issues 仍遵守脱敏约束。首版不引入 `lmnr-cli`、Debugger session 或 replay。

## 3. 目录与职责

```text
.
├── AGENTS.md
├── LICENSE
├── README.md
├── pyproject.toml
├── uv.lock
├── .env.example
├── docs/
│   ├── agent-architecture-and-runtime.md
│   ├── architecture.md
│   └── live-corpus-test-plan.md
├── scripts/
│   ├── run_agent_once.py
│   └── run_live_corpus.py
├── src/
│   └── cli_parser_agent/
│       ├── __init__.py
│       ├── config.py
│       ├── observability.py
│       └── ttp_generation/
│           ├── __init__.py
│           ├── contracts.py
│           ├── generator.py
│           ├── sampling.py
│           ├── agent/
│           │   ├── builder.py
│           │   ├── middleware.py
│           │   ├── prompt.py
│           │   ├── runner.py
│           │   └── tools.py
│           └── validation/
│               ├── __init__.py
│               ├── capture.py
│               ├── json_schema.py
│               └── ttp.py
├── testdata/
│   └── real_command_outputs/
│       ├── corpus.json
│       ├── README.md
│       ├── licenses/
│       ├── ntc_templates/
│       └── ttp_templates/
└── tests/
    ├── conftest.py
    ├── unit/
    └── integration/
```

| 模块 | 职责 | 允许依赖 AgentScope |
| --- | --- | --- |
| `config.py` | OpenAI 兼容配置与独立的执行/安全策略 | 否 |
| `observability.py` | 可选 Laminar 幂等初始化与 trace 边界辅助函数 | 否 |
| `contracts.py` | 请求、成功/失败结果、artifact、issue、metadata 和 Schema evidence | 否 |
| `sampling.py` | 确定性模型上下文采样，不改变全文验收输入；generator 另做最终序列化/token fitting | 否 |
| `generator.py` | 唯一公开用例；编排 Agent、预算、异常映射和 Agent 外终验 | 否；通过切片内窄接口调用 Agent 适配层 |
| `agent/builder.py` | 创建请求独占的模型、Agent、Toolkit 和 `AgentState` | 是 |
| `agent/middleware.py` | 按 Schema/TTP 阶段过滤为唯一可见工具，并禁止有损上下文压缩；不设置 `tool_choice` | 是 |
| `agent/prompt.py` | 版本化纯文本提示词，把输入明确标记为不可信数据 | 否 |
| `agent/runner.py` | 消费事件、统计模型轮次与分阶段零工具回复、发起固定中文提醒，并在终止工具结果后安全中断和清理事件流 | 是 |
| `agent/tools.py` | 两个非并发提交工具与请求级 generation session | 是 |
| `validation/capture.py` | 将无效 TTP 候选的实际 records 编码为不超过 `32 KiB` 的完整反馈或结构化 preview | 否 |
| `validation/json_schema.py` | Schema 元模式、安全子集、复杂度、字段证据和 record 校验 | 否 |
| `validation/ttp.py` | TTP 声明子集预检、参数 AST 检查、spawn 隔离解析、Schema/来源终验 | 否 |
| `scripts/run_agent_once.py` | 使用源码常量运行一个人工选择的真实模型请求，写入完整开发产物，打印 trace ID 并在退出前 flush | 否；只调用公共 API |
| `scripts/run_live_corpus.py` | 开发期公开语料 preflight、真实模型运行、独立终验与 resume；仅 `run` 路径在退出前 flush | 否；只调用公共 API 和确定性 validation |
| `testdata/real_command_outputs/` | 固定版本的第三方 raw CLI 输出、manifest、来源和许可证 | 不适用；不进入公共包，只有两个确定性 parser 回归由 pytest 直接读取 |

所有领域逻辑留在 `ttp_generation` 垂直切片中。即使 `validation/` 不依赖 AgentScope，也不提升到项目顶层；只有第二个真实用例需要复用时才提取共享模块。

## 4. 公共契约

```text
GenerationRequest
  command_outputs: list[str]          # 1-5，每项非空白且 <= 1 MiB UTF-8

ArtifactBundle
  ttp_template: str
  result_schema: dict                 # Draft 2020-12，根 type=object
  records: list[dict]                 # 与 command_outputs 按索引一一对应
  assumptions: list[str]

GenerationResult
  status: success | failed
  artifact: ArtifactBundle | None
  issues: list[ValidationIssue]
  metadata: GenerationMetadata
  last_attempt: LastAttempt | None    # 失败候选，validated 固定为 false

GenerationMetadata
  laminar_trace_id: str | None         # 未启用 tracing 时为 None
  schema_no_tool_responses: int       # Schema 阶段正常完成但没有工具调用的次数
  ttp_no_tool_responses: int          # TTP 阶段正常完成但没有工具调用的次数
  schema_no_tool_retries: int         # 实际发起的 Schema 中文提醒重试次数
  ttp_no_tool_retries: int            # 实际发起的 TTP 中文提醒重试次数
```

`TtpGenerator.from_env()` 从环境创建模型配置；也可使用 `TtpGeneratorSettings` 和独立的 `GenerationPolicy` 程序化构造。普通构造从进程环境初始化可选 Laminar tracing，`from_env(environ=...)` 则使用传入 mapping；公共辅助函数 `initialize_laminar_from_env(environ=None) -> bool` 可供其他入口显式初始化，缺少 Key 时返回 `False`，已初始化或成功初始化时返回 `True`。请求格式和缺失配置由 Pydantic/配置异常报告；模型请求、零工具协议、超时、预算和生成失败统一返回 `status="failed"` 的结构化结果；`asyncio.CancelledError` 原样传播。

成功结果必须有 artifact，且 records 数量等于输入数量。失败结果不得携带 artifact，必须至少有一个 error issue；未通过验收的模板只能进入 `last_attempt`，不能标记为有效产物。

## 5. 数据流与最终验收

```text
GenerationRequest
    │
    ├─ Pydantic 数量、空白与 UTF-8 字节上限检查
    ├─ 确定性采样（仅供模型） ─────────────┐
    ├─ 保存完整输入（仅供工具和终验）      │
    │                                      ▼
    ├─ 新建 Agent + AgentState + generation session
    │      ├─ Schema 阶段仅暴露 submit_result_schema
    │      ├─ 元模式/白名单/全文字段证据校验
    │      ├─ 首个有效 Schema 永久冻结
    │      ├─ TTP 阶段仅暴露 submit_ttp_template
    │      ├─ 零工具回复触发有界固定中文提醒
    │      └─ 对所有全文解析并根据结构化问题修正
    │
    ├─ Agent 外重新执行确定性最终验收
    │      ├─ TTP 实例化前安全预检
    │      ├─ 隔离进程逐份全文 parse(one=True)
    │      ├─ 每份恰好一个根 object 且索引不变
    │      ├─ 所有 records 符合冻结 Schema
    │      └─ 标量值具有输入来源
    │
    └─ GenerationResult(success | failed)
```

Schema 只允许项目支持的 Draft 2020-12 子集：ASCII `snake_case` 字段，所有对象完整声明 `required` 并设置 `additionalProperties: false`，最大 `64 KiB`、深度 `16`、属性总数 `256`。禁止 `$ref`、组合分支、远程内容和未列入白名单的关键字。每个叶子路径必须提交一条真实存在于指定完整输入中的连续原文证据。

TTP 实例化前只允许嵌套 `<group>`、受控 group 属性、内置模式、行控制、纯字符串条件、受限正则/聚合和安全数值/IP 转换。特殊变量只允许裸 `ignore`、`ignore(BUILTIN)` 或单个字符串正则参数的 `ignore("regex")`，并禁止后续 pipeline。显式拒绝 macro、vars、lookup、input、output、extend、returner、DNS/GeoIP、文件/URL、自定义函数，以及参数 AST 中的属性访问、下标、运算、推导式和嵌套调用。

由于 TTP 0.10.1 会对参数求值并可能把字符串识别为路径，模板和输入在安全检查后以不可成为路径的形式传入，清除模板路径环境变量，并使用临时 `TTPCACHEFOLDER`。每次解析在独立 spawn 进程中执行；超时立即终止，不调用 shell。无法重新导入 `__main__` 的交互式宿主返回 `ttp.worker_host_unsupported`，不会等待完整解析超时。

## 6. 测试策略

- 确定性单元测试覆盖输入边界、采样、Schema 安全子集与字段证据、TTP 标签/参数攻击、解析超时、嵌套记录、一一映射、Schema 回验和失败候选标记。
- pytest 中的稳定测试不隐式访问网络或模型；仅 Linux `ip address show` 与 Cisco IOS `show inventory` 两组测试直接读取固定 raw 语料，用真实 TTP 0.10.1 回归 `ignore(...)` 子语言，其余 corpus 仍由独立 runner 管理。
- Agent 集成测试只使用真实 OpenAI 兼容模型，不创建 Fake/Mock LLM。它们以 `live` marker、凭据和显式开关隔离，覆盖成功闭环、轮次预算和结构化失败；修正测试由 validator 确定性拒绝首个有效 Schema 和 TTP，并要求模型根据工具反馈重提，避免把随机失败当作断言前提。零工具检测、固定中文提醒、分阶段重试上限、metadata 计数和内容脱敏由确定性事件级单元测试覆盖，不依赖真实模型随机省略工具。
- Laminar 单测覆盖无 Key、可选 Base URL、自托管端口、幂等初始化、独立/继承 Trace、success/failed/exception/cancelled 生命周期、TOOL span、trace ID 契约和短进程 flush；未启用时原有行为保持不变。
- 普通测试离线运行确定性模块；首版验收仍需至少执行一次真实模型端到端闭环。

此外，仓库保留独立于 pytest 的公开真实命令输出语料：`networktocode/ntc-templates` `v9.2.0` 和 `dmulyalin/ttp_templates` `0.5.9` 中选取的 `13` 个 case、`40` 份 raw 文本。`corpus.json` 固定文件顺序、suite、来源版本和 SHA-256；不复制上游 YAML、解析模板、mock 数据或 JSON 输出。两份第三方许可证与版本说明随语料保存，本项目自身使用根目录 Apache-2.0 `LICENSE`。

`scripts/run_live_corpus.py` 提供三种开发操作：`list` 查看选择结果；`preflight` 在无模型或 Laminar 凭据、无网络请求的条件下检查数量、UTF-8、大小、终端噪声、凭据模式和哈希；`run` 通过公共 API 逐 case 调用真实模型，并把结果写入忽略版本控制的 `.artifacts/live-corpus/`，结束时 flush 已初始化的 Laminar。flush 失败只写有界警告，不替换生成退出码。成功结果还要在 Agent 外重新执行安全检查、全文解析、records 顺序/内容和冻结 Schema 验证。

真实语料验收先运行固定 smoke suite（`5` 个 case、`12` 份文本）并达到 `5/5`，再通过 `--resume` 扩展到完整 suite 并达到 `13/13`。Resume 只复用语料哈希和 `prompt_version` 均与当前运行一致、且再次通过独立全文验收的成功 case。完整命令、失败分类、隐私说明和恢复流程见 [真实命令输出语料测试计划](live-corpus-test-plan.md)。公开夹具可能已由上游整理，不能声称是未经处理的生产采集；未来加入私有数据前必须脱敏。

## 7. 暂缓事项

长期记忆、多 Agent 编排、Agent Team、HTTP/A2A/MCP 适配、产品 CLI、持久化、部署、消息总线、生产级监控与告警、Laminar CLI/Debugger/replay、`evals/` 和 `examples/` 均不属于首版。`scripts/run_live_corpus.py` 是独立开发测试工具，不扩大产品边界；生成产物通过 API 返回，语料 runner 的测试记录只写入 `.artifacts/`，不写入源码目录。只有出现明确消费者、第二个用例或统计质量目标后，才新增相应边界。

## 8. 官方依据

- [AgentScope 2.0.4 文档](https://docs.agentscope.io/versions/2.0.4/en)
- [Agent](https://docs.agentscope.io/versions/2.0.4/en/building-blocks/agent)
- [Message & Event](https://docs.agentscope.io/versions/2.0.4/en/building-blocks/message-and-event)
- [Model 与结构化输出](https://docs.agentscope.io/versions/2.0.4/en/building-blocks/model)
- [Tool 与 Toolkit](https://docs.agentscope.io/versions/2.0.4/en/building-blocks/tool)
- [Middleware](https://docs.agentscope.io/versions/2.0.4/en/building-blocks/middleware)
- [AgentScope v2.0.4 源码标签](https://github.com/agentscope-ai/agentscope/tree/v2.0.4)
