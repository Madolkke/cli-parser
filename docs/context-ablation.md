# 上下文策略消融实验

`scripts/context_ablation.py` 是默认关闭的开发实验适配器。它只在当前进程调用标准
`scripts/run_test_sets.py` 期间替换 builder 使用的两个类和实验用历史折叠函数；退出时恢复，产品默认
AgentScope 压缩机制、公共 API、采样、提示词、预算和评测资产不变。不要在同一进程嵌套
变体，或在实验期间执行无关生成；同一变体的并发 trial 各自拥有独立记录。

| 变体 | Thinking 估算 | 上下文策略 |
| --- | --- | --- |
| `current` | AgentScope 原实现 | 原生自动摘要和 ToolResult 截断 |
| `estimator` | 从计数副本移除 OpenAI formatter 未发送的 Thinking | 原生自动摘要和 ToolResult 截断 |
| `retention` | AgentScope 原实现 | 固定保留任务，关闭模型摘要和 ToolResult 截断 |
| `combined` | 移除未发送的 Thinking | 同 `retention` |

Thinking 计数修正已单独纳入产品默认实现。上表四组保留历史实验定义：`current`
表示旧计数对照，不再代表最新产品默认；`current` 和 `retention` 显式调用
AgentScope 原始计数，`estimator` 和 `combined` 调用产品修正计数。直接产品调用与
`estimator` 的 SDK 请求、预算及折叠行为由离线 parity 测试验证。保留策略仍仅供实验，
没有迁入产品默认。下方带日期的历史评测和当时选型结论保持原样。

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
只作辅助指标。8 次 trial 不足以预设准确率必然提升，权威上下文保留策略仍需容量和
完整性验证后再决定是否迁入产品默认；Thinking 计数修正单独按确定性兼容性验收。

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

## 最终 v35 回归

提示边界复核发现缩进示例不会在 Status 行结束 notes 捕获，因此 v35 补充适用条件和
实际解析反例测试。最终版本在 `32f7f2f` 上使用生产默认上下文策略单独完成同配置的
8 次标准评测，未重跑失败 trial；四组 v34 消融不受影响。

v35 严格通过 3/8、有效候选 7/8、finish 与独立验收均为 3/8；LLDP 为 0/4 严格通过、
3/4 有候选，Power 为 3/4 严格通过、4/4 有候选。平均耗时 640.67 秒，首次完整提交
平均 347.22 秒。Schema 拒绝 12/30 次提交，worker 失败 1/30 次提交和 2/20 次独立
测试；一次提交被裸管道兼容性门禁拦截，SystemExit 为零。

四个有候选但未 finish 的 trial 全部因剩余时间低于 120 秒而跳过下一轮，剩余分别为
61.297、58.906、84.531、100.641 秒；均未开始终验，不能把这些候选算作严格通过。
原生摘要和供应商拒绝强制 tool_choice 的现象仍有记录；这次直接标准评测没有实验
sidecar，不能据此报告精确的原始任务丢失次数。

最终生产版本为 3/8，v32 基线为 0/8，v34 current 为 3/8；改善集中于 Power，样本不足
以认定稳定泛化收益。最终离线验收为 824 passed、3 live skipped，Ruff 及格式检查通过，
标准离线 baseline 仍为 10/10，评测资产未修改。详见本地
`.artifacts/accuracy-optimization/v35/20260907T180402.519249Z/summary.json`。

## 2026-09-09 Thinking 计数默认修正与回归

产品实现提交为 `7a27c33`，在 `ObservedOpenAIChatModel.count_tokens` 的消息副本中
排除 formatter 未发送的 Thinking；Schema/TTP 两阶段统一使用该实现。其余计数沿用
AgentScope 算法，真实消息与观察通道不变。原生摘要、结果截断、历史折叠和时间门槛
保持原状；本次不是权威上下文保留策略的上线，也不减少模型生成推理的预算。

离线验收为 **845 passed、3 live skipped**，Ruff 与全仓格式检查通过，默认范围
preflight 无失败、baseline 为 **10/10**。新增测试验证消息及 metadata 不变、两阶段
初始拟合一致、240K 字符 Thinking 不触发摘要，以及真实可见长文本仍在容量内触发
原生摘要。直接产品调用与 `estimator` 的 SDK 请求、轮次、提交和候选状态完全一致；
旧 `current` 仍能复现 Thinking 误触发。评测资产、依赖和 v35 提示没有修改。

真实评测从干净的 `7a27c33` 执行，通过 `estimator` 观察适配器委托标准 runner，
仅运行修复版 8 次，没有补跑失败 trial。模型、policy、提示、输入选择及 case/trial
数量均与上一节 v35 历史对照一致：deepseek-v4-flash、temperature 0、非流式、
max_tokens 8192、context 128000、模型重试 2、HTTP timeout 120 秒；总预算
900 秒、13 轮、9 次提交、3 次独立测试。两个 hard 用例各 4 次、并发 4，使用
LLDP `inputs/005.txt` 和 Power `inputs/001.txt`，不是 full scope。

| 指标 | 历史 v35 | Thinking 修正 |
| --- | --- | --- |
| 严格通过 | 3/8 | 2/8 |
| 有效候选 | 7/8 | 4/8 |
| finish 成功 / 独立验收通过 | 3/8 / 3/8 | 2/8 / 2/8 |
| LLDP 严格通过 / 有效候选 | 0/4 / 3/4 | 0/4 / 0/4 |
| Power 严格通过 / 有效候选 | 3/4 / 4/4 | 2/4 / 4/4 |
| Schema 拒绝 / 提交 | 12/30 | 21/32 |
| worker 失败 / 提交 | 1/30 | 0/32 |
| worker 失败 / 独立测试 | 2/20 | 0/22 |
| SystemExit（提交与独立测试合计） | 0 | 0 |
| 平均耗时（秒） | 640.67 | 672.48 |
| 首次完整提交平均耗时（秒） | 347.22（8 次观测） | 339.92（8 次观测） |
| 首个有效候选平均耗时（秒） | 487.21（7 次观测） | 424.53（4 次观测） |
| 已观测输入 Token | 2,626,260 | 2,941,510 |
| 已观测输出 / 推理 Token | 619,943 / 594,977 | 596,701 / 575,011 |

Schema/worker 计数来自限定 Trace UUID 和时间范围的 Laminar 聚合，严格通过来自
标准 runner 独立评分。首个有效候选的观测分母不同，不能将其平均值直接解释为加速。
新组 58 次请求中有 56 个带 usage 的 LLM span，56 个均记录推理用量；另外两次
在途调用被总截止取消。历史推理用量为 57/61 个 LLM span。因此 Token 是已观测
用量，不代表完整账单，也不支持宣称推理成本降低。

机制 sidecar 与全部 **8/8** trial 按唯一 Trace UUID 对齐，无遗漏、重复或未关联
Agent。**58/58** 次请求保留初始任务文本（含采样输入和冻结 Schema），58 次检查
前后的工具配对均完整且顺序有效；compression 触发和完成均为 **0**，显式
`tool_choice` 和发送 reasoning content 均为 **0**。`context.fit` 仍为 8 次，
不能把这个初始拟合事件当成摘要。计数最大值为 13,499，formatter 请求近似值最大为
14,607，均远离 128,000 的配置容量；这些近似值不是供应商 tokenizer 结果。
没有记录 provider context 拒绝。本次没有真实压缩，不证明压缩发生后任务仍完整，
也没有验证保留候选 records 在真实历史中始终完整。历史 v35 未启用 sidecar，
不提供可直接对比的精确压缩次数或初始任务丢失次数。

失败漏斗如下：

- LLDP 全部 11 次提交都被 Schema 拒绝，4 次 trial 均无有效候选。trial 2/4 在
  剩余 95.844/70.266 秒时被 120 秒启动门槛拦住；trial 1/3 到 900 秒总截止取消
  在途调用。提前停止 trial 的顶层结果仍是 `agent_stopped`，rounds 中记录了
  `insufficient_remaining_time` 和阶段 `generation_timeout`；报告据事件说明触发事实。
  取消后出现的 no-tool retry 事件没有伴随后续模型请求，不能解释为模型主动空回复。
- Power 四次均产生候选，trial 3/4 显式 finish 并严格通过；trial 1/2 都继续修正
  到第 9 次提交，因提交预算耗尽结束，未调用 finish。其中 trial 2 直到第 9 次
  提交才首次通过，仍按既有协议因预算耗尽失败。两个候选的严格正确性未知，
  不能自动计作成功，也不能据此断言放宽时间门槛能解决这两次失败。
- 裸管道门禁拦截一次 LLDP 独立测试；全部提交和独立测试均无 worker 错误。
  worker 错误下降没有转化为整体准确率提升。

结论：**计数与 formatter 不一致的确定性缺陷已修复，本轮准确率没有改善**。
严格通过和有效候选均较历史对照下降，尤其 LLDP 的 Schema 拒绝仍需独立排查。
本次是历史对照而非同期随机实验，8 次样本不能确定下降由计数修正导致，也不能
认定修正改善准确率。按既定范围保留默认计数修正，原生摘要仍可能丢失输入或 Schema；
完整上下文保护、候选复核与 finish 改进仍需后续单独设计和验证。

本地脱敏产物位于 `.artifacts/thinking-token-fix/`：`comparison.json/.md`、
`estimator.json`、`mechanisms.json` 和 `offline.json`；标准运行目录为
`evaluation/20260908T154425.977638Z`。这些统计不导出输入、Schema、模板、records
或 Trace 正文，不持久化内容哈希。
