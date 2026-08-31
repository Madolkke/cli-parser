# 四件套 TTP-only 测试

`evals/test_sets/` 是唯一的评测输入格式。每个测试集目录完全独立，固定包含：

```text
<case-id>/
  inputs/001.txt ... 005.txt
  schema.json
  template.ttp
  expected.json
```

`inputs/` 保存同一命令的 `1-5` 份 UTF-8 回显，`schema.json` 是共享的受限
Draft 2020-12 Schema，`template.ttp` 是项目维护的确定性基线模板，`expected.json`
是按输入顺序排列的 records 数组。根 `evals/datasets.toml` 只保存 ID、命令、平台、标签和
四类文件的 SHA-256，不承载业务内容。可选 `default_input` 指向 `inputs/` 中已登记的一份
回显，用于默认单输入测评。

## 命令

```powershell
uv run python scripts/run_test_sets.py list --registry evals/datasets.toml
uv run python scripts/run_test_sets.py preflight --registry evals/datasets.toml
uv run python scripts/run_test_sets.py run --registry evals/datasets.toml --mode baseline
uv run python scripts/run_test_sets.py run --registry evals/datasets.toml --mode baseline --input-scope full
uv run --env-file .env python scripts/run_test_sets.py run --registry evals/datasets.toml --mode ttp-only --dataset linux.ip_route_show --trials 1 --concurrency 1
```

`list`、`preflight` 和 `baseline` 不读取模型配置、不初始化 Laminar、不联网。
默认范围的 `baseline` 只验证标准模板能从 `default_input` 确定性地产生同索引的 expected
record。`ttp-only` 固定注入标准 Schema 和该回显，调用公共
`TtpGenerator.generate_from_schema()`，不运行完整两阶段 Agent。使用 `--input-scope full`
可执行所有输入的回归；未登记 `default_input` 的数据集在默认范围为 pending。

严格通过要求生成成功、Agent 外全文验收通过，并且 records 与 expected records 深度全等。
对象键顺序忽略；数组顺序、标量类型、缺失字段、`null` 和空字符串严格区分。评分保留
records exact、逐输入通过率、叶子 precision/recall/F1、TTP 轮次、提交次数、终止原因、
耗时和可选 Trace ID；不计算 Schema 质量分数。

`ttp-only` 产物写入 `.artifacts/test-set-evaluation/<run-id>/`，可能包含模型生成的模板和
解析值，按本地敏感调试数据处理。退出码为：全部通过 `0`，正常但有失败 `1`，注册表、
配置或 preflight 错误 `2`，人工取消 `130`。

旧的 `evals/ttp_generation/`、外部 `target/schema_contract` 格式以及旧评测脚本不再参与
测试执行。需要没有 GT 的单次排查，继续使用专用诊断脚本，但它不属于标准测试集入口。

## 语义化标准

语义化 record 表达命令输出中的业务实体，而不是把原始行转存到 `lines[].text`。
字段使用 ASCII `snake_case`，值保持原文；重复实体进入有业务名称的数组，对象层级反映
真实归属。表头、分隔线和提示信息不进入结果，缺失的可选字段省略，数组保持原始顺序。

当前 `evals/test_sets/` 中的数据由 `evals/datasets.toml` 登记。新的测试集必须按照
[`evals/standard-test-dataset.md`](../evals/standard-test-dataset.md) 从原始回显重新制作，
通过 preflight 和 baseline 后才能进入 complete 严格评测。旧的 Schema、模板、
expected records 和历史目标记录不作为新数据集的来源。
