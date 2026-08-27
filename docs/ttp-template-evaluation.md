# 可评分 TTP-only 测试

`scripts/run_ttp_template_evaluation.py` 用于固定 Schema 后比较 TTP 阶段质量。它不运行 Schema Agent，每个 trial 只调用公共 `TtpGenerator.generate_from_schema()`，随后重复 Agent 外全文验收。

## Manifest

传入的 manifest 可以位于仓库外；所有路径必须是相对 manifest 目录的 POSIX 路径，且每个文件都要提供 SHA-256：

```json
{
  "version": 1,
  "cases": [
    {
      "id": "example.values",
      "suites": ["smoke"],
      "tags": ["example"],
      "schema": {"path": "schema.json", "sha256": "<sha256>"},
      "inputs": [
        {"path": "first.raw", "sha256": "<sha256>"}
      ],
      "expected_records": {"path": "expected.json", "sha256": "<sha256>"}
    }
  ]
}
```

`schema.json` 必须是项目受限 Draft 2020-12 Schema。`expected.json` 是按输入索引排列的 JSON object 数组，长度必须等于 `inputs`，并且每个 record 都要符合该 Schema。输入数量范围为 `1-5`。

## 命令

```powershell
uv run python scripts/run_ttp_template_evaluation.py list --manifest C:\fixtures\ttp-eval\manifest.json
uv run python scripts/run_ttp_template_evaluation.py preflight --manifest C:\fixtures\ttp-eval\manifest.json
uv run --env-file .env python scripts/run_ttp_template_evaluation.py run --manifest C:\fixtures\ttp-eval\manifest.json --suite smoke --trials 1 --concurrency 1
```

`list` 和 `preflight` 不读取模型配置、不会初始化 Laminar 或发起网络请求。`run` 默认一次 trial、单并发；`--trials` 范围为 `1-10`，`--concurrency` 范围为 `1-4`。模型与预算完全使用标准环境变量，高预算诊断仍须显式设置对应的 `GenerationPolicy` 环境变量。

严格通过要求生成成功、独立全文验收通过，并且 records 与 expected records 深度全等。对象键顺序忽略；数组顺序、标量类型、缺失字段、`null` 和空字符串严格区分。

结果写入 `.artifacts/ttp-template-evaluation/<run-id>/`，其中 `cases/` 保留每个 trial 的完整 `GenerationResult`、Schema/输入/expected 文件元数据、验收和分数，`summary.json` 汇总正确率、逐输入指标、TTP 轮次、提交次数、耗时与可选 Trace ID。该目录可能含有解析值和模板，按本地敏感调试数据处理。退出码为：全部通过 `0`，正常但有失败 `1`，配置或 fixture 错误 `2`，人工取消 `130`。
