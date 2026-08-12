# AGENTS.md

<!-- markdownlint-disable MD013 -->

## 原始目标

> 我想在这个项目中构建一个(未来可能有其他上下游Agent)基于AgentScope的Agent：它的主要目标是根据给定的一条或多条命令行模板，尽可能一次性地生成一份TTP模板，能够解析这些命令行，另外还有相应解析结果的JSON Schema。我想先从目录结构设计开始，请你结合AgentScope 2.0.*的文档，帮我设计一下目录结构，并将原始目标记录在AGENTS.md中。

## 当前解释

- 本项目中的 TTP 是 [Template Text Parser](https://ttp.readthedocs.io/)。
- 一次请求接收 `1-5` 份同一命令的纯命令输出文本，而不是命令文本或命令行模板。调用方保证来源相同，输入不含终端提示符和命令本身；每份非空白文本的 UTF-8 编码不超过 `1 MiB`。
- 一次性生成指上游只调用一次异步 `generate`。Agent 内部可以在受限轮次中通过确定性工具修正产物。
- 生成严格分为两阶段：模型先推断并提交 Draft 2020-12 JSON Schema；第一个通过校验的 Schema 永久冻结；随后持续提交和修正 TTP 模板，并在主动复核有效候选的 capture 后显式结束生成。
- 信息不足时由模型保守推断并写入 `assumptions`。默认保留字符串类型，只有来源证据和安全转换均充分时才使用数字或布尔类型。
- 成功产物包含一个共享 TTP 模板、冻结的 JSON Schema、`assumptions` 和按输入索引一一对应的 `records`。每份完整输入必须恰好解析为一个根 `object`；对象和数组可以嵌套。
- JSON Schema 描述单个 record，不描述 AgentScope `Msg`、输入列表或服务返回包络。

## 架构约束

- AgentScope 版本限制为 `>=2.0.4,<2.1` 并由 `uv.lock` 固定实际版本。
- 对外只提供异步 Python API：`TtpGenerator.generate(GenerationRequest, *, observer=None) -> GenerationResult`。请求、结果和 artifact 仍是框架无关的 Pydantic 2 契约，不得包含 AgentScope 消息、事件或状态对象；可选 `observer` 是显式的完整调试例外，可以同步接收原始 AgentScope `AgentEvent` 与项目级 `CustomEvent`，不得用于改变 Agent 决策或充当业务数据通道。
- 代码按 `ttp_generation/` 垂直功能切片组织。`generator.py` 只保留公共入口与根 Trace，私有 `workflow.py` 编排 Schema、TTP 和最终验收；跨阶段状态位于 `agent/session.py`，AgentScope 构造与工具包装位于同级 `agent/` 模块。领域契约、采样和 `validation/` 不导入 AgentScope。
- 每个请求顺序创建 `ttp_schema_generator` 和 `ttp_template_generator` 两个独立 Agent；两者分别拥有新的 `OpenAIChatModel`、`AgentState` 和 Toolkit，模型对话上下文绝不跨阶段复用。首版不使用长期记忆。
- 两阶段只共享请求级 `GenerationSession`。Schema Agent 的 Toolkit 只注册 `submit_result_schema`；Schema 冻结后结束该 reply，再以冻结 Schema 和重新从全文采样的命令输出启动 TTP Agent，其 Toolkit 固定注册 `submit_ttp_template` 与无参数的 `finish_generation`。rejected Schema、evidence、assumptions、issues、Thinking、ToolCall/ToolResult、零工具提醒和 usage 均不进入 TTP 模型上下文。
- 两个阶段发送给 OpenAI 兼容 HTTP API 的请求都完全省略 `tool_choice`。工具自身仍执行阶段、冻结和预算校验，不从普通 assistant 文本提取产物；middleware 只禁止有损 context compression，不承担阶段工具过滤。
- 运行配置可通过 `TtpGeneratorSettings.thinking_enable` / `reasoning_effort` 或对应的 `CLI_PARSER_MODEL_THINKING_ENABLE` / `CLI_PARSER_MODEL_REASONING_EFFORT` 控制标准 OpenAI 推理参数；开关未设置时保持省略，显式关闭时发送 `reasoning_effort=none`。程序化构造可通过冻结的 `TtpGeneratorSettings.extra_body` 为同一生成器的两个阶段统一提供供应商兼容请求字段；不提供环境变量或单请求覆盖，允许其按 OpenAI Client 语义覆盖标准字段，但递归拒绝凭据型键。项目 metadata、评测摘要和普通日志只记录是否配置及规范化内容的 SHA-256，不记录正文；Laminar 自动模型追踪仍可能捕获实际请求体。
- OpenAI 兼容 HTTP 客户端默认严格校验 TLS 证书；只有显式设置 `CLI_PARSER_INSECURE_SKIP_TLS_VERIFY=1`（也接受 `true`、`yes`、`on`）时，才为受信任内网端点禁用验证。该开关同样用于评测 SQL HTTP 请求；Laminar exporter 改用 HTTP OTLP，Laminar 自托管实例必须使用 `http://` 或安装内部 CA，不能绕过 gRPC TLS 校验。
- TTP 提示要求每个模型回复最多调用一个工具，并在 `submit_ttp_template` 的 ToolResult/records 已进入后续模型上下文后才能调用 `finish_generation`。模型只看到按输入索引排列的完整 records 或固定中文错误；accepted、issues、候选状态和预算等诊断字段仅保留在内部追踪通道。提示必须明确 WORD 匹配一个非空白 token、PHRASE 必须匹配至少两个 token、ORPHRASE 才能兼容一个或多个 token，并要求单行表格返回空对象时首先排查单 token 字段误用 PHRASE。对于标签存在但值为空且右侧有固定分隔符的字段，提示必须明确 WORD、PHRASE 与 ORPHRASE 不能捕获空字符串，并要求使用由右侧分隔符约束的零长度 `re`；不得用 `_start_`、`_end_` 或 `_exact_space_` 修复行内可变空白。表格多捕获表头时，提示要求在真实冻结字段 pipeline 上用 `exclude` 排除表头字面量，或在所有数据行确有稳定值时用 `equal`，不得把 required 字段改成模板字面量或新增全 `ignore` 表头控制 pattern。首版不新增候选轮次 ID 或同轮工具调用拦截，依赖兼容供应商遵守 `parallel_tool_calls=False`。
- 模型完成一轮但没有产生工具调用时，runner 只追加固定的中文提醒并在同一阶段重试；提醒不得引用、摘要或记录模型自由文本。Schema 和 TTP 阶段分别最多重试 `3` 次，允许配置为 `0`；耗尽后返回结构化模型失败。项目不根据供应商异常文本推断工具能力，也不发送 `thinking.type=disabled` 等供应商专用覆盖。
- 默认预算为总时长 `360` 秒、AgentScope `13` 轮、最多 `9` 次 TTP 提交、Schema/TTP 阶段各最多 `3` 次零工具重试；单次隔离解析默认 `20` 秒。限制均通过 `GenerationPolicy` 配置，所有零工具回复和语义重试都计入总轮次与总时长，任一预算先耗尽即终止。达到有效 `max_ttp_submissions` 上限时，该候选仍会校验并返回反馈，但请求随后必须以 `ttp_submission_limit` 失败，即使候选有效；默认上限为 `9`，因此默认最晚可在第 `8` 次提交后调用 `finish_generation` 成功结束。
- Schema 和 TTP 阶段分别从完整输入执行确定性采样，各自使用 `240,000` 字符总预算、按样例均分并保留约 `75%` 头部和 `25%` 尾部。每阶段再按自己的系统提示、任务消息、阶段工具 Schema 和 AgentScope 初始 token 估算独立收紧；TTP 阶段的拟合还计入冻结 Schema。最终 TTP 验收始终使用未经采样的完整输入。
- `submit_ttp_template` 通过校验时只把模板及 records 保存为最新有效候选，不终止 TTP Agent。模型必须根据返回的 records、冻结 Schema 和原始输入复核记录数量、异常空容器、表头/分隔线误捕获、字段粒度和多样例一致性；满意后调用 `finish_generation`，否则继续提交模板。后续无效提交不清除先前有效候选，新的有效提交会替换它；只有存在有效候选且 `finish_generation` 成功调用，TTP 阶段才以成功结束。
- `finish_generation` 成功后，私有 generation workflow 必须在 Agent 外再次执行完整安全检查、全文解析、records 映射和冻结 Schema 校验。模型的主动确认或工具阶段通过不能代替最终验收；未在预算内 finish 时，即使存在有效候选也返回失败，且 finish 后终验失败不重新进入 Agent。
- `LMNR_PROJECT_API_KEY` 非空时自动启用 Laminar Python tracing，可用 `LMNR_BASE_URL` 指向自托管实例；自托管 HTTP/gRPC 端口分别由可选的 `LMNR_HTTP_PORT` / `LMNR_GRPC_PORT` 显式配置。独立调用时 `ttp.generate` 创建 Trace 根，其下分别建立 `schema.phase` 和按需创建的 `ttp.phase`；模型、Schema 提交、TTP 提交与 `finish_generation` TOOL span 继承对应阶段上下文。存在上游 Agent span 时整条生成流程加入同一 Trace。`GenerationMetadata.laminar_trace_id` 用于定位 Trace；未配置 Key 时 tracing 完全禁用，初始化错误直接传播。
- 首版不提供产品 CLI 或 `examples/`，也不预建通用多 Agent/Agent Team 编排、HTTP/A2A/MCP 适配、持久化、部署或消息总线。两个阶段 Agent 只是当前垂直用例内部的固定顺序实现。`evals/ttp_generation/` 是独立的开发期黑盒评测定义，不进入产品包或公共 API。
- Laminar 既可作为可选的完整调试通道，也可由显式评测入口用 `evaluate(...)` 建立 `evaluation → executor → ttp.generate → phase → LLM/TOOL` Trace；不引入 `lmnr-cli`、Debugger session、replay、LLM judge 或 Laminar Dataset。会运行真实模型的短进程开发脚本在结束前 flush，所有 `list` 和 `preflight` 操作不初始化 Laminar 或产生网络请求。
- `testdata/real_command_outputs/` 是固定版本的公开 raw CLI 开发测试语料，不属于产品包、`evals/` 或 `examples/`；除 Linux `ip address show` 与 Cisco IOS `show inventory` 的确定性 TTP 语法回归外，不把完整语料套件接入 pytest。不得把上游解析模板、参考 YAML、mock 数据或 JSON 命令结果一并复制进来。
- `scripts/run_live_corpus.py` 只用于语料 preflight 和人工触发的真实模型闭环，不是产品 CLI，不得改变或绕过公共 `TtpGenerator` API。
- `scripts/run_agent_evaluation.py` 是人工触发的 Laminar 黑盒评测入口：仓库 manifest 物化为内存 `Datapoint`，executor 对每个 trial 只调用一次公共 `generate()` 且不传 observer，evaluator 仅在生成后读取 target。模型、Laminar、预算和产物位置均通过环境变量注入，真实 Key 不得暂存、提交、写入指纹、metadata、span、本地摘要、异常或测试快照。Evaluation/datapoint 创建失败不得调用 Agent；遥测入库不完整不重跑模型。
- 系统化评测必须同时报告结果正确性（case/input 通过率、records/Schema exact 与 precision/recall/F1）、流程漏斗（Schema 冻结、进入 TTP、首个有效候选、finish、最终验收）、可靠性（终止原因、故障域、issue-code、重复 trial）和资源效率（`context.fit`、`agent.round`、`generation.deadline_cleanup`、`final.acceptance`、LLM/TOOL 时延的 p50/p95/p99、tokens、cost、上下文增长和分段覆盖率）。结果按 case/suite/输入形状提供 macro 与 micro 视角；`strict_pass` 只由确定性 `candidate_pass` 决定，Evaluation/Trace/span 完整性与 Trace ID 一致性必须独立报告且不得影响严格正确率；部分分数只用于诊断。
- 需要诊断完整修正链时，开发评测入口可在独立进程、并发 `1` 下显式提升预算；本轮主运行固定使用总时长 `7200` 秒、`32` 个 Agent 轮次、`24` 次 TTP 提交和单次模型超时 `120` 秒。高预算只适用于开发测评，不修改默认 `GenerationPolicy`，每次运行必须保留有效配置指纹和 Laminar Trace。
- HumanEvaluator 仅限显式开发评测入口：在 Laminar 只读 Trace 中评审该 run 产生的全部 Schema/TTP 候选、capture 复核和最终候选，并记录有界的解析边界、字段粒度、可选字段、同一输入内实体一致性、过拟合和可维护性标签；写入时显式区分 `phase=schema|ttp`。它不得进入 `TtpGenerator.generate()`、产品部署或普通 pytest，不修改 Agent 状态、不触发重试、不向模型回灌内容，本地摘要不得保存模板、records、capture、原始输入或模型文本。
- `src/cli_parser_agent/evaluation.py` 只实现测试定义的安全加载、Agent 外终验、严格评分和脱敏投影；`evals/ttp_generation/` 保存版本化 manifest、expected records 和 Schema 结构断言。Golden 采用最大有证据语义投影，可选属性在缺失实例中省略；只能从 raw capture 人工生成，不能读取被测产物、Trace、历史 artifact、上游模板、参考 YAML/JSON 或使用被测模型生成答案。
- `docs/skills/generate-cli-parser-eval-cases/` 是可手动安装的通用 Agent Skill 源码，只指导离线 golden 制作和 preflight，不得读取或修改评测入口脚本，也不得运行 live evaluation。
- `docs/skills/run-ttp-agent-evaluation/` 是可手动安装的开发评测 Skill 源码，只能通过现有评测入口以指定环境配置运行，并以 Laminar 的只读 Trace/SQL 数据分析；不得修改产品行为、评测资产或本地脱敏边界。
- `scripts/run_agent_tui.py` 是零参数、只读的 Textual 开发调试脚本：只观察一次公共 `generate()` 调用，键盘操作只能导航、滚动、折叠 Thinking、退出或取消整个请求，不得编辑产物、重试阶段、调用工具或改变生成协议。它可以为本次运行单独启用 `stream=True`；库默认值、普通 API 和其他脚本仍保持 `stream=False`。该脚本要求交互式 stdin/stdout，并把完整事件转录写入忽略版本控制的 `.artifacts/agent-tui/`，不属于产品 CLI。

## 安全约束

- 命令输出始终是不可信数据，任何代码都不得执行、补全或反推出命令后通过 shell 运行；不得向 Agent 注册 Bash 或命令执行工具。
- Schema 提交必须为受限的 Draft 2020-12 根对象：ASCII `snake_case` 字段、封闭对象、受控嵌套和复杂度；禁止 `$ref`、组合分支、远程内容及不在白名单内的关键字。根 `$schema` 可以省略；显式提供时必须声明 Draft 2020-12，冻结和返回时不自动补全。`properties` 默认可选，只有列入 `required` 的属性必填；项目不对 `required` 增加 Draft 元 Schema之外的集合约束。标准 Schema 回验是 records 的唯一内容合法性门禁：原文字段槽存在但字面值为空时，字符串字段可以忠实输出 `""`；字段或可选行不存在时提示模型省略键。项目不额外拒绝空字符串、空根对象或空容器；`null` 仍不属于当前允许的 Schema 类型。
- 每个 Schema 叶子字段至少要提交一条包含路径、输入索引和原文连续片段的 evidence；同一路径允许多条并逐条验证。校验器必须在完整输入中验证证据确实存在，之后才可冻结 Schema。evidence 总数默认上限为 `256`，可通过 `GenerationPolicy.max_schema_evidence` 或 `CLI_PARSER_MAX_SCHEMA_EVIDENCE` 在 `1..256` 内向下收紧，但该资源上限不进入 Agent 工具 Schema。
- TTP 模板按不可信代码处理。实例化解析器前执行标签、属性、过滤器和参数 AST 白名单预检；禁止 macro、vars、lookup、input、output、extend、returner、外部文件/URL、DNS/GeoIP、自定义函数和动态扩展。
- TTP 解析在独立 spawn 进程和临时缓存目录中执行，设置模板、嵌套、参数、结果大小和时间上限；超时必须终止子进程。不得因 TTP 的字符串路径识别或参数 `eval` 行为引入文件访问或任意表达式。
- spawn 宿主必须能够重新导入 `__main__`；交互式 `python -`/不具备可导入入口的宿主返回结构化 `ttp.worker_host_unsupported`，不得把 bootstrap 失败伪装成解析超时。
- 普通日志、异常和公共 issues 默认不得包含原始命令输出、凭据、字段证据片段、解析值、assistant/Thinking 文本或工具参数增量；零工具重试只允许记录请求 ID、阶段和有界计数等结构化事实。请求内的 `submit_ttp_template` 模型结果直接返回完整 records，受 `GenerationPolicy.max_parse_result_bytes` 约束（默认上限 `8 MiB`）；无 records 时追加固定中文错误。模型不可见的 accepted、issues、候选状态、预算和有界 capture 继续保留在 Laminar、observer/TUI 与评测诊断 payload 中，但不得写入失败的公共 `GenerationResult`、`last_attempt` 或普通日志。完整调试有两个显式例外：启用 Laminar 后，Trace 可以包含命令输出、模型回复、evidence、模板、捕获结果和验证反馈；显式传入 observer 后，本地 TUI artifact 可以包含完整上下文快照、命令输出、Thinking/文本、工具参数与结果、模板、capture 和验收反馈。两种通道都只能只读观察，不得回灌模型上下文，任何模型或 Laminar API Key、credential/client 对象和未处理异常正文都不得进入事件、Trace 或本地 artifact。
- 黑盒评测本地 `.artifacts/agent-evals/<UTC-run-id>/summary.json` 只能保存配置指纹、Git 状态、case/trial 状态、数值指标、安全 issue code、Evaluation/Trace ID 与 URL；模板、records、capture、target、原始输入和模型文本只允许存在于显式 Laminar 通道。对象键顺序忽略，数组顺序、标量类型、缺失与 `null` 严格区分。

## 质量要求

- 确定性单元测试覆盖公共契约、采样、Schema 元模式/白名单/证据、TTP 安全预检与隔离执行，以及最终验收规则。
- Agent 集成测试使用真实 OpenAI 兼容模型，不创建 Fake/Mock LLM；必须以 `live` marker 和环境凭据显式启用，不能成为普通单元测试的隐式依赖。
- 测试覆盖 Schema 修正与冻结、Schema 接受后的安全暂停、TTP 首轮上下文无 Schema 阶段消息、TTP 候选保留与 capture 复核、显式 `finish_generation` 终止协议、零工具中文提醒及分阶段重试上限、达到有效模板提交上限后的严格失败、共享预算耗尽、所有 records 与输入一一对应，以及嵌套结构的 Schema 回验；真实模型修正测试由 validator 确定性拒绝首个有效候选，避免依赖随机的首次失败。
- 官方黑盒评测和真实语料将每份 raw 作为独立单输入 case；运行时公共 API 仍兼容 `1-5` 输入，但当前测试资产不测量跨样本共享模板泛化。公开真实语料 manifest 固定为 `31` 个 case、`31` 份文本；无凭据时必须可以独立执行 preflight，验证文件编码、大小、终端噪声、凭据模式和 SHA-256，不得产生模型请求。
- 真实语料闭环独立于 pytest：先要求 smoke 的 `5/5` 单输入 case（`5` 份文本）通过，再用同一结果目录 resume 完整代表集并达到 `31/31`；只有当前 `prompt_version` 的成功 case 才能被跳过，每个成功 case 都要在 Agent 外使用全文重新验收。
- Laminar 单测必须覆盖可选初始化、幂等行为、独立/继承 Trace、根与 TOOL span 的正常/失败/异常/取消生命周期、trace ID 契约和短进程 flush；语料 `list`/`preflight` 必须证明不触发 tracing 初始化或网络访问。
- 评测系统单测必须覆盖 manifest 严格解析、路径逃逸、哈希、非空 target、Schema 断言闭合、严格 records 比较、漏行/表头/空数组/类型差异、逐输入诊断、严格正确率与遥测完整性解耦、结果与 Trace ID 一致性、遥测延迟和 Key 排除。版本化 smoke 定义固定为 `12` 个单输入 case、`12` 份输入；低歧义 baseline 定义固定为 `3` 个单输入 case、`3` 份输入；live canary 只运行一个单输入 trial，严格失败仍可作为系统验收，但必须完整记录评分和 Trace 且不自动重试。高预算诊断、重复 trial 和 HumanEvaluator 评审均属于独立开发评测步骤，不进入普通 pytest。
- observer 与 TUI 单测必须覆盖缺省行为不变、事件顺序和请求隔离、observer 异常隔离、阶段上下文快照、零工具丢弃标记、外部/内部取消区分、完整 JSONL 转录和 Key 排除；Textual `run_test()` 还需覆盖上下导航、Thinking 折叠、详情滚动、自动跟随与完成后退出。
- 首版交付前至少完成一次真实模型的端到端闭环；普通测试仍必须离线、稳定且不依赖模型。

## 后续 Agent 工作规则

- 当前目录与职责见 [docs/architecture.md](docs/architecture.md)。不要创建无用途的空目录或 `.gitkeep`。
- 真实语料的来源、运行方式与验收标准见 [docs/live-corpus-test-plan.md](docs/live-corpus-test-plan.md)；新增或替换文件时同步更新 manifest、SHA-256、第三方来源说明和该文档中的计数。
- 重要契约、阶段协议或目录边界变化时，同步更新本文件和架构文档。
- 提示词若从 Python 模块迁移为 Markdown 资源，使用 `importlib.resources` 加载并在构建配置中声明 package data。
- 只有出现第二个真实产品用例或消费者后，才提取共享产品模块或新增适配与编排目录；现有 `evaluation.py`、`evals/` 和评测 Skill 始终是开发测试边界。
