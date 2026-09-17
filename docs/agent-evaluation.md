# Agent 评测边界

评测输入统一为 `evals/test_sets/` 下的四件套测试集。每个测试集固定包含
`inputs/`、`schema.json`、`template.ttp` 和 `expected.json`；输入按 `001.txt` 到
`005.txt` 排列，所有输入共享同一个标准 Schema、模板和 expected records。根
唯一注册表 `evals/datasets.toml` 负责索引、标签和文件路径；目录实际状态决定数据集是 inputs-only、template 还是 complete。

加载器和确定性校验位于 `src/cli_parser_agent/evaluation.py`，统一入口是
`scripts/run_test_sets.py`。加载器严格检查 UTF-8/BOM、重复 JSON 键、路径越界、
输入数量、Schema 受限子集、expected records 与模板基线。标准模板必须在隔离 TTP 解析后
默认范围内对 TOML 中显式指定的单份 `default_input` 产生与同索引
`expected.json` record 完全一致的 records。`--input-scope full` 才验证全部输入。

## 运行模式

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
提交次数、首个有效候选、终止原因、耗时和可选 Laminar Trace ID。Schema 质量不作为 TTP-only 模式
的分数。

## 资产边界

标准 Schema 和 expected records 必须由维护者从 raw 回显独立核对，不能读取被测 Agent
结果、Trace、历史 artifact、上游模板或模型生成答案。标准 TTP 模板是可审查的确定性基线，
用于确认四件套自身闭环；TTP-only Agent 只按 Schema 和 expected records 评估。

运行产物写入 `.artifacts/test-set-evaluation/<run-id>/`，仅保存状态、数值评分、安全 issue code、脱敏配置及 Trace ID 等脱敏投影。模板、records、capture、原始输入和模型文本只通过显式 Laminar 通道观察；完整产物仅在内存中评分，不写入 trial 文件。Schema-only 只运行独立 Schema 阶段；end-to-end 调用公共 generate 完成共享预算下的两个阶段。

runner 版本 5 始终收集安全执行事实；`--trace-rounds` 仅控制逐事件明细落盘。漏斗区分有效候选、finish 调用、finish 成功及最终验收，缺少观测时省略数值指标而非填写零。候选轨迹只有可证实的时间顺序才判定有效提交发生于成功 finish 之前，否则报告未知。严格评分与遥测完整性独立，正确率 baseline 格式保持版本 1。配置记录模型重试次数、TLS 校验开关和 `extra_body` 是否配置，不包含凭据或请求扩展正文。

旧的 `evals/ttp_generation/`、`target/schema_contract` 双格式、
`run_agent_evaluation.py` 和 `run_ttp_template_evaluation.py` 不再是评测路径。没有标准
expected records 的临时排查可以使用专用单次 TTP 诊断脚本，但不得作为标准测试执行。

只有 complete 阶段、指定了 `default_input` 且通过 preflight/baseline 的数据集才进入默认
strict TTP-only 统计；缺少默认回显的完整数据集在该范围会作为 pending。`inputs-only` 和
template 阶段同样会作为 pending 或 smoke 结果单独报告。标签可用
`--tag` 过滤，未指定过滤条件时运行 TOML 注册表中的全部数据集。

逐事件明细只接受固定事件类型与项目事件名；工具名限于注册的直接 Schema、SchemaPlan、确认及三个 TTP 工具，未知值统一为 `unknown_tool`，完成原因限定为框架枚举。协议修复仅保存受控类别与次数。这个本地投影不会改变 Agent 决策或工具行为。

## 运行信息与兼容

注册表版本为 `2`，每个文件条目只有 `{ file = "..." }`。加载器仅支持该版本并继续
拒绝未知字段；旧版注册表需将版本改为 `2` 并删除文件条目中的 `sha256`。修改资产内容
后可直接重新运行 preflight 和 baseline，无需维护内容摘要。文件路径或默认输入变化时
仍需更新注册表。`.gitattributes` 保留 LF 规则，文本加载器继续将 CRLF 归一化为 LF。

| 信息 | 保存和用途 |
| --- | --- |
| 数据集与输入选择 | case 路径、输入路径、原始索引、input scope 和标签用于说明测试范围 |
| 生效配置 | 运行 summary 保存脱敏模型参数、GenerationPolicy 和 `prompt.version`；扩展参数只记录是否配置 |
| 运行来源 | Git revision、dirty 状态、运行时间和唯一 trial ID 用于定位一次运行 |
| 执行与正确性 | 执行事实、计数、数值评分、安全 issue code 与 Trace ID 分别说明流程、结果和遥测 |
| 模板修正 | 安全 submission 事件保留提交序号与模板字符数；仅凭这两个值不能判定模板正文相同 |

新报告使用 `runner_version=5`，不计算或写入资产、注册表、提示词、模板或配置摘要。
三个单次/TUI 开发脚本使用 `script_version=2`，输入元信息保留路径和字节数。
WebUI 继续保存完整的本地配置并向页面提供现有脱敏视图，持久配置版本保持 `1`。

正确率 baseline 的 `baseline_version` 保持 `1`。`--write-baseline` 导出数值记录，
`--baseline` 按 case ID、成功数、trial 数及容忍值比较；离线 `--mode baseline` 则验证
标准模板与 expected 的一致性。两者不应混淆。严格正确率仍只由独立验收和 records
深度全等决定，模板写法或文件排版相同与否不作为评分依据。

旧报告和历史 baseline 保持原样，读取时允许已有摘要字段；配置比较忽略旧的
`model.extra_body_sha256`、`prompt.schema_system_sha256` 和 `prompt.ttp_system_sha256`，
继续展示真实参数、预算与提示版本差异。`scripts/compare_accuracy_runs.py` 比较完整的
runner v5 TTP-only 目录，核对记录的输入选择、case/trial 数、模型、预算和提示版本；
它不校验历史资产内容，分析时仍须确认预期配置差异。默认只读本地数值结果；显式
`--laminar` 才按时间窗口和 Trace ID 查询聚合计数、时延与用量，不导出 Trace 正文。
缺少推理用量的 span 不计为零，报告同时给出观测率，部分观测不计算完整推理占比。

上下文策略的四组消融通过默认关闭的进程内适配器执行，仍委托唯一标准入口评分，
不改变公共 API。Thinking 计数修正已成为产品默认，四组名称仍表示原始实验定义，
其中 `current` 是旧计数对照、`estimator` 与当前产品行为一致；原生压缩机制未更改。
使用方法、容量边界、结构诊断与离线验证见
[上下文策略消融实验](context-ablation.md)。

历史 WebUI 运行仍可打开和重执行，页面不再展示配置指纹。此前 `.artifacts/` 中的
launcher、审计脚本及报告作为历史材料保留；后续测试使用标准入口。Git 原生提交 ID
和 `uv.lock` 依赖包校验仍保留，不参与 case 或 trial 评分。


## Schema-only 基线

`run --mode schema-only` 对每份所选输入调用独立生成器的公共 `propose_schema()`，不运行
TTP Agent。默认输入选择、数据集 ID 并集、并发、重复次数、配置脱敏和退出时 Laminar flush
与现有入口共用；只有通过离线 preflight 的 complete 数据集参与。人工 Schema、标准模板和
expected 仅用于离线验收或评测侧比较，不进入被测请求。使用产品默认 Thinking 计数。

```powershell
uv run --env-file .env python scripts/run_test_sets.py run --registry evals/datasets.toml --mode schema-only --trials 4 --concurrency 4 --dataset-id 1 --trace-rounds
```

runner v5 的 Schema-only 文件显式保存 `mode=schema-only`；不生成 TTP 的 strict_pass、
finish 或 records 分数。禁止与 `--baseline`、`--write-baseline` 或非零
`--regression-tolerance` 组合。一次正常记录完成返回 0，提案失败仍计入所有 trial 的分母；
配置/preflight 错误返回 2，人工取消返回 130，已完成的 trial 文件保留。

成功提案在评测侧再次运行共用 Schema 校验。报告记录生成和复验状态、Schema 提交及轮次、
首份提交验收、首次冻结时间、拒绝类别、终止原因、Token 和耗时。拒绝计数是该类别影响的
已观察提交数，同一提交中的重复 issue 不放大计数；没有观测时用 null，观测完整性单独标记。
推理 Token 若观察事件未提供则为 null，后续仅可从 Laminar 数值字段补充，不推算为零。

结构描述仅保存属性数、叶子数、深度（根为 0，数组元素计一层）、类型分布、required 数和
属性 description 覆盖率。在内存中按字段路径、节点类型、相对于父对象的 required 标志
比较成功提案，忽略属性顺序、required 顺序、description；落盘只保存变体数量、主导比例和
两两一致率。不足两份时一致率不可用。人工 Schema 的路径、类型、required 差异只保存数量，
作为审阅线索，不是准确率；人工 Schema 可覆盖模型本次未见的其他输入。

语义审阅逐条只读进行；本轮明确授权通过 Laminar 数据库在分析内存读取正文，仍禁止落盘或回灌产品模型。按命名、主要字段覆盖、逻辑值拆分、结构归属、
类型及必填、业务边界和过度约束七项记录通过/有问题/证据不足/不适用，并给出
可接受/需修正/无法判断。没有可见业务错误才记为可接受，明确错误记需修正，关键证据缺失
记无法判断。描述缺失本身不构成错误。正文、Schema、description、enum 值、结构签名和内容
哈希均不写入本地报告；只保存受控分类、字段路径、计数和 Trace/span 标识。

## Schema 契约一致性（metrics v2）

Schema-only 新产物增加 `schema_metrics_version=2`，runner 仍为 5，旧结构指标含义不变。
契约比较仅使用生成成功且复验通过的提案；失败仍进入全部计划 trial 分母。
四份有效提案产生六对，按 trial ID 排序，不受并发完成顺序影响。
历史缺少新指标时显示不可用，不推算或补零。所有 Schema-only 报告明确
`parseability=not_tested`：Schema 合法不代表实际 TTP 可解析。

完整结构相等要求路径、类型和相对父对象的 required 相同；完整契约相等还要求受限
Schema 的所有校验约束相同。忽略 properties/required 顺序，省略 required 与空列表等价，
enum 按集合比较并区分布尔与数字。除数字的 JSON 数值等价外，不推导约束写法的数学等价。
只忽略 Schema 节点上的 title/description，不忽略同名业务属性。

逐对输出只含 ID、布尔结论、数量和比率：根以外节点的路径交集/并集/差集、路径重合度，
共同路径上的容器/标量类型差异、共同属性路径的 required 差异、同类型节点的约束差异，
以及共同节点的说明文字变化。数组元素以 `*` 表示；类型、约束、说明比较包含根节点。
每类差异分别保存受影响 pair 数、差异节点数和可比较节点分母；说明文字的增删也计一次
节点变化，不推断语义变化。路径差异不自动叫作改名，下游连带差异不代表独立根因。
每例保存契约变体数、主导占比和两两一致率；跨用例给出宏平均及按有效 pair 数加权汇总。
少于两份有效提案时一致率不可用。完整路径清单、约束值、签名及哈希均不落盘。

## 离线 Schema 审阅汇总

```powershell
uv run python scripts/run_test_sets.py schema-review --run-directory .artifacts/test-set-evaluation/RUN --review-file review.json
```

此子命令只读取数值运行产物和受限审阅文件，不读取注册表、模型配置或 Trace 正文。
写入独立 `schema-review-summary.json`，不覆盖原 summary 或审阅源文件。
审阅文件必须为以下精确结构，所有节点拒绝额外键，不接受自由正文：

- 根：`review_version=1`、`run_id`、`trials`、`pairs`。
- trial：`trial_id`、`case_id`、`trace_id`、`span_ids`、`dimensions`、`overall`、
  `categories`、`paths`。Trace 必须匹配运行记录；span 最多 64 个互异标准 UUID，
  是人工定位标识，汇总器只校验格式，不声称独立验证其 Trace 归属。
- dimensions 必须完整包含 `naming`、`coverage`、`decomposition`、`structure`、
  `types_required`、`value_boundaries`、`overconstraint`；值为 `passed`、`issue`、
  `insufficient_evidence` 或 `not_applicable`。
- overall：`acceptable`、`needs_revision` 或 `unjudgeable`。可接受必须生成及复验成功、
  有匹配 Trace，且没有 issue 或证据不足维度；需修正至少有一个 issue。
- pair：`case_id`、`left_trial_id`、`right_trial_id`、`annotation_semantics`、
  `judgment`、`categories`、`paths`。仅允许同用例的两份有效提案，拒绝重复或错配。
  annotation_semantics 为 `equivalent`、`different` 或 `unknown`；judgment 为
  `both_reasonable`、`at_least_one_issue` 或 `insufficient_evidence`。
- categories 为互异固定枚举：`synonym_naming`、`structure_placement`、`type`、`required`、
  `constraint`、`split_merge`、`coverage`、`annotation_wording`、`empty_slot`、`placeholder`、
  `unit`、`value_boundary`、`field_meaning`、`unresolved_mapping`。可同时记录多个类别。
- paths 最多 24 个互异路径，每条最多 2048 字符，只允许 `/` 或 ASCII snake_case 字段与
  `*` 组成的路径，每段最多 120 字符；只记录问题位置，不保存完整路径清单。

逐条和逐对审阅采用上述只读正文边界。说明变化必须区分措辞变化与捕获语义改变；
无法对应字段时使用 unresolved_mapping，不猜测改名。缺失项保留未完成，不默认通过。
汇总给出可接受/需修正/无法判断/缺失数量；失败与复验失败另列，可与无法判断重叠。
合理提案比例以全部计划 trial 为分母。两份均可接受的 pair 只有契约相等、说明语义等价且
人工确认两者合理时才计入合理且一致；未知说明不通过。另以全部计划 pair 为分母给出
已确认合理且一致比例，并逐例检查全部重复和全部 pair 是否都通过。两份同样错误的提案
即使契约完全相同，也不能计入联合通过。缺失旧指标时联合一致结果为 null。

## Schema 审阅 v2：政策、业务与一致性

`schema-review` 同时接受严格的 `review_version=1` 和 `2`，runner 仍为 5、自动
metrics 仍为 2。v1 的旧字段、七维和联合通过含义不变；新增政策及人工一致性指标为 null。
v2 保留全部旧字段，并要求以下附加字段；所有层级仍拒绝额外键和自由正文。

- trial 的 `policy_checks` 固定为 `root_scope`、`repeated_entities`、`fixed_role_sections`、
  `ownership_and_wrappers`、`source_label_fidelity`、`qualifier_ownership`、
  `container_name_source`、`fallback_and_collision`。值沿用 passed/issue/insufficient_evidence/
  not_applicable；root_scope 不能不适用，其他项仅在确无场景时不适用。
- trial 的 `policy_paths` 独立于业务问题路径，沿用现有格式限制；与 `paths` 的并集最多 24 条。
- pair 的 `naming_consistency`、`hierarchy_consistency` 为 consistent/different/unknown。
  人工可靠对应业务字段后判断；发现差异为 different，没有确认差异但对应不完整为 unknown。
  层级仅关注根粒度、容器类型和归属，不将标量类型或 required 差异混入。不得自动匹配同义名。

业务合理性与政策遵循相互独立：合理同义名可以 acceptable 但违反标签政策；两份同样错误的
Schema 仍不业务通过；合法语义兜底也可能命名不一致。旧联合指标保持原义。
新增 `policy_compliance` 汇总各规则四态、合规/违反/不足/失败/缺失，以及同时业务可接受的
本轮通过数；`pair_consistency_review` 分别汇总命名和层级的一致/不同/未知，列出有效 pair、
已审阅、缺失和可判断分母。生成或复验失败、证据不足和缺失审阅都不能记通过。

本轮政策见 [v48 协议](schema-policy-v48-protocol.md)。工程验收后保留新政策，不设真实结果
提升门槛、不自动回退或追加批次。Schema-only 继续标记可解析性未测。

## 生成契约的端到端评测

`run --mode end-to-end` 只将所选输入传给公共 `generate()`，Schema 与 TTP 共用产品预算；
不向模型提供标准 Schema、模板或 expected。成功后执行既有独立验收，禁止使用人工字段布局
进行 records 全等评分，也不自动匹配同义字段。本模式没有 `strict_pass` 或叶子准确率，
不支持 TTP accuracy baseline 选项。

`generation_success` 表示整次生成成功；`schema_generation_success` 表示有冻结契约。
观察器仅在工具结果明确 `frozen=true`、`accepted=true` 时从提交或确认结果收集 Schema；
待确认草稿和 last_attempt 不构成冻结证据。最终 artifact 同样可证明冻结。TTP 失败但已冻结的
Schema 仍参与 metrics v2 和七维审阅，避免因解析失败隐藏建模结果。完整 Schema 只在内存比较。
SchemaPlan 只附 nodes、references、fallback_names、evidence_incomplete_nodes、required_fields
五项受控非负整数；确认调用不增加 Schema 提交分母。

独立验收、候选/finish/终验事件、Schema 复验、输入输出 Token 和两阶段轮数分别保存。
缺失执行观测不补零。自动运行完成标记 `parseability=executed_pending_review`，需要下述
人工内容审阅才能计算端到端联合通过。

```powershell
uv run python scripts/run_test_sets.py parse-review --run-directory RUN --review-file parse-review.json --schema-review-file schema-review.json
```

该子命令只读取数值运行记录和受限审阅文件，重新验证 Schema 原始审阅文件，并单独生成
`parse-review-summary.json`；不覆盖来源、原 summary，不调用模型、不读取 Trace。

- 根为 `review_version=1`、`run_id`、`trials`；trial 的 ID、case、Trace、span、paths 约束
  与 Schema 审阅一致，所有节点拒绝额外键，无自由正文。
- 五维 `entities`、`coverage`、`value_fidelity`、`empty_missing`、`order` 必须齐全，取值为
  passed/issue/insufficient_evidence/not_applicable。前三项不能以 not_applicable 通过。
- overall 为 acceptable/needs_revision/unjudgeable。固定 categories 为 entity_count、
  entity_placement、coverage、value_changed、value_boundary、empty_slot、missing_key、order、
  unresolved_mapping、execution_failure。paths 最多 24 条，每条 2048 字符。
- acceptable 需要生成成功、独立验收通过、匹配 Trace，且没有 issue 或不足证据，问题 categories/paths 必须为空。needs_revision 必须给出固定问题类别。联合通过还
  需要该 trial 的 Schema 审阅可接受。未知、失败与缺失项保留在计划 trial 分母内。

## 历史同期 Schema 实验（fc2e31a）

以下是未采用实验提交 fc2e31a 的执行方式；旧 plan/plan_confirm 选择已经撤下。

历史评测专用 `--schema-experiment-arm` 可重复指定 direct、plan、plan_confirm，仅允许
schema-only/end-to-end；它是内部测试工厂入口，不是产品配置开关。省略时使用当前默认策略。
多组共享同一次加载得到的输入对象、model/policy、Git 状态与全局 semaphore。按 case/trial
循环轮换 ABC/BCA/CAB 入队，每组独立产物目录与原审阅格式，父 experiment.json 保存计划数、
已完成数、路径、版本及 `same_input_snapshot=true`。不保存输入指纹。

```powershell
uv run --env-file .env python scripts/run_test_sets.py run --registry evals/datasets.toml --mode schema-only --trials 2 --concurrency 4 --dataset-id 3 --schema-experiment-arm direct --schema-experiment-arm plan --schema-experiment-arm plan_confirm
```

任务局部 ContextVar 决定工具和提示版本，不同并发组互不修改默认策略。单组四份有效契约仍
产生六对比较，不跨实验组组合 pair。正式比较使用相同入口和两个组，全局并发仍为 4。
组数乘以用例数及重复数是实际请求计划数；runner 不自动补跑或调整采用门槛。

SchemaPlan 的最终冻结方案另外记录 `fallback_naming`：业务节点数、兜底节点数及比例。C 只有确认后才使用最新待确认方案；失败、缺失观察不补零，A 标为不适用。同用例有效 pair 的 `fallback_naming_consistency` 只保存兜底名称集合是否相等、交并比和差集数量。集合仅在内存中比较，不保存名称清单或签名；集合差异不能直接解释为同一业务字段改名。

## 历史 v47 同期轻量草稿实验（f7b9a16）

实验提交 f7b9a16 的选择仅允许 direct/draft。24 次预试未达标后撤下选择参数并恢复 v44；当前默认为 v48，以下描述历史执行及保留指标。两组复用同一原始输入快照、全局 semaphore、模型和 policy，按用例 AB/BA 轮换，不跨组组成一致性 pair。来源展示会增加候选上下文开销，单列采样量与展示字符数。工具参数、Schema 及兜底名称集合只在内存观察；保存的 draft 指标只含字段/引用/兜底/原因/拒绝/字节数量。供应商 finish_reason 缺失不能按零截断统计。

原计划为 24 次预试达标后才运行 112 次端到端对照，最多 136 次。实际止于 24 次，没有补跑或正式对照，见 [结果](schema-draft-v47-pretrial-regression.md)。受限 Schema/解析审阅及业务事实清单沿用现有协议，新增命名和根粒度观察不作为自动业务门禁。完整配置、采用门槛、隐私和回退见 [v47 实验协议](schema-draft-v47-protocol.md)。

## Schema 推理截断恢复观察

`observations.reasoning_recovery` 仅投影当前 Schema 受控恢复事件的状态、次数及轮次，
不导出供应商正文。未观察到有效事件时标为 unavailable，不以零代表历史已核对无激活。
提示版本与运行时策略分别记录：本轮仍为 v48，恢复为 schema-reasoning-recovery-v1；
真实配置由 revision、显式 model/policy 和逐条观察共同确定。详见
[恢复与保护协议](schema-reasoning-recovery-v1.md)。
