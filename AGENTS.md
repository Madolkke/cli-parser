# AGENTS.md

<!-- markdownlint-disable MD013 -->

## 原始目标

> 我想在这个项目中构建一个（未来可能有其他上下游 Agent）基于 AgentScope 的 Agent：它的主要目标是根据给定的一条或多条命令行模板，尽可能一次性地生成一份 TTP 模板，能够解析这些命令行，另外还有相应解析结果的 JSON Schema。我想先从目录结构设计开始，请你结合 AgentScope 2.0.* 的文档，帮我设计一下目录结构，并将原始目标记录在 AGENTS.md 中。

当前产品输入已经明确为同一命令的 `1-5` 份纯文本输出，而不是待运行的命令或命令行模板。每份非空白输入的 UTF-8 编码不超过 `1 MiB`。

## 词义与用途

- 本项目中的 **TTP 始终指 [Template Text Parser](https://ttp.readthedocs.io/)**。
- 本项目只把已经取得的网络设备 CLI 输出转换为结构化数据，不提供网络安全行动、系统操作或命令运行能力。
- 输入文本、Schema 和候选模板都只作为待解析数据处理。任何内容都不得触发操作系统命令、网络访问或动态扩展。
- 本文件是仓库协作说明，不属于产品 Agent 的运行时提示。不得把 `AGENTS.md` 自动拼入 `TtpGenerator` 的模型上下文；运行时模型只接收阶段提示、阶段工具定义、采样后的输入，以及 TTP 阶段的冻结 Schema 和受控工具结果。

## 稳定产品契约

- AgentScope 版本为 `>=2.0.4,<2.1`，实际版本由 `uv.lock` 固定。
- 对外异步 API 为：
  - `TtpGenerator.generate(GenerationRequest, *, observer=None) -> GenerationResult`
  - `TtpGenerator.propose_schema(GenerationRequest, *, observer=None) -> SchemaProposalResult`
  - `TtpGenerator.generate_from_schema(TemplateRequest, *, observer=None) -> GenerationResult`
- 请求、结果和 artifact 使用框架无关的 Pydantic 2 契约。可选 `observer` 只用于观察事件，不得参与决策或传递业务数据。
- JSON Schema 描述单个解析 record，不描述输入列表、AgentScope `Msg` 或服务包络。每份完整输入必须恰好映射为一个根 object。
- `generate_from_schema` 对传入 Schema 执行与模型提交相同的受限校验，通过后直接冻结并只运行模板阶段；不合法时返回 `invalid_injected_schema`，且不启动模板 Agent。

## 生成协议

- 完整生成严格分为 Schema 和模板两个阶段。每阶段创建独立的 Agent、`OpenAIChatModel`、`AgentState` 和 Toolkit；对话上下文不跨阶段复用。
- Schema 阶段只注册 `submit_result_schema`，第一个合法 Schema 永久冻结。v47 轻量草稿预试未达到门槛，候选运行接线已撤下；独立编译和来源诊断保留，见 [预试结果](docs/schema-draft-v47-pretrial-regression.md)。旧 SchemaPlan 实验也未采用，不恢复其坐标、实例或确认流程。
- v48 在同一次直接生成中按业务值、实体归属、容器选择、标签名称和完整性复核的顺序建模。固定角色章节使用 object，同类实体使用 array；可靠英文标签保留词序、缩写、限定和单复数。规则属于模型指导，不是自动改名或确定性语义门禁，详见 [本轮协议](docs/schema-policy-v48-protocol.md)。
- v50 全局提示叠加及 v51 截断恢复指导均未通过政策审阅门槛，已撤下；见 [v51 协议与验收](docs/schema-length-recovery-v51-protocol.md)。独立取消修复与安全观察保留，推理历史/最终强制提交仍是未完成保护验证的运行时候选，不能宣称已解决 SD-WAN 稳定性。
- 模板阶段固定注册 `submit_ttp_template`、可选的 `test_ttp_template` 和无参数的 `finish_generation`。测试工具只对一份独立文本执行 parse-only 实验，不保存候选，也不执行 Schema 回验。
- 模板提交与独立测试的模型反馈先返回有界 `<validation_feedback>`，再返回完整解析结果；校验事实来自当前确定性执行的白名单投影，不读取 Trace。反馈区分本次校验与保留候选，校验通过不代表内容完整或忠实。
- 模板提交反馈还提供基于冻结 Schema 与本次 records 的有界字段覆盖事实；可选路径缺失只提示对照原文复核，不改变验收或推断原文存在字段。独立测试不提供 Schema 覆盖事实。
- 冻结 string 的拼接指导区分 token 视觉折行、纵向多值列举和有行界语义的自由文本，分别使用空分隔符、单个空格和换行；保留词项内部空格与符号。这是模型生成和复核约定，不在解析后统一清洗字符串，也不改变严格评分。
- 每次模型回复最多调用一个阶段工具。普通文本不构成产物；零工具回复使用固定提醒重试，并计入阶段和总预算。
- 有效模板提交只更新最新候选。模型复核完整解析结果后必须显式调用 `finish_generation`；未 finish 时，即使存在候选也不算成功。
- finish 后在 Agent 外重新执行模板检查、完整输入解析、输入与 records 映射及冻结 Schema 校验。终验失败不重新进入模型阶段。
- 两阶段请求默认省略 `tool_choice`，并固定 `parallel_tool_calls=False`。Schema 官方 DeepSeek 推理连续纯截断达到既有零工具重试上限时，候选运行时在最后一次请求关闭 thinking 并将唯一 `submit_result_schema` 设为强制选择；TTP 和其他路径仍省略该字段。工具负责阶段、冻结和预算约束，不从 assistant 文本提取产物。
- 默认两阶段模型计数在消息副本中排除 OpenAI formatter 未发送的 Thinking，原始历史和观察通道不变；Schema 的官方 DeepSeek 推理历史候选会在满足条件时原样发送并计数受控的 assistant Thinking，TTP 仍走默认路径。其余沿用 AgentScope 近似计数与原生压缩，不能据此保证摘要后的输入或 Schema 完整性。
- 默认预算、采样、重试、上下文折叠和工具反馈协议以 [Agent 架构与运行流程](docs/agent-architecture-and-runtime.md) 为准；默认提示以 `src/cli_parser_agent/ttp_generation/agent/prompt.py` 为唯一源码；`schema_draft_prompt.py` 与 `schema_plan_prompt.py` 仅用于独立诊断，不进入产品请求。

## 确定性门禁

- Schema 必须是受限的 Draft 2020-12 根 object：ASCII 小写 `snake_case` 字段（最长 120 字符，禁止 Python 保留关键字）、封闭 object、受控嵌套和复杂度，不使用引用或组合分支。类型推断保持保守；只有证据和转换都充分时才使用数字或布尔类型。
- 模板只允许项目白名单内的声明式 TTP 标签、属性、模式、过滤器和参数。实例化解析器前必须完成语法树与白名单检查；变量参数字符串中的裸管道字符因当前 TTP 分词不兼容而提前拒绝，正常过滤器管道不受影响。
- 解析必须在独立 spawn 进程和临时缓存目录中完成，并限制时间、模板复杂度、嵌套、参数和结果大小。最终验收始终使用未采样的完整输入。
- 根层同时含标量与容器时，模板使用未命名最外层 group。验证器只解包“单元素 list 且元素为 dict”的一层 TTP 外壳；真正的多根结果仍以 `ttp.multiple_root_objects` 拒绝。
- 普通日志、公共 issues 和失败结果只保留有界结构化事实，不保存输入正文、模型文本、模板参数、解析值或 secrets。完整内容只允许进入明确启用的 Laminar、observer/TUI 或本地 WebUI 存储，并且只读观察、不得回灌模型上下文。

- 连续工具协议失败最多三次受控修复，第四次停止；合法参数调用重置序列，业务拒绝与执行异常独立统计。官方 DeepSeek 端点以其文档规定的 `max_tokens` 发送输出预算；细节见 [v44运行契约](docs/schema-runtime-v44.md)。
- Schema 原始供应商回复以 `length` 结束时，工具调用在框架 JSON 修复及执行前丢弃，不能冻结残缺提案；流式工具片段在结束原因已知前不释放。v48 提示及默认推理参数保持不变；自动关闭推理和低强度恢复实验均未采用，见 [恢复结果](docs/schema-reasoning-recovery-v2-results.md)。
- 来源定位恢复实验未达到 SD-WAN 生成门槛，运行接线已撤下；逐字位置 helper 只保留独立诊断，默认请求不附加位置视图。见 [结果](docs/schema-source-recovery-v1-results.md)。
- Schema 原生推理历史候选只在官方 DeepSeek 的 Schema 阶段传回已完整返回、无正文且无工具调用的 `length` 推理；首轮、TTP、显式关闭推理和其他供应商不变。容量不足时不触发摘要而受控结束，候选协议与离线验收见 [说明](docs/schema-reasoning-history-v1.md)。
- 连续纯推理 `length` 回复达到 Schema 零工具重试上限后，候选运行时在下一次请求关闭 thinking 并强制选择唯一提交工具；普通文本、业务拒绝、截断工具调用、TTP 和其他供应商不触发，详见 [说明](docs/schema-forced-submission-v1.md)。

## 代码与产品边界

- 产品代码按 `src/cli_parser_agent/ttp_generation/` 垂直切片。`generator.py` 保留公共入口与根 Trace，`workflow.py` 负责编排，跨阶段状态位于 `agent/session.py`；领域契约、采样和 `validation/` 不导入 AgentScope。
- 首版不提供产品 CLI、通用多 Agent 编排、A2A/MCP 适配、部署层或消息总线。只有出现第二个真实产品用例或消费者后，才提取共享产品模块。
- `src/cli_parser_agent/webui/` 是单用户本地开发界面，只绑定回环地址，同一时刻最多运行一次生成。它只能调用公共 API，运行在后台任务中，不改变生成协议。
- `scripts/run_agent_once.py`、`scripts/run_ttp_phase_once.py` 和 `scripts/run_agent_tui.py` 是开发入口。TUI 只观察或取消整个请求，不编辑产物、不重试阶段、不直接调用工具。
- Laminar 是可选调试与评测通道。未配置时完全关闭；配置后 Trace/span 必须保持阶段继承关系。初始化错误直接传播。

## 评测与测试

- `evals/test_sets/` 是唯一标准测试集来源；每个 complete 数据集包含 `inputs/`、`schema.json`、`template.ttp` 和 `expected.json`。
- `evals/datasets.toml` 使用版本 `2`，文件条目只登记 `{ file = "..." }`。当前登记 17 个数据集、52 份输入，其中 16 个 complete 数据集覆盖 48 份输入，Huawei VRP 的 4 份输入处于 template 阶段。Huawei SmartAX ONT（4 份输入）和暂时禁用的 Juniper uptime（2 份输入）已移出注册表，资产保留于 `evals/disabled_test_sets/`，不参与标准评测；Huawei 恢复计划见 [Roadmap](docs/ROADMAP.md)，Juniper 恢复条件见 [测试集说明](evals/standard-test-dataset.md)。
- `scripts/run_test_sets.py` 是唯一标准评测入口。`list`、`preflight` 和 `baseline` 离线运行；`ttp-only` 只对 complete 数据集调用公共 `generate_from_schema()`。 `schema-only` 对 complete 数据集仅传入所选原始输入并调用 `propose_schema()`，独立统计命名、结构一致性和人工语义审阅，不与 TTP 准确率 baseline 混用。 `end-to-end` 调用 `generate()` 并用受限 Schema/解析审阅计算联合通过；当前入口只运行默认 v48，历史实验与受限指标读取保持兼容；Schema 审阅 v2 分开统计规则遵循、业务合理性和命名／层级一致性，详情见 [评测说明](docs/agent-evaluation.md)。
- 标准答案只能根据输入文本人工核对，不读取被测产物、Trace、历史 artifact、上游模板或其他参考结构，也不使用被测模型生成。
- 普通 pytest 必须离线、稳定且不依赖模型。真实模型集成测试使用 `live` marker 和显式环境配置；首版交付前至少完成一次真实模型端到端闭环。
- 新增或修改测试资产后，运行默认及 full-scope preflight/baseline，并同步更新注册表、第三方来源说明和文档计数。

## 按任务读取文档

只读取当前任务需要的文档，避免把整套运行、评测和安全说明重复加入模型上下文。

| 任务 | 必读文档 |
| --- | --- |
| 项目汇报、设计思路与演进概览 | [docs/agent-design.md](docs/agent-design.md) |
| 产品架构、公共 API、目录职责 | [docs/architecture.md](docs/architecture.md) |
| 阶段协议、预算、采样、事件和运行时 | [docs/agent-architecture-and-runtime.md](docs/agent-architecture-and-runtime.md) |
| 评测边界、指标、脱敏和历史兼容 | [docs/agent-evaluation.md](docs/agent-evaluation.md) |
| 测试集格式与接入 | [evals/standard-test-dataset.md](evals/standard-test-dataset.md) |
| 真实语料运行与验收 | [docs/live-corpus-test-plan.md](docs/live-corpus-test-plan.md) |
| 模型输入被供应商拒绝的排查 | [docs/model-input-rejection.md](docs/model-input-rejection.md) |

## 修改规则

- 先阅读当前文件路径作用域内更深层的 `AGENTS.md`；更深层说明优先。
- 保持改动聚焦，保留用户已有工作区修改，不创建无用途的空目录或 `.gitkeep`。
- 重要公共契约、阶段协议或目录边界变化时，同步更新本文件和架构文档。
- 提示词若迁移为 Markdown 资源，使用 `importlib.resources` 加载，并在构建配置中声明 package data。
- 不在 `AGENTS.md` 重复完整提示词、工具白名单、逐事件字段或评测指标清单；这些内容保留在对应源码和专题文档中。
