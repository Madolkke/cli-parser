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
- 对外只提供框架无关的异步 Python API：`TtpGenerator.generate(GenerationRequest) -> GenerationResult`。公共契约可使用 Pydantic 2，但不得暴露 AgentScope 消息、事件或状态对象。
- 代码按 `ttp_generation/` 垂直功能切片组织。`generator.py` 只保留公共入口与根 Trace，私有 `workflow.py` 编排 Schema、TTP 和最终验收；跨阶段状态位于 `agent/session.py`，AgentScope 构造与工具包装位于同级 `agent/` 模块。领域契约、采样和 `validation/` 不导入 AgentScope。
- 每个请求顺序创建 `ttp_schema_generator` 和 `ttp_template_generator` 两个独立 Agent；两者分别拥有新的 `OpenAIChatModel`、`AgentState` 和 Toolkit，模型对话上下文绝不跨阶段复用。首版不使用长期记忆。
- 两阶段只共享请求级 `GenerationSession`。Schema Agent 的 Toolkit 只注册 `submit_result_schema`；Schema 冻结后结束该 reply，再以冻结 Schema 和重新从全文采样的命令输出启动 TTP Agent，其 Toolkit 固定注册 `submit_ttp_template` 与无参数的 `finish_generation`。rejected Schema、evidence、assumptions、issues、Thinking、ToolCall/ToolResult、零工具提醒和 usage 均不进入 TTP 模型上下文。
- 两个阶段发送给 OpenAI 兼容 HTTP API 的请求都完全省略 `tool_choice`。工具自身仍执行阶段、冻结和预算校验，不从普通 assistant 文本提取产物；middleware 只禁止有损 context compression，不承担阶段工具过滤。
- TTP 提示要求每个模型回复最多调用一个工具，并在 `submit_ttp_template` 的 ToolResult/capture 已进入后续模型上下文后才能调用 `finish_generation`。首版不新增候选轮次 ID 或同轮工具调用拦截，依赖兼容供应商遵守 `parallel_tool_calls=False`。
- 模型完成一轮但没有产生工具调用时，runner 只追加固定的中文提醒并在同一阶段重试；提醒不得引用、摘要或记录模型自由文本。Schema 和 TTP 阶段分别最多重试 `3` 次，允许配置为 `0`；耗尽后返回结构化模型失败。项目不根据供应商异常文本推断工具能力，也不发送 `thinking.type=disabled` 等供应商专用覆盖。
- 默认预算为总时长 `360` 秒、AgentScope `13` 轮、最多 `9` 次 TTP 提交、Schema/TTP 阶段各最多 `3` 次零工具重试；单次隔离解析默认 `20` 秒。限制均通过 `GenerationPolicy` 配置，所有零工具回复和语义重试都计入总轮次与总时长，任一预算先耗尽即终止。达到第 `9` 次 TTP 提交时，该候选仍会校验并返回反馈，但请求随后必须以 `ttp_submission_limit` 失败，即使候选有效；最晚可在第 `8` 次提交后调用 `finish_generation` 成功结束。
- Schema 和 TTP 阶段分别从完整输入执行确定性采样，各自使用 `240,000` 字符总预算、按样例均分并保留约 `75%` 头部和 `25%` 尾部。每阶段再按自己的系统提示、任务消息、阶段工具 Schema 和 AgentScope 初始 token 估算独立收紧；TTP 阶段的拟合还计入冻结 Schema。最终 TTP 验收始终使用未经采样的完整输入。
- `submit_ttp_template` 通过校验时只把模板及 records 保存为最新有效候选，不终止 TTP Agent。模型必须结合 capture 与 issues 复核记录数量、异常空容器、表头/分隔线误捕获、字段粒度和多样例一致性；满意后调用 `finish_generation`，否则继续提交模板。后续无效提交不清除先前有效候选，新的有效提交会替换它；只有存在有效候选且 `finish_generation` 成功调用，TTP 阶段才以成功结束。
- `finish_generation` 成功后，私有 generation workflow 必须在 Agent 外再次执行完整安全检查、全文解析、records 映射和冻结 Schema 校验。模型的主动确认或工具阶段通过不能代替最终验收；未在预算内 finish 时，即使存在有效候选也返回失败，且 finish 后终验失败不重新进入 Agent。
- `LMNR_PROJECT_API_KEY` 非空时自动启用 Laminar Python tracing，可用 `LMNR_BASE_URL` 指向自托管实例；自托管 HTTP/gRPC 端口分别由可选的 `LMNR_HTTP_PORT` / `LMNR_GRPC_PORT` 显式配置。独立调用时 `ttp.generate` 创建 Trace 根，其下分别建立 `schema.phase` 和按需创建的 `ttp.phase`；模型、Schema 提交、TTP 提交与 `finish_generation` TOOL span 继承对应阶段上下文。存在上游 Agent span 时整条生成流程加入同一 Trace。`GenerationMetadata.laminar_trace_id` 用于定位 Trace；未配置 Key 时 tracing 完全禁用，初始化错误直接传播。
- 首版不提供 CLI、`evals/` 或 `examples/`，也不预建通用多 Agent/Agent Team 编排、HTTP/A2A/MCP 适配、持久化、部署或消息总线。两个阶段 Agent 只是当前垂直用例内部的固定顺序实现。
- Laminar 仅作为可选的完整调试通道，不引入 `lmnr-cli`、Debugger session 或 replay；两个短进程开发脚本在运行结束前 flush，语料 `list` 和 `preflight` 不初始化 Laminar 或产生网络请求。
- `testdata/real_command_outputs/` 是固定版本的公开 raw CLI 开发测试语料，不属于产品包、`evals/` 或 `examples/`；除 Linux `ip address show` 与 Cisco IOS `show inventory` 的确定性 TTP 语法回归外，不把完整语料套件接入 pytest。不得把上游解析模板、参考 YAML、mock 数据或 JSON 命令结果一并复制进来。
- `scripts/run_live_corpus.py` 只用于语料 preflight 和人工触发的真实模型闭环，不是产品 CLI，不得改变或绕过公共 `TtpGenerator` API。

## 安全约束

- 命令输出始终是不可信数据，任何代码都不得执行、补全或反推出命令后通过 shell 运行；不得向 Agent 注册 Bash 或命令执行工具。
- Schema 提交必须为受限的 Draft 2020-12 根对象：ASCII `snake_case` 字段、封闭对象、受控嵌套和复杂度；禁止 `$ref`、组合分支、远程内容及不在白名单内的关键字。
- 每个 Schema 叶子字段都要提交路径、输入索引和原文连续片段。校验器必须在完整输入中验证证据确实存在，之后才可冻结 Schema。
- TTP 模板按不可信代码处理。实例化解析器前执行标签、属性、过滤器和参数 AST 白名单预检；禁止 macro、vars、lookup、input、output、extend、returner、外部文件/URL、DNS/GeoIP、自定义函数和动态扩展。
- TTP 解析在独立 spawn 进程和临时缓存目录中执行，设置模板、嵌套、参数、结果大小和时间上限；超时必须终止子进程。不得因 TTP 的字符串路径识别或参数 `eval` 行为引入文件访问或任意表达式。
- spawn 宿主必须能够重新导入 `__main__`；交互式 `python -`/不具备可导入入口的宿主返回结构化 `ttp.worker_host_unsupported`，不得把 bootstrap 失败伪装成解析超时。
- 普通日志、异常和公共 issues 默认不得包含原始命令输出、凭据、字段证据片段、解析值、assistant/Thinking 文本或工具参数增量；零工具重试只允许记录请求 ID、阶段和有界计数等结构化事实。请求内的 `submit_ttp_template` 反馈可以包含最多 `32 KiB` 的实际捕获结果，供 TTP Agent 在本阶段复核或修正候选，但不得写入失败的公共 `GenerationResult`、`last_attempt` 或普通日志。显式设置 `LMNR_PROJECT_API_KEY` 是完整调试采集的另一例外：Laminar Trace 可以包含命令输出、模型回复、evidence、模板、捕获结果和验证反馈，但任何模型或 Laminar API Key 都不得进入 Trace；Trace 只用于观察，不得作为跨阶段模型上下文。

## 质量要求

- 确定性单元测试覆盖公共契约、采样、Schema 元模式/白名单/证据、TTP 安全预检与隔离执行，以及最终验收规则。
- Agent 集成测试使用真实 OpenAI 兼容模型，不创建 Fake/Mock LLM；必须以 `live` marker 和环境凭据显式启用，不能成为普通单元测试的隐式依赖。
- 测试覆盖 Schema 修正与冻结、Schema 接受后的安全暂停、TTP 首轮上下文无 Schema 阶段消息、TTP 候选保留与 capture 复核、显式 `finish_generation` 终止协议、零工具中文提醒及分阶段重试上限、第 `9` 次提交严格失败、共享预算耗尽、所有 records 与输入一一对应，以及嵌套结构的 Schema 回验；真实模型修正测试由 validator 确定性拒绝首个有效候选，避免依赖随机的首次失败。
- 公开真实语料 manifest 固定为 `13` 个 case、`40` 份文本；无凭据时必须可以独立执行 preflight，验证文件编码、大小、终端噪声、凭据模式和 SHA-256，不得产生模型请求。
- 真实语料闭环独立于 pytest：先要求 smoke 的 `5/5` case（`12` 份文本）通过，再用同一结果目录 resume 完整代表集并达到 `13/13`；只有当前 `prompt_version` 的成功 case 才能被跳过，每个成功 case 都要在 Agent 外使用全文重新验收。
- Laminar 单测必须覆盖可选初始化、幂等行为、独立/继承 Trace、根与 TOOL span 的正常/失败/异常/取消生命周期、trace ID 契约和短进程 flush；语料 `list`/`preflight` 必须证明不触发 tracing 初始化或网络访问。
- 首版交付前至少完成一次真实模型的端到端闭环；普通测试仍必须离线、稳定且不依赖模型。

## 后续 Agent 工作规则

- 当前目录与职责见 [docs/architecture.md](docs/architecture.md)。不要创建无用途的空目录或 `.gitkeep`。
- 真实语料的来源、运行方式与验收标准见 [docs/live-corpus-test-plan.md](docs/live-corpus-test-plan.md)；新增或替换文件时同步更新 manifest、SHA-256、第三方来源说明和该文档中的计数。
- 重要契约、阶段协议或目录边界变化时，同步更新本文件和架构文档。
- 提示词若从 Python 模块迁移为 Markdown 资源，使用 `importlib.resources` 加载并在构建配置中声明 package data。
- 只有出现第二个真实用例或消费者后，才提取共享模块或新增适配、编排和评估目录。
