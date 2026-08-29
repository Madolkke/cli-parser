# 标准测试集格式与接入方案

本目录只保存开发期评测数据，不进入 Python 包，也不作为产品运行时依赖。每个测试集必须能够脱离其他测试集独立审查和执行。

## 数据集组成

标准测试集使用固定的四件套目录：

```text
evals/test_sets/<case-id>/
├── inputs/
│   ├── 001.txt
│   └── 002.txt
├── schema.json
├── template.ttp
└── expected.json
```

每个测试集表示同一条命令、同一种输出结构。`inputs/` 中必须有 1-5 份纯命令回显，文件名从 `001.txt` 开始连续编号。输入不应包含命令本身、终端提示符或人为添加的说明；每份非空白输入必须是 UTF-8、禁止 BOM，且不超过 1 MiB。

`schema.json` 描述单份输入对应的一个结果 record，必须是项目支持的受限 Draft 2020-12 Schema：

- 根节点必须是封闭的 `object`；
- 字段使用 ASCII `snake_case`，Python 关键字可以作为字段名；
- `properties` 默认可选，只有确实在每个实体中出现的字段才放入 `required`；
- 重复实体使用语义数组，数组顺序保持输入顺序；
- 保留原始字符串值，不擅自翻译、大小写转换、单位换算或数值化；
- 不使用 `lines`、`raw_lines` 或其他整行兜底字段；
- 不包含 Evidence、assumptions、模型元数据或评测诊断字段；
- 禁止项目未开放的关键字、远程引用、组合 Schema 和动态内容。

`template.ttp` 是维护者编写的确定性基线模板。它只用于验证 Schema、输入和 `expected.json` 彼此一致，不要求被测 Agent 生成相同文本。模板必须通过 TTP 安全预检，使用字段级捕获，明确处理实体边界、可变空白、可选字段、表头和分隔线。

`expected.json` 必须是 JSON 数组，元素数量与输入数量相同，元素按输入顺序排列且每个元素都是根 `object`。它只能根据原始输入人工核对生成，不能读取模型产物、Trace、历史 artifact、上游模板或其他参考答案。对象键顺序不参与比较，但数组顺序、字段缺失、空字符串、`null`、类型和值都严格比较。

## 根 manifest

`evals/test_sets/manifest.json` 只负责索引，不承载测试集业务数据。每个条目包含：

```json
{
  "version": 1,
  "cases": [
    {
      "id": "vendor.platform.command",
      "path": "vendor.platform.command",
      "command": "show example",
      "suites": ["smoke"],
      "tags": ["key-value-lines"],
      "files": {
        "schema": {"sha256": "..."},
        "template": {"sha256": "..."},
        "expected": {"sha256": "..."},
        "inputs": [
          {"name": "001.txt", "sha256": "..."}
        ]
      }
    }
  ]
}
```

`path` 相对于 manifest 所在目录解析。加载器会拒绝路径越界、重复 ID、重复路径、缺少或多余文件、文件名不连续、无效 SHA-256、非法 UTF-8、BOM、重复 JSON 键、输入数量越界以及不符合受限 Schema 的 records。

测试集目录只能包含 `inputs/`、`schema.json`、`template.ttp` 和 `expected.json` 四项。不要在目录中保存模型生成结果、Trace 原文、凭据、临时日志或评测报告；运行产物放在被忽略的 `.artifacts/` 下。

## 建集流程

1. 收集同一平台、同一命令和同一种输出结构的原始回显。结构不兼容时拆成不同的 `case-id`，不要强行共用 Schema。
2. 逐份阅读输入，标记实体边界、稳定字段、可选字段、重复实体和多行关系。
3. 仅根据输入编写语义化 Schema。先确定根对象和数组边界，再决定字段是否 required；字段不足时保守使用 `string`。
4. 仅根据输入人工编写 expected records，逐输入核对字段值、字段缺失和数组顺序。
5. 编写维护者基线 TTP 模板，避免整行捕获和过宽正则；对表头、分隔线、提示符和无法稳定归属的内容显式忽略。
6. 运行 preflight。preflight 会校验文件安全性、Schema、expected records、模板白名单、输入映射和模板基线可执行性。
7. 运行 baseline，确认标准模板对每份输入产生的 records 与 expected 完全一致。baseline 失败时先修复四件套，不启动模型评测。
8. 对四类文件计算 SHA-256，更新根 manifest，并再次运行 preflight。
9. 通过 `ttp-only` 入口评估 Agent。该模式固定注入标准 Schema 和全部输入，只调用公共 `generate_from_schema()`，不运行 Schema Agent；评分只看 Agent 外最终验收和 records 与 expected 的严格比较。

## 运行入口

```text
uv run python scripts/run_test_sets.py list --manifest evals/test_sets/manifest.json
uv run python scripts/run_test_sets.py preflight --manifest evals/test_sets/manifest.json
uv run python scripts/run_test_sets.py run --manifest evals/test_sets/manifest.json --mode baseline --suite smoke
uv run --env-file .env python scripts/run_test_sets.py run --manifest evals/test_sets/manifest.json --mode ttp-only --case vendor.platform.command --trials 3 --concurrency 1
```

`list` 和 `preflight` 不读取模型配置、不联网、不初始化 Laminar。`baseline` 是离线确定性校验。`ttp-only` 才会访问模型；每个 trial 使用独立生成器，建议在分析收敛、轮次、延迟或候选质量时使用 `--concurrency 1`。

严格通过条件是：生成成功、独立最终验收通过、record 数量正确，并且 records 与 expected 深度全等。叶子 precision/recall/F1、首个有效候选、finish、轮次和耗时仅作为诊断指标，不能替代 strict pass。

## 审查清单

- 输入确实来自同一命令且格式兼容；
- 没有把表头、分隔线、提示符或整行文本当成业务字段；
- 每个字段有稳定且可解释的语义名称；
- required 判定基于所有输入中的实际出现情况；
- 缺失字段被省略，而不是填入 `null` 或空字符串；
- 重复实体未被去重，数组顺序与原始输入一致；
- expected 没有来自模型、Trace、历史 artifact 或上游模板的内容；
- baseline 与 expected 完全一致；
- manifest 哈希在最后一次修改后重新计算；
- 测试资产中没有 API Key、Authorization、Trace 原文或临时产物。

## 当前状态

现有测试数据已清空，等待重新提供原始输入。重新接入时应从新的 `evals/test_sets/` 和 manifest 开始，不能复用旧的 expected、Schema、模板或历史目标记录。
