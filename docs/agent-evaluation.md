# Agent 评测边界

评测输入统一为 `evals/test_sets/` 下的四件套测试集。每个测试集固定包含
`inputs/`、`schema.json`、`template.ttp` 和 `expected.json`；输入按 `001.txt` 到
`005.txt` 排列，所有输入共享同一个标准 Schema、模板和 expected records。根
唯一注册表 `evals/datasets.toml` 负责索引、标签和文件路径；目录实际状态决定数据集是 inputs-only、template 还是 complete。

加载器和确定性校验位于 `src/cli_parser_agent/evaluation.py`，统一入口是
`scripts/run_test_sets.py`。加载器严格检查 UTF-8/BOM、重复 JSON 键、路径越界、
输入数量、Schema 受限子集、expected records 与模板基线。标准模板必须在隔离 TTP 解析后
默认范围内对 TOML 中显式指定的单份 `default_input` 产生与同索引
`expected.json` record 完全一致的 records。`--input-scope full` 才验证全部输入。

## 两种运行模式

```powershell
uv run python scripts/run_test_sets.py list --registry evals/datasets.toml
uv run python scripts/run_test_sets.py preflight --registry evals/datasets.toml
uv run python scripts/run_test_sets.py run --registry evals/datasets.toml --mode baseline
uv run python scripts/run_test_sets.py run --registry evals/datasets.toml --mode baseline --input-scope full
uv run --env-file .env python scripts/run_test_sets.py run --registry evals/datasets.toml --mode ttp-only --trials 1 --concurrency 1
```

`list`、`preflight` 和 `baseline` 完全离线，不读取模型配置、不初始化 Laminar。`baseline`
只验证维护者提供的标准 TTP 模板可执行且结果正确，它不要求 Agent 生成相同的模板文本。

默认 `ttp-only` 将标准 Schema 和一份已登记的默认回显传给一次独立的
`TtpGenerator.generate_from_schema()`，随后执行 Agent 外全文验收和严格 records 评分。
它不调用 `generate()`，不运行 Schema Agent，也不把标准模板字符串相似度作为得分。
默认 `trials=1`、`concurrency=1`；模型与预算从环境变量读取，高预算只能由人工显式配置。

严格通过条件是：生成成功、独立验收通过、records 数量和输入索引一致、records 与
`expected.json` 深度全等。对象键顺序忽略；数组顺序、类型、缺失字段、`null` 和空字符串
严格区分。报告保留 records exact、逐输入通过率、叶子 precision/recall/F1、TTP 轮次、
提交次数、首个有效候选、终止原因、耗时和可选 Laminar Trace ID。Schema 质量不作为本入口
的分数。

## 资产边界

标准 Schema 和 expected records 必须由维护者从 raw 回显独立核对，不能读取被测 Agent
结果、Trace、历史 artifact、上游模板或模型生成答案。标准 TTP 模板是可审查的确定性基线，
用于确认四件套自身闭环；TTP-only Agent 只按 Schema 和 expected records 评估。

运行产物写入 `.artifacts/test-set-evaluation/<run-id>/`，仅保存状态、数值评分、安全 issue code、脱敏配置及 Trace ID 等脱敏投影。模板、records、capture、原始输入和模型文本只通过显式 Laminar 通道观察；完整产物仅在内存中评分，不写入 trial 文件。完整两阶段 Schema Agent 评测不属于本入口。

runner 版本 5 始终收集安全执行事实；`--trace-rounds` 仅控制逐事件明细落盘。漏斗区分有效候选、finish 调用、finish 成功及最终验收，缺少观测时省略数值指标而非填写零。候选轨迹只有可证实的时间顺序才判定有效提交发生于成功 finish 之前，否则报告未知。严格评分与遥测完整性独立，正确率 baseline 格式保持版本 1。配置记录模型重试次数、TLS 校验开关和 `extra_body` 是否配置，不包含凭据或请求扩展正文。

旧的 `evals/ttp_generation/`、`target/schema_contract` 双格式、
`run_agent_evaluation.py` 和 `run_ttp_template_evaluation.py` 不再是评测路径。没有标准
expected records 的临时排查可以使用专用单次 TTP 诊断脚本，但不得作为标准测试执行。

只有 complete 阶段、指定了 `default_input` 且通过 preflight/baseline 的数据集才进入默认
strict TTP-only 统计；缺少默认回显的完整数据集在该范围会作为 pending。`inputs-only` 和
template 阶段同样会作为 pending 或 smoke 结果单独报告。标签可用
`--tag` 过滤，未指定过滤条件时运行 TOML 注册表中的全部数据集。

逐事件明细只接受固定事件类型与项目事件名；工具名限于四个注册工具，未知值统一为 `unknown_tool`，完成原因限定为框架枚举。这个本地投影不会改变 Agent 决策或工具行为。

## 运行信息与兼容

注册表版本为 `2`，每个文件条目只有 `{ file = "..." }`。加载器仅支持该版本并继续
拒绝未知字段；旧版注册表需将版本改为 `2` 并删除文件条目中的 `sha256`。修改资产内容
后可直接重新运行 preflight 和 baseline，无需维护内容摘要。文件路径或默认输入变化时
仍需更新注册表。`.gitattributes` 保留 LF 规则，文本加载器继续将 CRLF 归一化为 LF。

| 信息 | 保存和用途 |
| --- | --- |
| 数据集与输入选择 | case 路径、输入路径、原始索引、input scope 和标签用于说明测试范围 |
| 生效配置 | 运行 summary 保存脱敏模型参数、GenerationPolicy 和 `prompt.version`；扩展参数只记录是否配置 |
| 运行来源 | Git revision、dirty 状态、运行时间和唯一 trial ID 用于定位一次运行 |
| 执行与正确性 | 执行事实、计数、数值评分、安全 issue code 与 Trace ID 分别说明流程、结果和遥测 |
| 模板修正 | 安全 submission 事件保留提交序号与模板字符数；仅凭这两个值不能判定模板正文相同 |

新报告使用 `runner_version=5`，不计算或写入资产、注册表、提示词、模板或配置摘要。
三个单次/TUI 开发脚本使用 `script_version=2`，输入元信息保留路径和字节数。
WebUI 继续保存完整的本地配置并向页面提供现有脱敏视图，持久配置版本保持 `1`。

正确率 baseline 的 `baseline_version` 保持 `1`。`--write-baseline` 导出数值记录，
`--baseline` 按 case ID、成功数、trial 数及容忍值比较；离线 `--mode baseline` 则验证
标准模板与 expected 的一致性。两者不应混淆。严格正确率仍只由独立验收和 records
深度全等决定，模板写法或文件排版相同与否不作为评分依据。

旧报告和历史 baseline 保持原样，读取时允许已有摘要字段；配置比较忽略旧的
`model.extra_body_sha256`、`prompt.schema_system_sha256` 和 `prompt.ttp_system_sha256`，
继续展示真实参数、预算与提示版本差异。`scripts/compare_accuracy_runs.py` 比较完整的
runner v5 TTP-only 目录，核对记录的输入选择、case/trial 数、模型、预算和提示版本；
它不校验历史资产内容，分析时仍须确认预期配置差异。默认只读本地数值结果；显式
`--laminar` 才按时间窗口和 Trace ID 查询聚合计数、时延与用量，不导出 Trace 正文。
缺少推理用量的 span 不计为零，报告同时给出观测率，部分观测不计算完整推理占比。

上下文策略的四组消融通过默认关闭的进程内适配器执行，仍委托唯一标准入口评分，
不改变公共 API 或生产压缩策略。使用方法、容量边界、结构诊断与离线验证见
[上下文策略消融实验](context-ablation.md)。

历史 WebUI 运行仍可打开和重执行，页面不再展示配置指纹。此前 `.artifacts/` 中的
launcher、审计脚本及报告作为历史材料保留；后续测试使用标准入口。Git 原生提交 ID
和 `uv.lock` 依赖包校验仍保留，不参与 case 或 trial 评分。
