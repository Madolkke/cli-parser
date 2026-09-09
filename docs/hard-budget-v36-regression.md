# v36 Hard 扩大预算实验

2026-09-09 将单次生成预算从 **900 秒 / 13 轮 / 9 次提交**提高为
**1800 秒 / 26 轮 / 18 次提交**，重跑两个 Hard 用例各 4 次、并发 4。
严格通过由历史 v36 的 **3/8 到 4/8**，有效候选由 **6/8 到 8/8**，成功
finish 由 **3/8 到 7/8**。完成率的变化比严格准确率明显，但输入 Token 增加
82.14%，平均耗时增加 21.25%。

四个严格通过的 trial 都在原预算范围内完成。实际使用额外预算的三个 LLDP
trial 虽成功 finish，仍有内容差异；另一个 Power trial 耗尽 18 次提交也未
finish。因此本轮支持“更多预算提供了完成机会”，尚不能证明扩大预算稳定提高
严格准确率。产品默认预算保持不变。

## 配置与复现

从干净提交 `452c912feb019a14279b1df4875fc136883a0097` 启动。它相对上一轮
产品提交 `0ad1db6bae8b2a5b97085ceb9eaf9699d5d7eb2d` 只增加历史评测报告，
产品代码、提示、依赖和评测资产相同。

| 配置 | 历史 v36 | 本次 |
| --- | --- | --- |
| 总时间 | 900 秒 | 1800 秒 |
| 总轮数 | 13 | 26 |
| 模板提交 | 9 | 18 |
| 独立测试 | 3 | 3 |
| HTTP timeout / 模型重试 | 120 秒 / 2 | 相同 |
| 输入 | LLDP `inputs/005.txt`、Power `inputs/001.txt` | 相同，默认范围 |
| 次数 / 并发 | 各 4 次 / 4 | 相同 |

模型保持 deepseek-v4-flash、`https://api.deepseek.com`、temperature 0、非流式、
max_tokens 8192、context 128000、parallel_tool_calls false、TLS 校验开启。
thinking_enable/reasoning_effort 未设置，无 extra_body。提示仍为
`ttp-generator-v36-container-start-guidance-zh-cn`，Thinking 计数修正默认启用。

运行前和完成后均逐字段核对：policy 仅表中三项扩大，其他 policy、模型、提示、
输入选择及 case/trial 数一致。提交上限配套提高，是因为现有规则在最后一次提交
后即停止，可能提前挡住新增轮次。未修改这条规则或剩余时间不足 120 秒的启动门槛。
这是三个预算共同扩大的实验，不能把结果单独归因于时间或轮数。

```powershell
$env:CLI_PARSER_MODEL_TIMEOUT_SECONDS='120'
$env:CLI_PARSER_GENERATION_TIMEOUT_SECONDS='1800'
$env:CLI_PARSER_MAX_AGENT_ITERS='26'
$env:CLI_PARSER_MAX_TEMPLATE_SUBMISSIONS='18'
$env:CLI_PARSER_MAX_TTP_TEST_CALLS='3'
$env:CLI_PARSER_TEST_SET_ARTIFACT_ROOT='.artifacts/hard-budget-v36/evaluation'
uv run --env-file .env python scripts/context_ablation.py run --variant estimator --diagnostics .artifacts/hard-budget-v36/estimator.json -- run --registry evals/datasets.toml --mode ttp-only --tag hard --trials 4 --concurrency 4 --trace-rounds
```

`estimator` 是已验证与产品一致的观察适配器，委托标准 `run_test_sets.py`。
本次目录为 `.artifacts/hard-budget-v36/evaluation/20260909T135059.051142Z`；
历史目录为 `.artifacts/container-start-v36/evaluation/20260909T130055.139407Z`。
只运行这 8 次，没有补跑失败 trial、重跑历史组或修改 `.env`。

## 结果与失败层次

| 指标 | 历史 v36 | 扩大预算 |
| --- | --- | --- |
| 严格通过 | 3/8（37.5%） | 4/8（50%） |
| LLDP 严格通过 | 1/4 | 1/4 |
| Power 严格通过 | 2/4 | 3/4 |
| 有效候选 | 6/8 | 8/8 |
| finish 调用 / 成功 | 3/8 / 3/8 | 7/8 / 7/8 |
| 产品终验开始 / 通过 | 3/8 / 3/8 | 7/8 / 7/8 |
| runner 独立验收通过 | 3/8 | 7/8 |
| Schema 拒绝 / 全部提交 | 14/31 | 20/50 |
| Schema 拒绝涉及 trial | 6/8 | 6/8 |
| accepted / 全部提交 | 15/31 | 22/50 |
| worker 错误 / 提交 | 0/31 | 0/50 |
| worker 错误 / 独立测试 | 2/19 | 0/22 |
| parse-only 成功 / 独立测试 | 17/19 | 21/22 |
| SystemExit | 0 | 0 |

本次 LLDP 为 21 次提交、5 次 accepted、14 次 Schema 拒绝（涉及 4/4 trial）、
2 次其他拒绝；12 次测试中 11 次成功。Power 为 29 次提交、17 次 accepted、
6 次 Schema 拒绝（涉及 2/4 trial）、6 次其他拒绝；10 次测试均成功。
Schema 比例从 45.16% 到 40%，但拒绝绝对数增加、受影响 trial 数不变，不能据比例
下降认定语义修正能力改善。accepted 含同一 trial 的多次候选更新。

| Trial | 提交 / 测试 / 轮数 | 首候选秒数 | finish 秒数 | 完整请求秒数 | 严格结果 |
| --- | --- | --- | --- | --- | --- | --- |
| L1 | 4 / 3 / 8 | 818.991 | 943.515 | 946.859 | 内容不全等 |
| L2 | 8 / 3 / 12 | 1118.434 | 1125.344 | 1128.485 | 内容不全等 |
| L3 | 7 / 3 / 11 | 1124.434 | 1133.172 | 1136.344 | 内容不全等 |
| L4 | 2 / 3 / 6 | 672.427 | 680.125 | 683.000 | 通过 |
| P1 | 4 / 3 / 8 | 272.611 | 504.860 | 507.953 | 通过 |
| P2 | 4 / 3 / 8 | 267.094 | 518.390 | 521.656 | 通过 |
| P3 | 3 / 1 / 5 | 227.420 | 313.937 | 317.219 | 通过 |
| P4 | 18 / 3 / 21 | 227.910 | — | 1248.500 | 提交上限，未 finish |

首候选时间使用 Laminar 首 accepted TOOL 结束减 trial.started_at；finish 与请求
耗时使用本地事件/指标，两种时钟口径有小幅差异。各 trial 编号不是与历史组配对的样本。

**LLDP：结构验收通过后仍有内容差异。** L1–L3 均通过产品终验、独立验收和记录
数量检查，但 records 不与标准答案深度全等。L1 叶子 precision/recall 均为
0.982143；L2/L3 precision 为 1、recall 为 0.982143。本轮仅凭数值不能定位具体
字段或归因到容器起点，未将这些差异自动归为既往机制。

L1–L3 都在 900 秒后 finish，L2/L3 首候选也在 900 秒后产生；三者均使用了
780 秒后启动的逻辑轮。780 秒是旧总预算减 HTTP 120 秒的近似观察边界，具体门槛
由 session 时钟决定。唯一严格通过的 L4 未使用这些后续轮次。LLDP 最多 12 轮、
8 次提交，没有实际使用超过旧轮数或提交上限的额度。

**Power：额外尝试仍可能耗尽提交而不 finish。** P4 首次提交已 accepted，之后
共接受第 1、3、5、8、9、10、12、17 次提交。第 18 次返回 `ttp.invalid_record`，
随即以 `ttp_submission_limit` 结束；仍保留 S17 候选（第 8 个有效候选版本）。
它耗时 1248.5 秒、21 轮，尚有约 551.5 秒和 5 轮，但提交额度耗尽。

P4 的 18 次提交中 5 次 Schema 拒绝、5 次 invalid_record；没有 worker 错误、
原样重复提交拒绝或测试预算拒绝。后续失败没有清空候选，不能用“没有可 finish 的
候选”解释其未完成；accepted 也不能证明保留候选严格正确。本轮没有复放或评分
未 finish 的候选。三个严格通过的 Power trial 都未使用 780 秒后启动的轮次。

## 代价与上下文

| 指标 | 历史 v36 | 扩大预算 |
| --- | --- | --- |
| 平均请求耗时（秒） | 669.06 | 811.25（+21.25%） |
| 整批墙钟（秒） | 1614.68 | 2389.66（约 39.8 分钟） |
| 首次提交平均秒数（8 次观测） | 404.81 | 367.03 |
| 首候选平均秒数 | 486.35（6 次观测） | 591.17（8 次观测） |
| 输入 Token | 3,078,342 | 5,606,941（+82.14%） |
| 输出 Token | 617,116 | 730,246（+18.33%） |
| 推理 Token | 596,520 | 704,339（+18.07%） |
| 推理 / 输出 Token | 96.66% | 96.45% |
| 有 usage / 推理观测的 LLM span | 53 / 53 | 79 / 79 |
| 原生压缩触发 / 完成 | 0 / 0 | 0 / 0 |
| 旧提交结果折叠 | 23 | 42 |

Token 是已观测用量，未作账单保证；推理 Token 已包含在输出 Token 内，不能重复加总。
首候选均值分母不同，不作配对速度比较。
P4 单独消耗 1,639,957 输入 Token、131,644 输出 Token，始终未 finish。

sidecar 与 8/8 trial 按唯一 Trace UUID 关联完整，无省略；79 次实际请求与本地
模型尝试及有 usage 的 LLM span 对齐。79/79 请求保留初始任务，检查点的工具配对
与顺序均有效，无显式 tool_choice 或 reasoning content 发送。估算计数最大
16,602，formatter 近似计数最大 18,250；它们不是 provider tokenizer。

`context.fit` 仍为 8 次初始拟合，旧提交结果折叠不等于原生压缩。本轮没有摘要，
不能把失败归因于摘要丢失输入，也不能据此保证以后压缩后的材料完整。
task_intact 检查只证明初始任务仍在，不等同最新候选、反馈和 records 的完整性证明。

## 判断与后续方向

1. 本轮最明确的收益是候选和完成率提高。三个 LLDP trial 确实用额外时间走到
   finish，但内容仍未严格正确。
2. 严格分数多一次通过来自 Power；所有严格成功轨迹都处于旧预算范围内。
   8 次非配对历史对照不足以建立稳定增益，也不能排除提交预算反馈变化对行为的影响。
3. 当前应优先研究两类剩余问题：LLDP 已完成结果的具体内容差异，以及 Power
   在多次 accepted 后仍反复修改、不 finish 的决策原因。继续盲目扩容不能替代定位。

本次仅做预算实验和报告，没有修改产品默认、提示、解析、反馈或标准答案，也没有
自动实施后续建议。标准 runner 的本轮 Hard preflight 通过；未变更产品或测试资产，
未重复全量离线测试。既有实现验收仍为 872 passed、3 live skipped，Ruff/格式及
默认 preflight/baseline 通过，详见历史 v36 报告。

## 证据索引

本地文件位于 `.artifacts/hard-budget-v36/`：`comparison.json/.md`、
`mechanisms.json`、`estimator.json`、`budget-events.json`、`power4-tool-facts.json`。
SQL 仅投影有界计数、时序、UUID、工具状态与白名单错误码；本轮无需读取 Trace 正文，
未导出输入、Schema、模型文本、模板、records 或内容哈希，未向产品模型回灌 Trace。

| Trial | Trace UUID |
| --- | --- |
| L1 | `a9083ae6-6608-1330-1d9a-e3e4afb44d63` |
| L2 | `260f20ed-d265-97f7-8664-d81647ddfe8c` |
| L3 | `085d8410-e4e3-4606-bb7c-f86192f2b0fe` |
| L4 | `d443186d-8a00-a468-7ddd-9c145ab0ad10` |
| P1 | `1d0b940c-4afe-1b33-c64e-3a9fb0e52f55` |
| P2 | `f72454ae-7a1b-b26a-bd9e-d05209ee9003` |
| P3 | `086e85af-9fd0-e670-ca63-ddca60e6fbb6` |
| P4 | `8ad4a34f-9552-db20-1b3d-6c7c053d2114` |

历史对照见 [v36 容器起点回归](container-start-v36-regression.md)。
