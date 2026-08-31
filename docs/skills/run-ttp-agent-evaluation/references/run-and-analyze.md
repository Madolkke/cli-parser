# 运行与分析

先进行离线检查：

```powershell
uv run python scripts/run_test_sets.py preflight --registry evals/datasets.toml
uv run python scripts/run_test_sets.py run --registry evals/datasets.toml --mode baseline
uv run python scripts/run_test_sets.py run --registry evals/datasets.toml --mode baseline --input-scope full
```

需要真实模型时，只运行 TTP-only：

```powershell
uv run --env-file .env python scripts/run_test_sets.py run `
  --registry evals/datasets.toml --mode ttp-only `
  --tag easy --trials 1 --concurrency 1
```

默认每个 trial 使用标准 Schema、登记的 `default_input` 及同索引 expected record，随后独立全文验收并比较。需要多输入回归时显式传入 `--input-scope full`。
Laminar 仅作为可选 Trace 通道，strict pass 不依赖遥测完整性。运行产物写入被忽略的
`.artifacts/test-set-evaluation/`，按 case、输入和终止原因分析结果，不把模型文本或
原始输入复制到 summary。
