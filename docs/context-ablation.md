# 上下文策略消融实验

`scripts/context_ablation.py` 是默认关闭的开发实验适配器。它只在当前进程调用标准
`scripts/run_test_sets.py` 期间替换 builder 使用的两个类和实验用历史折叠函数；退出时恢复，产品默认
AgentScope 压缩、公共 API、采样、提示词、预算和评测资产不变。不要在同一进程嵌套
变体，或在实验期间执行无关生成；同一变体的并发 trial 各自拥有独立记录。

| 变体 | Thinking 估算 | 上下文策略 |
| --- | --- | --- |
| `current` | AgentScope 原实现 | 原生自动摘要和 ToolResult 截断 |
| `estimator` | 从计数副本移除 OpenAI formatter 未发送的 Thinking | 原生自动摘要和 ToolResult 截断 |
| `retention` | AgentScope 原实现 | 固定保留任务，关闭模型摘要和 ToolResult 截断 |
| `combined` | 移除未发送的 Thinking | 同 `retention` |

估算修正不删除 AgentState、observer 或 Trace 中的 Thinking，也不会减少 formatter
本来就未发送的内容。它继续使用 UTF-8 字节数除以 4 的近似值，并不是供应商 tokenizer。
适配器限于锁定的 AgentScope 2.0.4 系列；升级依赖后必须重新核对私有方法和离线测试。

保留变体把阶段初始 user message 保留为权威任务，其中包括已经完成采样的输入、输入
分组和 TTP 阶段的完整冻结 Schema。每轮在内存中比较原文是否仍相等；它不把未采样全文
重新塞进模型，也不生成续接摘要。旧提交结果使用项目现有固定说明折叠，但保留变体同时
保护最新提交和当前有效候选对应的完整结果；候选通过受控反馈中的提交编号定位，不把
工具调用次数当作预算计数。因此失败提交之后，模型仍能复核先前有效
候选。所有调用参数、独立测试历史及 session 中保留候选的状态不变，失败提交仍可 finish
此前的有效候选。保留变体拒绝缺失、重复、结果先于调用的工具配对；如果保留候选的
受控反馈不能唯一定位，也在下一次模型请求前以 `ContextAblationIntegrityError` 停止，
sidecar 记录 `context_integrity_failed`，不继续使用无法复核的候选历史。

每轮在调用模型前检查 formatter 输出的近似 token 数与配置输出预留。超限时立即以固定
`ContextAblationBudgetExceeded` 停止，不裁切 records 或原始任务。sidecar 记录
`context_budget_exceeded`；现有公共 API 将其归入 `internal_error`，本实验不改变生产
错误映射。估算不能保证精确预知供应商边界；供应商明确返回 `context_length_exceeded`
时另记 `provider_context_rejected`，继续使用现有受控失败路径，不改写请求再试。

## 离线验证

```powershell
uv run python scripts/context_ablation.py offline --output .artifacts/context-ablation/offline.json
uv run pytest tests/unit/test_context_ablation.py
```

离线模式使用合成输入、Schema、校验结果和模拟 OpenAI SDK transport，实际经过
AgentScope runner 与 OpenAI formatter。四组执行同样的长 Thinking、有效提交、独立
测试、失败提交和 finish 序列；原始组可触发框架摘要，另外三组应保留原始任务。
这些结果仅验证机制，不能用作模型准确率评测或代替标准测试集分数。

## 标准评测

每组在独立进程显式选择变体，后面的参数原样交给唯一标准评测入口：

```powershell
uv run --env-file .env python scripts/context_ablation.py run --variant current --diagnostics .artifacts/context-ablation/current.json -- run --registry evals/datasets.toml --mode ttp-only --tag hard --trials 4 --concurrency 4 --trace-rounds
```

再分别将 `current` 与 sidecar 文件名替换为 `estimator`、`retention`、`combined`。
运行前核对 `--tag hard` 的实际 case 数和 input scope；维持同一模型、提示版本、输入、
预算与 120 秒模型 HTTP 超时。脚本不自行设置供应商参数或更改标准 runner 的输出目录。
标准准确率和 Trace ID 仍由标准 runner 生成，sidecar 只补充实验机制事实。

sidecar 按 Agent 序号、阶段及可用的请求 UUID、Trace UUID 记录有限个计数、固定类别与原始任务相等
布尔值，包括摘要前后的角色数量、工具配对、字节数、触发次数和完成压缩次数。最多保留
每 Agent 的 256 次检查及 256 次请求观察，超出只增加省略计数。不写原始输入、Schema、
模板、records、模型文本、摘要正文、工具参数、凭据或裸内容哈希。所有相等比较都在
内存中执行，sidecar 不提供跨进程内容指纹。

`role_estimated_tokens` 分别按固定角色的 formatter 消息 JSON 估算 UTF-8 字节数除以 4；
它不包含工具定义，分组 JSON 开销也与整体不同，因此各角色估算之和不保证等于包含
tools 的总估算。它仅帮助定位上下文增长来自哪类实际发送消息。

`trace_id` 在 Agent 创建时从当前 Laminar Trace 读取，并验证为 UUID；未启用或不合法时
写 `null`。并发评测用这个字段与标准 trial 的 `trace_id` 对齐，不能用 Agent 创建顺序
猜测 case 或 trial。没有 Trace ID 的运行只能保留独立实验事实，不声称已逐 trial 关联。

先比较原始任务保留率、摘要请求和强制工具选择次数、容量停止及工具配对；再按 case
比较严格通过率、有效候选、finish、Schema 错误和 worker 失败。Token、耗时及估算变化
只作辅助指标。8 次 trial 不足以预设准确率必然提升，未取得证据前不迁移为生产策略。
