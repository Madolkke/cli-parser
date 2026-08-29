# 运行与分析

先进行离线检查：

```powershell
uv run python scripts/run_test_sets.py preflight --manifest evals/test_sets/manifest.json
uv run python scripts/run_test_sets.py run --manifest evals/test_sets/manifest.json --mode baseline --suite smoke
```

需要真实模型时，只运行 TTP-only：

```powershell
uv run --env-file .env python scripts/run_test_sets.py run `
  --manifest evals/test_sets/manifest.json --mode ttp-only `
  --suite smoke --trials 1 --concurrency 1
```

每个 trial 使用标准 Schema 和全部输入，随后独立全文验收并比较 expected records。
Laminar 仅作为可选 Trace 通道，strict pass 不依赖遥测完整性。运行产物写入被忽略的
`.artifacts/test-set-evaluation/`，按 case、输入和终止原因分析结果，不把模型文本或
原始输入复制到 summary。
