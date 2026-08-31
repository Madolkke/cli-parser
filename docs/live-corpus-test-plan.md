# 标准测试集运行计划

公开命令输出曾整理到 `evals/test_sets/`，但当前测试数据已按要求清空，等待重新提供。
旧的 `testdata/real_command_outputs/` 语料目录和 `run_live_corpus.py` 入口已删除，不再存在
单输入 corpus 与四件套之外的运行格式。重新导入时，每个测试集必须独立包含实际存在的
`inputs/001.txt` 到 `005.txt`、`schema.json`、`template.ttp` 和 `expected.json`。

2026-08 重新接入第一批数据集（4 个目录、17 份回显，见 `evals/datasets.toml`）：
`broadcom_icos.show_version`（5）、`cisco_ios.show_lldp_neighbors_detail`（5）、
`fortinet.get_system_status`（3）、`huawei_vrp.display_port_vlan`（4 + 维护者基线
`template.ttp`）。原始回显拷贝自
[ntc-templates](https://github.com/networktocode/ntc-templates)（Network to Code，Apache-2.0）
测试语料，仅重命名编号、未修改内容。它们尚无完整四件套，不进入 strict baseline 或 ttp-only；
补齐 schema/expected 后按 `evals/standard-test-dataset.md` 的建集流程接入。

## 离线验收

```powershell
uv run python scripts/run_test_sets.py list --registry evals/datasets.toml
uv run python scripts/run_test_sets.py preflight --registry evals/datasets.toml
uv run python scripts/run_test_sets.py run --registry evals/datasets.toml --mode baseline
uv run python scripts/run_test_sets.py run --registry evals/datasets.toml --mode baseline --input-scope full
```

这些命令不读取模型配置、不初始化 Laminar、不联网。Preflight 严格检查 TOML 注册表、路径
越界、文件命名、SHA-256、BOM、UTF-8、输入数量、受限 Schema、expected records 数量和
默认范围的逐条 Schema 合法性，并在隔离 TTP 进程中确认标准模板能复现默认回显的同索引 expected record。`--input-scope full` 才检查全部 expected records。

## TTP-only Agent 验收

```powershell
uv run --env-file .env python scripts/run_test_sets.py run `
  --registry evals/datasets.toml --mode ttp-only `
  --trials 1 --concurrency 1
uv run --env-file .env python scripts/run_test_sets.py run `
  --registry evals/datasets.toml --mode ttp-only `
  --trials 1 --concurrency 1
```

该模式只调用 `TtpGenerator.generate_from_schema()`，不运行 Schema Agent。每个 trial 使用
测试集的标准 Schema、登记的默认回显及同索引 expected record，之后进行 Agent 外全文验收，并严格比较 records。使用 `--input-scope full` 可执行全部输入回归。标准
模板只作为确定性基线，不要求 Agent 模板文本相同。

报告包括 case/input 通过率、records exact、叶子 precision/recall/F1、TTP 轮次、提交
次数、首个有效候选、终止原因、耗时和可选 Trace ID。模型生成的模板和 records 写入被
忽略的 `.artifacts/test-set-evaluation/`，输入原文只保留在四件套目录中。

退出码为：全部通过 `0`，正常完成但有失败 `1`，定义/配置/preflight 错误 `2`，人工取消
`130`。需要无 GT 的一次性排查时使用 `run_ttp_phase_once.py`，但它不属于标准测试集
评测入口。
