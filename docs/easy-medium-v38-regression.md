# v38 Easy / Medium 修复与回归

## 修改与离线验收

修复提交 `dd3639e`，提示版本 `ttp-generator-v38-multivalue-string-guidance-zh-cn`。
针对 [Broadcom 诊断](broadcom-v37-regression-diagnosis.md) 中的分隔符选择偏差，
将冻结 string 的 token 视觉折行、纵向多值列表和有行界语义的正文分别指导为
空分隔符、单个空格和换行。增加通用 Bundle/Choices 示例，复核条目顺序、词项
内部空格和符号及拼接分隔符；原有自由文本换行示例保留。

三个新增离线测试直接提取提示模板，经白名单、spawn worker 和 Schema 校验：
正确列表示例覆盖重复实体、缺少可选列表、多词条目、符号与尾字段；双向单因素
反例验证列表误用换行、正文误用空格均可通过 Schema，但违背独立预期。

最终状态完整离线 pytest：878 passed、3 live deselected。Ruff、格式检查和
git diff --check 通过；标准默认范围 preflight、baseline 为 10/10 complete 通过，
Huawei 保持 pending。首次全量运行中旧提示原句断言未同步，修正后先通过相关
40 项测试，再完成最终全量检查；没有修改产品以绕过失败测试。

Schema 提示和两阶段重试提示通过 AST 字符串比较确认不变。公共 API、工具协议、
解析规则、默认预算、依赖、评测资产和严格评分保持不变。Thinking 计数修正继续启用。

## 真实回归配置

2026-09-10 从干净提交启动，通过 estimator 观察适配器委托标准 run_test_sets.py。
8 个 Easy/Medium 用例各 4 次，全局并发 4，共 32 次，仅默认 `001.txt` 输入。
全部模型与 policy 字段在启动前与 v37 回归配置逐项比对一致，唯一预期差异是提示。

- deepseek-v4-flash、temperature 0、非流式、max_tokens 8192、context 128000。
- HTTP timeout 120 秒、模型重试 2、TLS 校验开启、无 extra_body。
- 总预算 1800 秒、26 轮、18 次提交、3 次独立测试。
- 数据集 ID 并集：1、3、5、6、7、9、10、11。
- 运行目录：`.artifacts/easy-medium-v38/evaluation/20260910T151138.888588Z`。

运行初期 Docker Desktop 未运行，Laminar 导出报 UNAVAILABLE；启动本地 Docker 后
既有 Laminar 容器恢复，SQL 确认新 span 已入库。初期 Trace 存在不可恢复的观测缺口，
本地严格评分与运行事实独立保存。未为补齐 Trace 重跑任何 trial。

## 结果

**严格通过 32/32：Easy 16/16，Medium 16/16。** 相比
[v37 回归](easy-medium-v37-regression.md) 的 28/32，Broadcom 从 0/4 恢复为 4/4，
其余七个用例保持各 4/4。全部 trial 都有有效候选、成功 finish、产品终验和 runner
独立验收通过；全部 records 与标准 expected 深度全等。

| 难度 | 用例 | 严格通过 | 提交 / 独立测试 | 平均秒数 |
| --- | --- | --- | --- | --- |
| Easy | broadcom_icos.show_version | 4/4 | 6 / 7 | 170.27 |
| Easy | fortinet.get_system_status | 4/4 | 4 / 0 | 19.04 |
| Easy | paloalto_panos.show_interface_hardware | 4/4 | 4 / 0 | 72.71 |
| Easy | oneaccess_oneos.show_voice_mos | 4/4 | 5 / 9 | 202.67 |
| Medium | cisco_s300.show_lldp_neighbors | 4/4 | 4 / 0 | 44.78 |
| Medium | hp_procurve.show_interfaces_status | 4/4 | 4 / 2 | 71.07 |
| Medium | cisco_nxos.show_interface_status | 4/4 | 6 / 4 | 89.75 |
| Medium | cisco_ios.show_sdwan_control_connections | 4/4 | 4 / 1 | 28.33 |

共 37 次提交、23 次独立测试、92 次模型调用；本地记录模型重试为零。由于早期
Laminar 丢失导出，SQL 仅观测到 29/32 条 Trace 的部分或全部 span：33/37 次提交、
20/23 次测试、82/92 次 LLM 调用。不能将这 29 条都视为完整 Trace。

在已观测的 33 次提交中，30 次 accepted，3 次 Schema 拒绝，worker/SystemExit/
裸管道错误均为零；20 次已观测独立测试均成功。未观测的提交和测试不补零，不能
断言全体提交均无 worker 错误。Schema 拒绝来自 NX-OS 一个 trial 的两次提交和
OneAccess 一个 trial 的一次提交，均在后续修正后严格通过，未做正文级错误归因。
相关 Trace 分别为 `cf7a80c8-7f65-ffd6-734e-ba451c5b34eb`、
`69734246-367d-977d-2c72-708bd46284d4`。

## 资源与机制

| 指标 | v37 | v38 |
| --- | --- | --- |
| 严格通过 | 28/32 | 32/32 |
| 整批墙钟秒数 | 853.31 | 737.86 |
| 平均请求秒数 | 99.04 | 87.33 |
| 提交 / 独立测试 | 36 / 13 | 37 / 23 |
| 模型调用 | 81 | 92 |
| 输入 Token（本地完整观测） | 1,393,450 | 1,928,538 |
| 输出 Token（本地完整观测） | 382,228 | 529,380 |
| 原生压缩 | 0 | 0 |
| 旧提交反馈折叠 | 4 | 5 |

输入/输出 Token 分别增加 38.40%/38.50%，因此不能声称资源效率整体改善。
日期、供应商响应速度、Docker 恢复和并行离线验收可能影响时延，不把墙钟下降
归因为提示的确定因果收益。首次提交平均 62.79 秒（32 次本地观测）；Laminar
观测到的首候选平均 85.82 秒仅覆盖 29 次，且缺失早期 span 可能高估首次时间，
不用于与历史完整观测直接比较。

已观测 82 次 LLM 的推理 Token 为 461,743，占这 82 次输出 Token 的 96.52%；
它们包含在输出 Token 内。不能作为全体 92 次调用的推理占比或完整费用依据。

独立 sidecar 完整匹配 32/32 trial，无缺失或重复。92/92 实际请求初始任务完整，
工具配对和顺序检查通过；无显式 tool_choice，无 reasoning_content 发送。
压缩触发/完成、观测省略均为零。估算 Token 最大 12,582，formatter 近似计数
最大 13,748；均非 provider tokenizer。初始任务完整不代表对全部历史候选逐字复验。

## 结论与边界

本次通用提示修复在四重复默认范围内恢复 Broadcom，并保持全部 Easy/Medium
用例通过。真实严格评分说明最终表示方式已符合标准；本轮没有导出或逐条审阅新
Trace 正文，不将结果解释成四条模型都使用了同一模板实现。

四次重复是回归证据，不保证长期正确率为 100%。本轮未运行 full-scope 或 Hard
真实回归；自由文本保留换行由离线合成测试验证，不能代替真实 Hard 无回归证据。
不继续堆叠其他提示、不补跑失败 trial，也不放宽评分。后续优先保持 Easy/Medium
作为提示变更的固定回归范围，再独立解决探索次数和 Token 成本。

完整数值与标识位于 `.artifacts/easy-medium-v38/aggregate.json`、`mechanisms.json`、
`estimator.json` 和标准运行目录；仅保存既有允许的脱敏结构事实，不增加正文或内容
哈希导出。运行结束后再次确认模型与 policy 和 v37 完全相同。
