# 四件套格式

`evals/datasets.toml` 中的每个 `name` 都必须对应一个独立目录，且只能包含：

```text
inputs/001.txt ... inputs/005.txt
schema.json
template.ttp
expected.json
```

TOML 注册表示例：

```toml
[[dataset]]
id = 1
name = "linux.ip_route_show"
command = "ip route show"
platform = "linux"
source = "source-name"
tags = ["linux"]
default_input = "inputs/001.txt"
inputs = [{ file = "inputs/001.txt", sha256 = "<sha256>" }]
template = { file = "template.ttp", sha256 = "<sha256>" }
schema = { file = "schema.json", sha256 = "<sha256>" }
expected = { file = "expected.json", sha256 = "<sha256>" }
```

`expected.json` 必须是按输入顺序排列的 object 数组。Loader 会校验受限 Schema、输入
数量、文件名、UTF-8/BOM、SHA-256，并在隔离 TTP 进程中确认标准模板与默认回显的同索引 expected record 完全一致。`default_input` 未登记时默认范围为 pending；使用 `--input-scope full` 检查全部输入。
不得使用旧的 `target`、`schema_contract` 或外部文件双格式。
