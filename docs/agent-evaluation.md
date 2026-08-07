# Agent 黑盒评测

<!-- markdownlint-disable MD013 -->

## 边界

评测系统从人工维护的 raw CLI 与 golden 出发，经 Laminar `evaluate(...)` 对现有 `TtpGenerator.generate()` 做非侵入式黑盒调用。它不修改公共 API、提示词、工具协议、AgentState 或默认策略；executor 不传 observer，每个 datapoint 只调用一次 `generate()`，target 在调用结束后才由 evaluator 读取。

版本化定义位于 `evals/ttp_generation/`，确定性加载与评分位于 `src/cli_parser_agent/evaluation.py`，人工入口为 `scripts/run_agent_evaluation.py`。`docs/skills/generate-cli-parser-eval-cases/` 是可手动安装的通用 Agent Skill 源码，只用于从 raw capture 制作 golden。

## Golden 定义

Manifest version `1` 的每个 case 包含 ID、命令说明、suite/tags、1-5 个有序输入路径及 SHA-256，以及 target 路径和 SHA-256。Target 为同序 expected records 和由 `path/type/required` 三元组构成的封闭 Schema 结构断言。当前 smoke suite 固定 `5` 个 case、`12` 份输入；baseline suite 固定 `3` 个 case、`3` 份输入，分别覆盖固定宽表、重复详情块和层级配置。baseline 的每个 case 只有一份格式明确的输入，用于低歧义的严格正确性基准，不替代多样例 smoke 验收。

Golden 采用“最大有证据语义投影”：保留每份输入中的所有主实体和源顺序，并保留至少在一个同类实体中非空出现、语义与边界明确的细粒度业务字段。只在部分父对象实例中出现的字段标记为可选并在缺失实例中省略；在每个父对象实例中都存在的字段才标记为 required。排除表头、分隔线、控制文本和空值，不使用空字符串或 `null` 占位。值默认保持字符串。禁止从被测 Agent 产物、Laminar、历史 artifact、上游模板、参考 YAML/JSON 或模型生成结果复制答案。

产品运行时允许冻结 Schema 接受的 `""`、空根对象和空容器，并交由模型判断其业务合理性；golden 规则仍排除空值，严格评分和质量指标继续区分空字符串、缺失键与空对象。这一评测差异不得反向实现为候选提交门禁。

离线操作不读取入口脚本中的 Key，也不初始化 Laminar 或联网：

```text
uv run python scripts/run_agent_evaluation.py list
uv run python scripts/run_agent_evaluation.py preflight
```

Preflight 检查严格 JSON、路径逃逸、UTF-8、大小、终端噪声、凭据模式、文件哈希、record/input 一一对应、字符串来源和 Schema 断言闭合。

## Live 配置与运行

Live run 从环境变量读取模型、预算与自托管 Laminar 配置。必须设置
`OPENAI_API_KEY`、`OPENAI_MODEL`、`LMNR_PROJECT_API_KEY`、`LMNR_BASE_URL`、
`LMNR_HTTP_PORT`、`LMNR_GRPC_PORT` 和 `LMNR_FRONTEND_PORT`；可选变量及其语义
见仓库根目录的 [`.env.example`](../.env.example)。缺失、空白或明显过短的 Key 会
在任何网络访问之前被拒绝。Key 不进入配置指纹、metadata、span input/output、
本地摘要、异常或测试快照。

Live run 必须显式选择 suite 或 case：

```text
uv run python scripts/run_agent_evaluation.py run --suite smoke
uv run python scripts/run_agent_evaluation.py run --suite baseline --trials 1 --concurrency 1
uv run python scripts/run_agent_evaluation.py run --case ntc.cisco_ios.show_interfaces_status
```

`--trials` 范围 `1-10`、默认 `1`；`--concurrency` 范围 `1-4`、默认 `1`；`--name` 可覆盖 Evaluation 显示名。Harness 不 resume，也不重试失败 trial。退出码 `0` 表示全部 trial 严格通过且遥测完整，`1` 表示正常完成但至少一个 trial 未通过，`2` 表示定义、配置、Laminar 或归档错误，`130` 表示人工取消。

### 高预算诊断运行

默认 `360` 秒、`13` 轮和 `9` 次 TTP 提交用于常规验收。需要观察完整修正链时，使用独立进程和单并发的高预算配置；诊断主运行固定为总时长 `7200` 秒、`32` 个 Agent 轮次、`24` 次 TTP 提交和单次模型超时 `120` 秒，避免时间预算放大后仍被较小的阶段预算截断：

```powershell
$env:CLI_PARSER_GENERATION_TIMEOUT_SECONDS = "7200"
$env:CLI_PARSER_MAX_AGENT_ITERS = "32"
$env:CLI_PARSER_MAX_TEMPLATE_SUBMISSIONS = "24"
$env:CLI_PARSER_MODEL_TIMEOUT_SECONDS = "120"
uv run python scripts/run_agent_evaluation.py run --suite baseline --trials 1 --concurrency 1
```

高预算值只属于开发诊断，不改变默认 `GenerationPolicy`，每次运行必须通过配置指纹记录有效模型、推理、预算和 Laminar 配置。先运行 `baseline` 建立低歧义基线，再运行 `smoke`；完整公开语料仍按 [真实命令输出语料测试计划](live-corpus-test-plan.md) 先 `5/5` smoke、再以 `--resume` 达到 `11/11`。高预算运行不自动重试失败 trial，取消或重新运行必须使用新的 run 目录并保留旧 Trace。

## Trace、评分与本地产物

Live run 先验证 `http://127.0.0.1:8000` SQL HTTP 与 `127.0.0.1:8001` gRPC，再创建 Evaluation 和 datapoints。Trace 层级为 `evaluation → executor → ttp.generate → schema.phase/ttp.phase → LLM/TOOL`，只启用 OpenAI instrumentation。运行结束 flush 后，入口通过只读 SQL 查询 `evaluation_datapoints` 和 `spans`，最多等待 `60` 秒处理延迟；超时标记 `telemetry_incomplete`，不重新调用模型。

严格通过同时要求生成成功、Agent 外全文重新验收、records 与 golden 深度全等、Schema 结构全等、公共 issues 无 error，并且 Evaluation、Trace 和必要 spans 完整入库。对象键顺序忽略；数组顺序、标量类型、缺失和 `null` 严格区分。叶子 precision/recall/F1 使用“数组索引归一为 `*` 的 JSON 路径 + 规范化标量值”多重集合；Schema 指标按路径、类型和 required 三元组计算。严格通过是 case 级全量门槛，部分分数只用于诊断，不替代最终验收。

## 系统化指标

每次评测同时报告四类指标，并按 case、suite 和输入形状分层；对象键顺序不影响评分，数组顺序、标量类型、缺失与 `null` 继续严格区分。

- **结果正确性**：case 严格通过率、input 级 records 通过率、record 数量与索引映射、records exact、Schema contract exact、叶子值 precision/recall/F1，以及 Schema 路径/类型/required precision/recall/F1。
- **结构与候选质量**：Schema 冻结率、是否进入 TTP、首个有效 TTP 候选率、有效候选后的 finish 率、最终验收率、每输入的漏字段/多字段和空容器诊断。模板是否过拟合、字段分组是否合理、可选字段是否自然省略等语义质量，只能在显式开发评测的 HumanEvaluator 评审中记录，不能由严格分数推断。
- **流程可靠性**：Agent 总轮次和分阶段轮次、工具调用及错误、Schema/TTP 提交、无工具回复/重试、模型重试、终止原因、故障域和 issue-code 分布；重复 trial 报告通过率、样本数和置信区间。
- **资源效率**：根、phase、`context.fit`、`agent.round`、`generation.deadline_cleanup`、`final.acceptance`、LLM 和 TOOL 的耗时及其 p50/p95/p99，LLM input/output tokens、cost、每轮上下文 token 增长斜率/峰值、分段解释时长比例及每个成功 case 的归一化成本。正常运行要求分段覆盖端到端时长至少 `98%`；Laminar 入库不完整时该 trial 只能标记为 `telemetry_incomplete`，不得用缺失数据补零来宣称通过。

case 汇总至少包含 trial 数、严格通过数/率、均值、p50、p95 和 p99；低 trial 数时明确注明尾部分位数的不确定性。报告同时保留 macro（按 case）和 micro（按输入）视角，避免多输入 case 以样本数量掩盖单输入 case 的失败。配置指纹必须绑定实际模型、prompt version、推理开关/强度、预算、采样和安全限制；Key 和原始输入不进入指纹。

### 开发期 HumanEvaluator

HumanEvaluator 只属于 `scripts/run_agent_evaluation.py` 等显式开发评测入口，不属于 `TtpGenerator.generate()`、产品 API、普通 pytest 或生产部署。评审人员在 Laminar Trace 的只读调试通道中检查一次 run 产生的**全部** Schema/TTP 候选（包括被拒绝候选、有效候选、capture 复核和最终候选），使用固定标签记录：解析边界、主实体/字段粒度、可选字段表达、多输入一致性、fixture-specific 过拟合和可维护性。

HumanEvaluator 不修改 Agent 状态、不触发重试、不向模型回灌评审内容，也不把模板、records、capture、原始输入或模型文本写入本地脱敏摘要；本地只允许保存有界的评审标签、issue-code、Trace ID 和数值指标。未启用 Laminar 时不执行全量候选人工评审。

Reviewer 子进程写入单条标签时只传 Trace ID、提交序号和有界标签，不传模板正文：

```powershell
uv run python scripts/run_agent_evaluation.py review `
  --trace-id <trace-id> --phase ttp --submission 1 --label repairable `
  --dimension boundary=misaligned --dimension optionality=stable `
  --issue-code ttp.capture_mismatch
```

`--phase schema|ttp` 用于区分 Schema 提案和 TTP 模板提交，默认是 `ttp`；两阶段候选都按 submission index 聚合评审覆盖率。

本地 `.artifacts/agent-evals/<UTC-run-id>/summary.json` 仅包含配置指纹、Git revision/dirty、case/trial 状态、数值指标、安全 issue code、Evaluation/Trace ID 和 URL。完整配置、文件哈希、模板、records、capture、原始输入、target 和模型文本不写入本地摘要。
