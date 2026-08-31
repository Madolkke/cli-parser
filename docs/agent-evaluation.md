# Agent 评测边界

评测输入统一为 `evals/test_sets/` 下的四件套测试集。每个测试集固定包含
`inputs/`、`schema.json`、`template.ttp` 和 `expected.json`；输入按 `001.txt` 到
`005.txt` 排列，所有输入共享同一个标准 Schema、模板和 expected records。根
唯一注册表 `evals/datasets.toml` 负责索引、标签和 SHA-256；目录实际状态决定数据集是 inputs-only、template 还是 complete。

加载器和确定性校验位于 `src/cli_parser_agent/evaluation.py`，统一入口是
`scripts/run_test_sets.py`。加载器严格检查 UTF-8/BOM、重复 JSON 键、路径越界、哈希、
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

运行产物写入 `.artifacts/test-set-evaluation/<run-id>/`，可能包含模板、records 和模型
输出，应按本地敏感调试数据处理。入口可以保存脱敏配置指纹和可选 Trace ID，不把 API Key
写入 summary。完整两阶段 Schema Agent 评测不属于本入口。

旧的 `evals/ttp_generation/`、`target/schema_contract` 双格式、
`run_agent_evaluation.py` 和 `run_ttp_template_evaluation.py` 不再是评测路径。没有标准
expected records 的临时排查可以使用专用单次 TTP 诊断脚本，但不得作为标准测试执行。

只有 complete 阶段、指定了 `default_input` 且通过 preflight/baseline 的数据集才进入默认
strict TTP-only 统计；缺少默认回显的完整数据集在该范围会作为 pending。`inputs-only` 和
template 阶段同样会作为 pending 或 smoke 结果单独报告。标签可用
`--tag` 过滤，未指定过滤条件时运行 TOML 注册表中的全部数据集。
