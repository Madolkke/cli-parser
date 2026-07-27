# Agent 黑盒评测

<!-- markdownlint-disable MD013 -->

## 边界

评测系统从人工维护的 raw CLI 与 golden 出发，经 Laminar `evaluate(...)` 对现有 `TtpGenerator.generate()` 做非侵入式黑盒调用。它不修改公共 API、提示词、工具协议、AgentState 或默认策略；executor 不传 observer，每个 datapoint 只调用一次 `generate()`，target 在调用结束后才由 evaluator 读取。

版本化定义位于 `evals/ttp_generation/`，确定性加载与评分位于 `src/cli_parser_agent/evaluation.py`，人工入口为 `scripts/run_agent_evaluation.py`。`docs/skills/generate-cli-parser-eval-cases/` 是可手动安装的通用 Agent Skill 源码，只用于从 raw capture 制作 golden。

## Golden 定义

Manifest version `1` 的每个 case 包含 ID、命令说明、suite/tags、1-5 个有序输入路径及 SHA-256，以及 target 路径和 SHA-256。Target 为同序 expected records 和由 `path/type/required` 三元组构成的封闭 Schema 结构断言。当前 smoke suite 固定 `5` 个 case、`12` 份输入。

Golden 采用“最大稳定语义投影”：保留每份输入中的所有主实体和源顺序，只保留所有同类实体都稳定、非空且边界明确的细粒度业务字段；排除表头、分隔线、控制文本、空值和不稳定可选字段。值默认保持字符串。禁止从被测 Agent 产物、Laminar、历史 artifact、上游模板、参考 YAML/JSON 或模型生成结果复制答案。

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
uv run python scripts/run_agent_evaluation.py run --case ntc.cisco_ios.show_interfaces_status
```

`--trials` 范围 `1-10`、默认 `1`；`--concurrency` 范围 `1-4`、默认 `1`；`--name` 可覆盖 Evaluation 显示名。Harness 不 resume，也不重试失败 trial。退出码 `0` 表示全部 trial 严格通过且遥测完整，`1` 表示正常完成但至少一个 trial 未通过，`2` 表示定义、配置、Laminar 或归档错误，`130` 表示人工取消。

## Trace、评分与本地产物

Live run 先验证 `http://127.0.0.1:8000` SQL HTTP 与 `127.0.0.1:8001` gRPC，再创建 Evaluation 和 datapoints。Trace 层级为 `evaluation → executor → ttp.generate → schema.phase/ttp.phase → LLM/TOOL`，只启用 OpenAI instrumentation。运行结束 flush 后，入口通过只读 SQL 查询 `evaluation_datapoints` 和 `spans`，最多等待 `60` 秒处理延迟；超时标记 `telemetry_incomplete`，不重新调用模型。

严格通过同时要求生成成功、Agent 外全文重新验收、records 与 golden 深度全等、Schema 结构全等、公共 issues 无 error，并且 Evaluation、Trace 和必要 spans 完整入库。对象键顺序忽略；数组顺序、标量类型、缺失和 `null` 严格区分。叶子 precision/recall/F1 使用“数组索引归一为 `*` 的 JSON 路径 + 规范化标量值”多重集合；Schema 指标按路径、类型和 required 三元组计算。Laminar SQL 还汇总阶段/LLM/TOOL 时延、调用数、tokens 和 cost，并按 case 计算 trial 通过率、均值、p50 和 p95。

本地 `.artifacts/agent-evals/<UTC-run-id>/summary.json` 仅包含配置指纹、Git revision/dirty、case/trial 状态、数值指标、安全 issue code、Evaluation/Trace ID 和 URL。完整配置、文件哈希、模板、records、capture、原始输入、target 和模型文本不写入本地摘要。
