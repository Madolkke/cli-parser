# v37 状态字符串保真提示与 Hard 回归

2026-09-10（北京时间）完成 v37 提示实现和 8 次真实回归。相同扩大预算下，
严格通过从历史 **4/8 到 6/8**：LLDP 从 **1/4 到 2/4**，Power 从 **3/4 到 4/4**。
四条 LLDP 最后 accepted 候选均保留了本轮重点检查的状态字段及其值内前导符号。
但其他字段仍有已知缺失，且 Schema/worker 错误与 Token 增加，不能据此宣称稳定
提升、普遍解决状态字段遗漏，或降低了推理成本。

## 实现与离线验收

实现提交为 `a28e66a89eb9ceb14687a9f38e71365af8af3d65`，提示版本为
`ttp-generator-v37-string-value-fidelity-zh-cn`。只改写 TTP 提示的可选字段指导，
区分冻结 string 字段的值槽缺失、字面空值和非空状态字符串；增加通用 assets
正例，要求保留业务值内符号，仅去除明确位于值外的标签和装饰边界。

三项合成测试直接提取模型实际收到的示例，经现有白名单、隔离 spawn worker 和
Schema 校验验证。正确例同时包含普通值、带符号状态值、空槽、缺失行及其前后
实体；两个单因素对照分别增加状态排除条件、将业务符号移入模板字面量，均仍通过
Schema，但 records 与独立预期不同。这证明内容复核仍有必要，未新增验收规则。

- recipe 文件 9 项通过，其中新增 3 项。
- 完整离线 pytest：875 passed、3 live deselected，耗时 423.24 秒；9 条既有依赖及转义警告。
- Ruff、格式检查（82 个文件）及 `git diff --check` 通过。
- 默认范围 preflight 无失败；baseline 10/10，原有 1 个 pending。
- AST 对照确认只改变提示版本和 TTP 系统提示；Schema、任务构造、重试提示及输入序列化不变。

公共 API、工具、反馈协议、解析规则、依赖、产品默认预算和评测资产均未修改。
Thinking 计数修正继续默认启用。表头根起点、历史诊断保留、finish 和上下文策略
未纳入本轮。

## 配置与复现

从上述干净提交启动，标准 runner 记录的 `git.dirty=false`。使用现有 estimator
观察适配器委托唯一标准入口 `run_test_sets.py`，仅运行新版本 8 次，无失败补跑。

| 配置 | 本轮与历史对照共同值 |
| --- | --- |
| 输入 | LLDP `inputs/005.txt`、Power `inputs/001.txt`，默认范围 |
| 次数 / 并发 | 各 4 次 / 4 |
| 模型 / endpoint | deepseek-v4-flash / `https://api.deepseek.com` |
| 采样 / 输出 | temperature 0，非流式，max_tokens 8192，context 128000 |
| HTTP timeout / 模型重试 | 120 秒 / 2 |
| 生成预算 | 1800 秒、26 轮、18 次提交、3 次独立测试 |
| 其他 | TLS 校验开启，parallel_tool_calls false，无 extra_body，thinking_enable/reasoning_effort 未设置 |

运行前及结束后均逐字段核对模型、全部 policy、输入选择、case/trial 数一致，预期
配置差异只有提示版本。扩大预算仅由本次评测进程的环境覆盖提供，不改变产品默认。

```powershell
$env:CLI_PARSER_MODEL_TIMEOUT_SECONDS='120'
$env:CLI_PARSER_GENERATION_TIMEOUT_SECONDS='1800'
$env:CLI_PARSER_MAX_AGENT_ITERS='26'
$env:CLI_PARSER_MAX_TEMPLATE_SUBMISSIONS='18'
$env:CLI_PARSER_MAX_TTP_TEST_CALLS='3'
$env:CLI_PARSER_TEST_SET_ARTIFACT_ROOT='.artifacts/string-value-v37/evaluation'
uv run --env-file .env python scripts/context_ablation.py run --variant estimator --diagnostics .artifacts/string-value-v37/estimator.json -- run --registry evals/datasets.toml --mode ttp-only --tag hard --trials 4 --concurrency 4 --trace-rounds
```

本轮目录为 `.artifacts/string-value-v37/evaluation/20260909T154533.557280Z`；
历史为 `.artifacts/hard-budget-v36/evaluation/20260909T135059.051142Z`。
历史产品行为与 v36 相同，详见 [扩大预算回归](hard-budget-v36-regression.md)。

## 结果与失败层次

| 指标 | 历史 v36 扩大预算 | v37 |
| --- | --- | --- |
| 严格通过 | 4/8 | 6/8 |
| LLDP 严格通过 | 1/4 | 2/4 |
| Power 严格通过 | 3/4 | 4/4 |
| 有效候选 | 8/8 | 8/8 |
| finish 调用 / 成功 | 7/8 / 7/8 | 7/8 / 7/8 |
| 产品终验 / runner 独立验收通过 | 7/8 / 7/8 | 7/8 / 7/8 |
| accepted / 全部提交 | 22/50 | 18/59 |
| Schema 拒绝 / 全部提交 | 20/50 | 32/59 |
| Schema 拒绝涉及 trial | 6/8 | 6/8 |
| worker 错误 / 提交 | 0/50 | 3/59 |
| worker 错误 / 独立测试 | 0/22 | 2/21 |
| parse-only 成功 / 独立测试 | 21/22 | 17/21 |
| 裸管道参数门禁 / 独立测试 | 0/22 | 1/21 |
| SystemExit | 0 | 0 |

LLDP 共 46 次提交：9 次 accepted、29 次 Schema 拒绝、3 次 worker 错误；
12 次独立测试中有 2 次 worker 错误。Power 共 13 次提交：9 次 accepted、
3 次 Schema 拒绝、0 次 worker 错误；9 次独立测试无 worker 错误。
受影响提交计数按一次提交是否包含该错误统计，不按重复 issue 条数放大。

| Trial | 提交 / 测试 / 轮数 | 首候选秒数 | 请求秒数 | 严格结果 |
| --- | --- | --- | --- | --- | --- |
| L1 | 12 / 3 / 16 | 1279.541 | 1390.703 | finish，但内容差异 |
| L2 | 4 / 3 / 8 | 854.291 | 867.344 | 通过 |
| L3 | 12 / 3 / 16 | 990.016 | 1458.031 | 通过 |
| L4 | 18 / 3 / 21 | 1036.537 | 1036.547 | 提交上限，未 finish |
| P1 | 3 / 3 / 7 | 398.862 | 590.234 | 通过 |
| P2 | 4 / 2 / 7 | 217.666 | 615.797 | 通过 |
| P3 | 4 / 3 / 8 | 203.846 | 499.157 | 通过 |
| P4 | 2 / 1 / 4 | 312.094 | 378.969 | 通过 |

首候选使用 Laminar 首 accepted TOOL 结束与 trial.started_at 的时间差；请求耗时
来自本地指标，两种时钟口径有小幅差异。各 trial 编号不是历史组的配对样本。
L3 严格成功使用了超过产品默认的时间、轮数及提交额度，因此本轮结果不能直接外推
为默认预算下的准确率。

## Trace 中的保真行为与局限

本轮在 Laminar UI 检查了全部 **4 条 LLDP、46 次提交**中的
`/neighbors/*/vlan_id` 捕获写法，结合受控 records 存在计数及最后候选审阅。
这不等于对全部提交的每个字段进行了完整归因。

| 目标字段现象 | 受影响 trial / 提交 |
| --- | --- |
| 明确排除该状态值 | 0/4、0/46 |
| 将该值前导符号移入模板字面量 | 0/4、0/46 |
| 缩减模板时未写目标字段捕获 | 1/4、13/46 |
| 模板中有目标字段捕获 | 4/4、33/46 |
| 最后 accepted 候选保留完整目标状态字符串 | 4/4、4/4 最后候选 |

L1、L2、L3 的全部 28 次提交均保留目标字段的直接字符串捕获。L4 在
S1/S2/S8/S17/S18 有目标捕获，其余 13 次提交缩减了模板、同时遗漏其他字段。
不能把后一类缺失归为按状态含义主动省略，也不能从“写了捕获”推断 records 一定
包含该字段：仅 35 次提交有可比较的完整 neighbors records，其中 22 次所有现有
neighbors 均包含该键；其他无可比较结果的提交不按零值计算。

历史三个已 finish 的 LLDP 失败最终候选分别存在目标符号清洗或状态排除。本轮
这两个具体写法没有再出现，支持目标行为在本组样本中改善。该比较不能证明提示
已经普遍解决其他状态字段遗漏。

**L1 仍在明知缺失时 finish。** 最后 S12 accepted 后，模型明确注意到
`/neighbors/*/management_addresses/status` 和 `/total_entries_displayed` 在原文
有对应内容而结果缺失。它认为嵌套起点与根尾字段无法兼顾，反复讨论后选择保留
当前候选并 finish。目标 vlan_id 已保真，剩余错误属于其他字段的捕获和结束决策；
其推理仍包含以可选性和实现困难接受缺失的行为。未在本轮复放候选或穷举严格差异，
不声称这两项就是全部差异。

**L4 首候选出现得太晚。** S18 首次 accepted，并保留目标状态值，但
`remaining_submissions=0`，现有规则立即以 `ttp_submission_limit` 终止。未执行
finish、产品终验或严格候选评分，不能称其为严格正确结果，也不能仅靠放行 finish
保证正确。三次提交 worker 错误均在 L4 的 S1/S10/S12，UI 中受控异常类别为
`error`，不是 SystemExit；具体正则机制没有在本轮做新的合成复现，暂不进一步归因。

Power 本轮 4/4 严格通过，但没有针对 Power 的产品改动或完整修正链归因，不能把
相对历史多一次通过直接归因于状态值提示。

## 代价、上下文与判断

| 指标 | 历史 v36 扩大预算 | v37 |
| --- | --- | --- |
| 平均请求秒数 | 811.25 | 854.60（+5.34%） |
| LLDP 平均秒数 | 973.67 | 1188.16 |
| Power 平均秒数 | 648.83 | 521.04 |
| 整批墙钟秒数 | 2389.66 | 1897.88 |
| 首次提交平均秒数，8 次观测 | 367.03 | 353.37 |
| 首候选平均秒数，8 次观测 | 591.17 | 661.61 |
| 输入 Token | 5,606,941 | 6,875,300（+22.62%） |
| 输出 Token | 730,246 | 779,794（+6.79%） |
| 推理 Token | 704,339 | 746,839（+6.03%） |
| 推理 / 输出 Token | 96.45% | 95.77% |
| usage / 推理观测 | 79/79 | 87/87 |
| 原生压缩触发 / 完成 | 0 / 0 | 0 / 0 |
| 旧提交结果折叠 | 42 | 51 |

推理 Token 包含在输出 Token 内，不能重复加总；上述用量是已观测值，不作账单
保证。整批墙钟受并发调度及各 trial 分布影响，不能用它代替平均请求耗时。

sidecar 与 8/8 trial 的 Trace UUID 唯一对应，无遗漏、重复或未关联；87/87 实际
请求保留初始任务，检查前后工具配对与顺序完整，无显式 tool_choice 或
reasoning_content 发送。估算计数最大 18,095，formatter 近似计数最大 20,060，
均不是 provider tokenizer。初始任务完整不代表所有历史候选材料完整，旧提交
反馈仍按原规则折叠。

合成机制验证成立，目标字段的具体错误写法在本组样本中未复现，严格分数增加两次
通过；但 Schema/worker 错误和 Token 增加，L1 仍接受已知字段缺失，L4 仍被提交
上限阻断。8 次非配对历史对照不足以证明稳定提升。本轮保留 v37，不自动追加
根起点、历史诊断、finish 或预算优化。

## 证据索引

本地 `.artifacts/string-value-v37/` 保存标准数值结果、`comparison.json/.md`、
`mechanisms.json`、`estimator.json`、`trace-facts.json` 和 `ui-review.json`。
SQL 限定时间与 Trace UUID，只投影受控状态、计数、路径、类型和标识；完整 Trace
内容仅在 Laminar UI 审阅，没有导出输入、Schema、模板、records、模型文本或
内容哈希，没有向产品模型回灌 Trace。

| Trial | Trace UUID |
| --- | --- |
| L1 | `a9b8062c-450b-7607-693e-b779291615ac` |
| L2 | `0ed589a5-13e5-b8be-0a52-6082adfa537e` |
| L3 | `323574c9-a88a-e47f-f8a4-43e339abfa72` |
| L4 | `4f91ce96-8522-be76-fac3-0d8398841f63` |
| P1 | `1c61f579-b7db-11b2-870a-3ddf1d61c143` |
| P2 | `c46774c0-7217-e3bb-3f69-a764271d6efc` |
| P3 | `99a45075-e72c-4fa9-9825-08a5ecd3fda4` |
| P4 | `5925f5d6-bb1a-0a46-ef83-00ee21c852fb` |

关键 UI 证据：

- [L1 最终复核及已知缺失后的 finish](http://127.0.0.1:5667/project/2c7d5e70-70e6-4b53-932f-d88e7a604382/traces/a9b8062c-450b-7607-693e-b779291615ac?spanId=00000000-0000-0000-1dda-ce8da4cca256)。
- [L2 严格成功的最终提交](http://127.0.0.1:5667/project/2c7d5e70-70e6-4b53-932f-d88e7a604382/traces/0ed589a5-13e5-b8be-0a52-6082adfa537e?spanId=00000000-0000-0000-918c-69ec7d976132)。
- [L4 末次 accepted 与零剩余提交](http://127.0.0.1:5667/project/2c7d5e70-70e6-4b53-932f-d88e7a604382/traces/4f91ce96-8522-be76-fac3-0d8398841f63?spanId=00000000-0000-0000-ee5e-0b8536e6b88b)。
