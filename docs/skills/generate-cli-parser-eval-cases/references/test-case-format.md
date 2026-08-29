# 四件套格式

相对于 `evals/test_sets/manifest.json` 的每个 `path` 都必须是一个独立目录，且只能包含：

```text
inputs/001.txt ... inputs/005.txt
schema.json
template.ttp
expected.json
```

manifest 条目示例：

```json
{
  "id": "linux.ip_route_show",
  "path": "linux.ip_route_show",
  "command": "ip route show",
  "suites": ["smoke", "all"],
  "tags": ["linux"],
  "files": {
    "schema": {"sha256": "<sha256>"},
    "template": {"sha256": "<sha256>"},
    "expected": {"sha256": "<sha256>"},
    "inputs": [{"name": "001.txt", "sha256": "<sha256>"}]
  }
}
```

`expected.json` 必须是按输入顺序排列的 object 数组。Loader 会校验受限 Schema、输入
数量、文件名、UTF-8/BOM、SHA-256，并在隔离 TTP 进程中确认标准模板与 expected 完全一致。
不得使用旧的 `target`、`schema_contract` 或外部文件双格式。
