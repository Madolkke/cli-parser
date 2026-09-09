# v37 Easy / Medium 四重复回归

2026-09-10（北京时间）完成 8 个用例各 4 次、全局并发 4 的标准 TTP-only
回归，共 32 次。严格通过 **28/32（87.5%）**：Easy **12/16（75%）**，
Medium **16/16（100%）**。四次失败全部来自 Broadcom 版本信息用例，均为
生成及 Schema 验收通过后的 records 内容差异。

## 配置与范围

从干净提交 `01ed9aa` 启动，产品仍为 v37 状态字符串保真实现。模型、全部 policy
与上一轮 [v37 Hard 回归](string-value-v37-regression.md)逐字段一致：

- deepseek-v4-flash，temperature 0，非流式，max_tokens 8192，context 128000。
- HTTP timeout 120 秒、模型重试 2，TLS 校验开启，无 extra_body。
- 单次生成 1800 秒、26 轮、18 次提交、3 次独立测试。
- 提示 `ttp-generator-v37-string-value-fidelity-zh-cn`；Thinking 计数修正默认启用。
- 全部用例仅使用登记的默认 `inputs/001.txt`，每个 4 次，并发上限 4。

未修改产品、默认预算、输入、Schema、标准模板或 expected，未补跑失败 trial。
离线默认输入范围 baseline 8/8 通过，标准入口在真实运行前再次执行 preflight。
已有产品离线验收为 875 passed，见 v37 Hard 报告，本轮未重复无代码变更的全量 pytest。

标准 runner 多个 `--tag` 取交集，Easy 与 Medium 因此用明确的数据集 ID 并集选取：

```powershell
$env:CLI_PARSER_MODEL_TIMEOUT_SECONDS='120'
$env:CLI_PARSER_GENERATION_TIMEOUT_SECONDS='1800'
$env:CLI_PARSER_MAX_AGENT_ITERS='26'
$env:CLI_PARSER_MAX_TEMPLATE_SUBMISSIONS='18'
$env:CLI_PARSER_MAX_TTP_TEST_CALLS='3'
$env:CLI_PARSER_TEST_SET_ARTIFACT_ROOT='.artifacts/easy-medium-v37/evaluation'
uv run --env-file .env python scripts/context_ablation.py run --variant estimator --diagnostics .artifacts/easy-medium-v37/estimator.json -- run --registry evals/datasets.toml --mode ttp-only --dataset-id 1 --dataset-id 3 --dataset-id 5 --dataset-id 6 --dataset-id 7 --dataset-id 9 --dataset-id 10 --dataset-id 11 --trials 4 --concurrency 4 --trace-rounds
```

使用与产品行为一致的 estimator 观察适配器，委托唯一标准评测入口；本轮目录：
`.artifacts/easy-medium-v37/evaluation/20260909T163601.410863Z`。

## 分用例结果

| 难度 | 用例 | 严格通过 | 提交 / 独立测试 | 平均秒数 |
| --- | --- | --- | --- | --- |
| Easy | broadcom_icos.show_version | 0/4 | 6 / 7 | 230.67 |
| Easy | fortinet.get_system_status | 4/4 | 4 / 1 | 42.19 |
| Easy | paloalto_panos.show_interface_hardware | 4/4 | 4 / 0 | 59.92 |
| Easy | oneaccess_oneos.show_voice_mos | 4/4 | 4 / 2 | 108.96 |
| Medium | cisco_s300.show_lldp_neighbors | 4/4 | 6 / 1 | 66.95 |
| Medium | hp_procurve.show_interfaces_status | 4/4 | 4 / 2 | 113.95 |
| Medium | cisco_nxos.show_interface_status | 4/4 | 4 / 0 | 102.07 |
| Medium | cisco_ios.show_sdwan_control_connections | 4/4 | 4 / 0 | 67.59 |

全部 32 次都有有效候选，成功调用 finish，通过产品终验及 runner 独立验收。
36/36 次模板提交均 accepted，13/13 次独立测试解析成功；Schema 拒绝、worker
错误、SystemExit 和裸管道门禁拒绝均为零。严格通过还要求 records 与 expected
深度全等，不能用上述流程成功率代替。

Broadcom 四次均在正常 finish 后被严格评分判为 records 差异，未出现预算或
模型调用终止失败。仅对首条失败 Trace 作了初步 UI 审阅，未穷举四条 Trace 的
字段差异，也未复放候选；具体错误机制待后续诊断，本报告不作因果归因。

## 代价与上下文

| 指标 | 结果 |
| --- | --- |
| 整批墙钟 | 853.31 秒，约 14.22 分钟 |
| 平均单次请求 | 99.04 秒 |
| 首次提交平均时间 | 74.12 秒，32 次观测 |
| 首个有效候选平均时间 | 77.23 秒，32 次观测 |
| LLM 调用 / usage 观测 | 81 / 81 |
| 输入 Token | 1,393,450 |
| 输出 Token | 382,228 |
| 其中推理 Token | 367,255（96.08%） |
| 原生压缩触发 / 完成 | 0 / 0 |
| 旧提交结果折叠 | 4 次 |

推理 Token 包含在输出 Token 中；用量为已观测统计，不作账单保证。首候选时间
来自 Laminar TOOL 结束与 trial 开始的时间差，请求耗时来自本地指标，口径略有差异。

sidecar 完整关联 32/32 trial，无重复、缺失或未关联 Agent。81/81 实际请求保留
初始任务，工具配对和顺序完整，没有显式 tool_choice 或 reasoning_content 发送。
这只证明初始任务保留，不证明全部历史候选材料完整。估算计数最大 11,613，
formatter 近似计数最大 12,426，均非 provider tokenizer。

本轮是当前 v37 的范围回归，未运行同范围旧版对照，不能据此判断 Broadcom 问题
是由 v37 新引入，或断言其他用例的稳定通过率。结论适用于默认单输入范围，
不外推为 full-scope 的全部输入准确率。

## 证据

`.artifacts/easy-medium-v37/` 中的标准运行目录、`aggregate.json`、
`mechanisms.json` 和 `estimator.json` 仅保存既有允许的结构事实、数值和标识。
Laminar SQL 仅按本轮 Trace UUID 和时间范围聚合统计，无 Trace 正文、模板、
records、模型文本或内容哈希导出，不回灌产品模型。

失败 Trace 标识保存在标准 trial 文件及 aggregate 中；
[Broadcom 首次失败 Trace](http://127.0.0.1:5667/project/2c7d5e70-70e6-4b53-932f-d88e7a604382/traces/6258d332-620e-78ab-b336-3fd75ea5f1ba)
可用于后续只读诊断。本轮仅记录回归结果，不自动实施修复。
