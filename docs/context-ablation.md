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

先比较原始任务保留率、摘要请求和显式 tool_choice 请求数、容量停止及工具配对；再按 case
比较严格通过率、有效候选、finish、Schema 错误和 worker 失败。Token、耗时及估算变化
只作辅助指标。8 次 trial 不足以预设准确率必然提升，未取得证据前不迁移为生产策略。

## v1 结果解释边界

v1 sidecar 的 `forced_tool_choice` 实际表示请求提供了显式 `tool_choice` 参数，包含
框架回退使用的 `auto`，不能解释为真实强制选择次数。报告统一称“显式 tool_choice
请求数”；已有布尔值不足以还原其中强制选择和 `auto` 的数量。

`retention` 与 `combined` 使用相同的模型策略：都关闭摘要和结果截断，并使用独立的
formatter 估算执行容量门禁。后者的 Thinking 计数修正主要改变触发遥测，不能把两组
分数差异解释为组合收益。保留组整体包含原始任务保护、有效候选结果保留、关闭摘要、
关闭结果截断及容量门禁；与其他组的比较不能单独证明其中某一项带来准确率提升。

真实评测的 `task_intact` 直接验证初始任务文本（含采样输入和冻结 Schema）仍完整存在。
候选结果完整性依靠保护规则和离线 SDK 测试，v1 sidecar 没有逐轮记录候选正文相等结果，
因此不能声称真实评测提供了候选 records 逐轮字节相等的独立证据。

## 2026-09-08 消融结果与选型

四组从 `6e62880` 的独立 checkout 顺序执行，使用 v34 提示、deepseek-v4-flash、
temperature 0、HTTP timeout 120 秒、总预算 900 秒、13 轮、9 次提交、3 次测试。
两个 hard 数据集各重复 4 次、并发 4，使用注册表的默认输入范围；不是全部输入评测。
模型参数、采样、预算、提示和输入选择在四组间一致，评分来自标准 runner 的独立验收。

| 变体 | 严格通过 | 有效候选 | finish 成功 | LLDP / Power 严格通过 | 平均耗时（秒） |
| --- | --- | --- | --- | --- | --- |
| current | 3/8 | 5/8 | 3/8 | 0/4 / 3/4 | 707.77 |
| estimator | 1/8 | 6/8 | 2/8 | 0/4 / 1/4 | 710.29 |
| retention | 4/8 | 4/8 | 4/8 | 0/4 / 4/4 | 628.66 |
| combined | 2/8 | 5/8 | 2/8 | 0/4 / 2/4 | 687.75 |

| 变体 | Schema 拒绝/提交 | worker 失败/提交 | 摘要完成次数 | 初始任务丢失请求数 |
| --- | --- | --- | --- | --- |
| current | 16/31 | 1/31 | 2 | 2 |
| estimator | 12/34 | 1/34 | 0 | 0 |
| retention | 16/28 | 0/28 | 0 | 0 |
| combined | 16/34 | 1/34 | 0 | 0 |

四组均无 SystemExit 提交失败，`context.fit` 各 8 次；这个采样拟合事件与自动摘要次数
不是同一指标。每组 8 个 sidecar 都按 Trace UUID 唯一匹配，观测没有省略。
retention 的 53 次请求和 combined 的 59 次请求都保留初始任务；两组均无上下文完整性
停止、容量停止或已记录的 provider context 拒绝，但真实请求没有接近容量上限。

本轮不把实验上下文策略迁入生产。estimator 修正了误计 Thinking 导致的压缩触发，
retention 保留了权威任务与候选复核材料；机制验证成立，严格准确率收益尚不稳定。
retention 与 combined 的模型策略相同，共 6/16 严格通过，与 current 的 3/8 比例相同；
这只是描述性比较，不构成等效性检验。四组都未解决 LLDP，不能凭单组 Power 4/4 宣称
整体准确率提升，也不能用本轮未超限证明真实长上下文的容量安全。

后续优先验证整行锚点、自由文本与业务标签的匹配冲突、字段值与装饰符边界，以及已有
可复核候选时是否应将下一轮启动门槛与 HTTP I/O timeout 分离。保持显式 finish 和独立
终验；第 9 次提交之后仍允许 finish 会改变现有协议，不属于本轮改动。

脱敏明细位于 `.artifacts/accuracy-optimization/` 的 `final-comparison.json`、
`ablation-mechanisms.json` 和 `implementation-and-results.md`。Token 为已观测用量，
超时或取消请求可能没有 usage；缺少记录不能按零计入完整用量。
