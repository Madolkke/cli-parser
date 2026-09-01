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

## TOML 注册表

`evals/datasets.toml` 是所有标准测试集的唯一注册、选择和状态入口，不承载测试集业务数据。每个条目包含元数据、输入文件哈希，以及可选的模板、Schema 和 expected 文件哈希：

```toml
version = 1

[[dataset]]
id = 1
name = "vendor.platform.command"
command = "show example"
platform = "vendor_platform"
source = "source-name"
tags = ["easy"]
default_input = "inputs/001.txt"
inputs = [{ file = "inputs/001.txt", sha256 = "..." }]
template = { file = "template.ttp", sha256 = "..." }
schema = { file = "schema.json", sha256 = "..." }
expected = { file = "expected.json", sha256 = "..." }
```

数据集目录名必须等于 `name`，且位于 `evals/test_sets/` 下；注册表中的每个目录都必须存在，目录中不允许有未登记目录。加载器会拒绝路径越界、重复 ID/名称、未知字段、缺少成对的 Schema/expected、文件名不连续、无效 SHA-256、非法 UTF-8、BOM、重复 JSON 键、输入数量越界以及不符合受限 Schema 的 records。

目录实际内容决定阶段：只有 `inputs/` 是 `inputs-only`，再有 `template.ttp` 是 `template`，具备完整四件套才是 `complete`。缺失文件报告为 pending；格式、哈希、Schema、模板或四件套一致性错误报告为 preflight failure。不要在目录中保存模型生成结果、Trace 原文、凭据、临时日志或评测报告；运行产物放在被忽略的 `.artifacts/` 下。

## 建集流程

1. 收集同一平台、同一命令和同一种输出结构的原始回显。结构不兼容时拆成不同的 `case-id`，不要强行共用 Schema。
2. 逐份阅读输入，标记实体边界、稳定字段、可选字段、重复实体和多行关系。
3. 仅根据输入编写语义化 Schema。先确定根对象和数组边界，再决定字段是否 required；字段不足时保守使用 `string`。
4. 仅根据输入人工编写 expected records，逐输入核对字段值、字段缺失和数组顺序。
5. 编写维护者基线 TTP 模板，避免整行捕获和过宽正则；对表头、分隔线、提示符和无法稳定归属的内容显式忽略。
6. 运行 preflight。preflight 会校验文件安全性、Schema、expected records、模板白名单、输入映射和模板基线可执行性。
7. 选择经人工审阅的 `default_input` 并登记到 TOML。运行默认 baseline，确认标准模板对该回显产生的 record 与同索引 expected 完全一致；使用 `--input-scope full` 再验证所有输入。baseline 失败时先修复四件套，不启动模型评测。
8. 对四类文件计算 SHA-256，更新 `evals/datasets.toml`，并再次运行 preflight。
9. 通过 `ttp-only` 入口评估 Agent。默认模式固定注入标准 Schema、登记的 `default_input` 和同索引 expected record，只调用公共 `generate_from_schema()`，不运行 Schema Agent；`--input-scope full` 用于全部输入回归。评分只看 Agent 外最终验收和 records 与 expected 的严格比较。

## 运行入口

```text
uv run python scripts/run_test_sets.py list --registry evals/datasets.toml
uv run python scripts/run_test_sets.py preflight --registry evals/datasets.toml
uv run python scripts/run_test_sets.py run --registry evals/datasets.toml --mode baseline
uv run python scripts/run_test_sets.py run --registry evals/datasets.toml --mode baseline --input-scope full
uv run --env-file .env python scripts/run_test_sets.py run --registry evals/datasets.toml --mode ttp-only --dataset vendor.platform.command --trials 3 --concurrency 1
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
- `datasets.toml` 中的文件哈希在最后一次修改后重新计算；
- 测试资产中没有 API Key、Authorization、Trace 原文或临时产物。

## 数据集阶段约定

四件套是测试集的**完成态**。实际制作分阶段进行，允许数据集以不完整的目录形式存在；一个数据集处于哪个阶段只由目录里实际存在的文件决定，禁止使用占位文件、空文件或状态标记文件来表达阶段。

| 阶段 | 目录内容 | 可独立运行的内容 |
| --- | --- | --- |
| inputs-only | `inputs/001..00N.txt` | 人工审查、后续阶段的输入基线 |
| template 阶段 | 上者 + `template.ttp` | 默认回显的模板解析验证；`--input-scope full` 验证全部输入 |
| 完成态（四件套） | 再加 `schema.json`、`expected.json` | 注册 TOML 后可 preflight / baseline / ttp-only |

补充规则：

- 数据集目录名与 TOML 的 `name` 一致，使用满足 `[a-z][a-z0-9]*(?:[._-][a-z0-9]+)*` 的小写形式，例如 `broadcom_icos.show_version`。
- 每个数据集目录必须自包含：不引用其他数据集的文件，所有派生信息相对数据集目录本身成立；任何一个目录可以单独拷出、单独审查、单独运行。
- 非完成态的数据集目录不受“只允许四件套”的约束；注册表会按阶段检查其实际文件，且会拒绝目录内无关文件。

### evals/datasets.toml

`evals/datasets.toml` 由 `evaluation.py` 和 `scripts/run_test_sets.py` 直接读取，是唯一运行入口。数据集可以先登记为 inputs-only 或 template 阶段；补齐四件套并更新哈希后自动进入 complete 阶段。无需生成或维护 JSON manifest。

```toml
version = 1

[[dataset]]
id = 1                                  # 正整数，全局唯一，用于按数字指定运行
name = "broadcom_icos.show_version"     # 数据集目录名
command = "show version"                # 产出该回显的命令
platform = "broadcom_icos"              # 平台/网络操作系统
source = "ntc-templates"                # 原始回显来源
tags = ["easy"]                         # 标记一类测试例
inputs = [                              # 相对数据集目录的路径 + 实测 SHA-256
  { file = "inputs/001.txt", sha256 = "..." },
]
```

- `tags` 只使用小写字母、数字、`-`、`_`，形如 `^[a-z][a-z0-9]*(?:[-_][a-z0-9]+)*$`；一组结构或难度相似的测试例共用同一 tag（如 `easy`）。
- `schema` 和 `expected` 必须同时登记；缺失其中一项属于注册错误。缺失已登记文件的目录保留为 pending，补齐文件后重新运行 preflight。
- `default_input` 必须精确匹配一个已登记的 `inputs` 路径。默认范围未登记它时为 pending，禁止回退到 `001.txt`；四件套可通过 `--input-scope full` 执行全量回归。
- 运行选择支持 `--dataset`、`--dataset-id` 和 `--tag`；不指定筛选条件时运行注册表中的全部数据集。

### 第三方来源

当前数据集的原始回显取自 [ntc-templates](https://github.com/networktocode/ntc-templates)（Network to Code，Apache License 2.0）的测试语料，仅做重编号拷贝，未修改内容。每个数据集通过 `source` 字段登记来源；新增第三方来源时必须在此与 `datasets.toml` 中同步说明许可与出处。

## 当前状态

2026-08 重新接入第一批数据集，见 `evals/datasets.toml`。
`broadcom_icos.show_version`（5 份）、`fortinet.get_system_status`（3 份）、
`paloalto_panos.show_interface_hardware`（2 份）与 `oneaccess_oneos.show_voice_mos`
（3 份，四者均标记 `easy`）已完成四件套并登记
`schema.json` 与 `expected.json`：全部字段保守使用 `string`，required 判定基于各输入的
实际出现情况（`fortinet` 的 `security_level`、`cluster_uptime`、`cluster_state_change_time`、
`extreme_db`、`fortios_x86_64` 与 `broadcom` 的 `cpld_version`、`board_revision`、
`fru_number`、`part_number` 为可选，缺失字段省略键）；`expected.json` 仅由原始输入回显
人工核对生成，preflight 与 baseline 均已通过，具备 strict baseline 与 ttp-only 资格。
`oneaccess_oneos.show_voice_mos` 是首个嵌套 object 形态：根对象下 `current_hour` 与
`previous_hour` 两个必填子对象各含 10 个必填字符串字段，模板以两个段标题行作命名子组
锚点，行首缩进经 `ignore("[ \t]+")` 消耗；原始语料为 CRLF，拷贝时已规范化为 LF。
`huawei_vrp.display_port_vlan`（4 份，无难度标签）仍处于 template 阶段。已登记模板的要点：
`cisco_nxos.show_interface_status`（5 份，标记 `medium`）为定宽 7 列接口状态表：`name`
列是自由文本（含空格与制表符，如 `managed by puppet`、`interface1<TAB>`），模板用
`re("(\S+(?: \S+)*)")` 贪婪匹配单空格词组、遇到 2+ 空格或制表符自然截断，`port` 用
`[A-Za-z]+\d\S*` 排除表头行；截断状态值（`xcvrAbsen`、`noOperMem` 等）忠实保留。
上游语料的 tunnel 变体（含第二段 5 列 Tunnel 表）与第 7 份简单变体未收入：5 列行会被
7 列空白分列模式错位吸收，与 hp_procurve 的缺列歧义同类，白名单内无法区分。
`cisco_ios.show_sdwan_control_connections`（2 份，标记 `medium`）为三行表头的 14-15 列
宽表：第 2 份输入多一个尾列 `controller_group_id`，模板用两个行模式变体并依赖
`method="table"` 使组内全部模式都成为记录起点（TTP 默认仅第一行模式为记录起点，多个
行模式变体共存时必须加该组属性）；`uptime` 用严格 `re("(\d+:\d+:\d+:\d+)")` 防止吞掉
可选尾列。
`fortinet` 为每键一行的扁平 `Key: value` 结构（值用 `ORPHRASE` 捕获）；
`broadcom` 的点线填充用 `ignore("[.]+[ ]*")` 消耗，`Additional Packages` 跨行续行经
`joinmatches` 合并为单字符串，含双空格的两个自由文本字段用 `ROW` 捕获；`paloalto` 以嵌套
子组实现"一条根 record + `interfaces` 数组"，行模式为 4 个空白分列 token；`huawei` 的
`vlan_list` 有意采用空格拼接的单字符串形式，与上游 ntc-templates TextFSM 的 token 数组
形态不同——TTP 行级匹配模型每行每变量仅能捕获一个值，且安全白名单禁止 macro 等动态
扩展，无法产出同值列表。模板制作受阻
记录：`hp_procurve.show_interfaces_status`（4 份，`medium`）为定宽缺列表格，`_headers_`
定宽解析虽能正确解析全部数据行，但其表头行自清洁机制要求字段名回显输入表头的原始
大小写（PascalCase），与受限 Schema 的 snake_case 契约冲突；空白分列解析因缺列行被证明
存在歧义，故暂不交付模板、维持 inputs-only，待白名单增加 strip 类过滤器后重做。
`cisco_ios.show_lldp_neighbors_detail`（5 份，标记 `hard`，重复邻居块、
嵌套可选 MED 子块与多行文本叠加，为全库复杂度最高形态）、
`cisco_s300.show_lldp_neighbors`（2 份，标记 `medium`，定宽列内截断换行需按列宽拼接）、
`hp_procurve.show_interfaces_status`（4 份，标记 `medium`，
表头一致的纯单表；模板制作受阻，见上）与
`cisco_ios.show_power_status`（3 份，标记 `hard`，跨行表头加主/子两级行结构）
仍为 inputs-only。这四个 inputs-only 数据集与 template 阶段的 `huawei` 均
尚无 `schema.json`、`expected.json`，因此暂不具备 strict baseline 或 ttp-only 资格；
`huawei` 仍可由 TOML runner 执行 template smoke。重新接入时不得复用旧的 expected、Schema、模板或历史目标记录。
