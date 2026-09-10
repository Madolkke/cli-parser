# v38 Hard 结构基线与定向修复依据

## 结论与实施门槛

2026-09-11 基线严格通过 **6/8：LLDP 3/4、Power 3/4**。8 次都有有效候选、
成功 finish、产品终验和 runner 独立验收；两次最终失败均为内容差异。
本轮选择一个修复点：**混合根结构的完整早期匹配起点**，收紧实际行匹配条件，
澄清经验证的重复分隔行不必因缺少唯一标题而一概排除。不改变章节结束规则。

实施依据来自两条新失败 Trace：LLDP 第 4 次的早期根结构实验，以及 Power
第 1 次的完整提交。LLDP 最终仅剩自由文本换行转义错误，根起点并非它最终
严格失败的直接原因；本项只能尝试减少中途结构探索，不能宣称必然修复 LLDP。
Power 最终省略可选根字段，且此前反复使用不匹配完整表头的根起点。
满足“新失败 trial 的结构证据 + 单因素合成复现”的实施条件，但不提前判定采用。

采用门槛固定为修复版 LLDP 4/4、Power 至少 3/4、Easy 16/16、Medium 16/16，
加上全部离线检查通过。之后依次完整运行 Hard 8 次、Easy/Medium 32 次；不补跑。
不达门槛以新提交恢复 v38 产品提示和默认文档，保留诊断测试及结果。

## 启动与配置

从干净 main `b0e3380` 启动；与已验证 `dd3639e` 比对，src、evals、uv.lock
无差异，仍是 v38。Docker/Laminar 正常；启动前已完成 write/flush/API 回读及
实际项目 UI 登录验证，探针 Trace `a0f1f7e5-2dd7-1f18-e6f7-a3f40eb0a34b`。
默认 preflight/baseline：10 个 complete 全部通过，Huawei 保持 pending。
现有结构复现和提示示例相关测试 25 passed。未修改评测资产。

- 标准入口 run_test_sets.py 的 ttp-only，由 estimator 观察适配器委托。
- LLDP 005.txt、Power 001.txt，默认范围；各 4 次，全局并发 4。
- deepseek-v4-flash，temperature 0，非流式，max_tokens 8192，context 128000。
- HTTP timeout 120 秒、模型重试 2、TLS 开启，无 extra_body。
- 仅评测进程覆盖 1800 秒、26 轮、18 次提交、3 次独立测试。
- 其余 model/policy 与 v38 Easy/Medium 运行配置逐字段相等。
- 本地运行：`.artifacts/hard-structure-v38-baseline/evaluation/20260910T161606.307875Z`。

Trace 正文仅在 Laminar UI 审阅。SQL 只投影有界计数、白名单字段路径和标识；
未导出输入、Schema、模板、records、模型正文或内容哈希。成功模板只用于机制
对照，未视为标准答案或复制进提示。下表中的无法确定原因明确记为未知。

## 全体结果

| Trial | 严格通过 | 提交/测试 | 首次有效候选秒 | 总秒 | Trace |
| --- | --- | --- | --- | --- | --- |
| L1 | 是 | 5.0/3.0 | 394.40 | 417.00 | `b1fe10a2-1391-8d3b-2d64-80512f4297ce` |
| L2 | 是 | 2.0/3.0 | 646.90 | 658.58 | `59a7a2b4-0902-0ee6-276c-1bc61c2fa8e9` |
| L3 | 是 | 3.0/3.0 | 333.90 | 475.58 | `b81d9309-376b-0358-10e9-b89bd9d6992f` |
| L4 | 否 | 7.0/3.0 | 1455.41 | 1463.89 | `735733b8-bf29-200f-a169-8ce57ddd7ddc` |
| P1 | 否 | 17.0/3.0 | 1280.87 | 1335.22 | `5b0489fc-bfe0-3eaf-f40f-50574c4ba14d` |
| P2 | 是 | 2.0/2.0 | 162.76 | 167.34 | `7b819517-07c7-04f5-ebf7-8f7bb852fd90` |
| P3 | 是 | 1.0/2.0 | 225.50 | 232.53 | `d0e93e8f-3a71-5e2c-6f0c-5667c5cfd1f5` |
| P4 | 是 | 4.0/3.0 | 227.25 | 537.73 | `69c452e8-91f9-962e-21b6-2b14ef25881f` |

41 次提交中 accepted 11、Schema 拒绝 25、非 object 结果拒绝 3、非法 group
名称拒绝 2，worker/SystemExit/裸管道错误均为 0。22 次独立测试都 parse-only
成功；这不意味着结构符合冻结 Schema 或包含原文所有字段。没有预算终止。

73 次模型调用完整观测。输入 Token 5,944,300、输出 1,151,031，其中 reasoning
1,130,023（占输出 98.175%，不是额外相加）；LLM 秒数合计 5,055.84。
总请求秒数合计 5,287.87，平均 660.98；整批墙钟 1,758.67 秒。
首次提交平均 322.83 秒，首次有效候选平均 590.87 秒。
context.fit 8 次，原生 compression 0 次；旧提交反馈折叠 33 次。
初始任务完整、工具配对及顺序检查 73/73，未发送 Thinking 73/73；这不等于
已逐条审阅所有反馈正文。Laminar 与本地计数相符，无缺失观测需要补零。
供应商 span 显示的模型名与配置名存在别名差异，按配置记录模型，不据此改参数。

## 失败 trial 的逐次证据

序号 S 为完整提交，X 为独立测试。时间为 trial 启动后的工具开始秒数；剩余提交
由当时 session 计数确定，不代表模型仍有足够时间执行全部剩余提交。
Schema issue 为上游去重后的诊断，通配路径不声称定位具体数组元素。

### LLDP L4

Trace `735733b8-bf29-200f-a169-8ce57ddd7ddc`。

| 工具 | 秒/剩余提交 | 结果与诊断 | UI 归因 | Span |
| --- | --- | --- | --- | --- |
| X1 | 170.1/18 | parse-only 成功 | 局部根结构实验：子组结果存在而根汇总缺失。 | `00000000-0000-0000-ba7b-6a3a9c5e6d34` |
| X2 | 190.9/18 | parse-only 成功 | 继续根起点实验，根汇总仍未保留。 | `00000000-0000-0000-60a9-92124b8361f0` |
| X3 | 393.9/18 | parse-only 成功 | 拆为多个根 group；parse-only 成功不能证明根 object 映射正确。 | `00000000-0000-0000-b8b6-4a2334d4b132` |
| S1 | 932.0/17 | schema.record_mismatch /neighbors/* system_description,time_remaining_seconds | description 与 time 必填缺失；根探索后 932 秒才首次完整提交。 | `00000000-0000-0000-bc87-02590663c639` |
| S2 | 982.7/16 | schema.record_mismatch /neighbors/* system_description,time_remaining_seconds | 上述两类缺字段仍在。 | `00000000-0000-0000-df81-5db845503cd3` |
| S3 | 1078.3/15 | schema.record_mismatch /neighbors/* time_remaining_seconds | description 出现，time 仍缺失。 | `00000000-0000-0000-63a4-c6288579193f` |
| S4 | 1157.4/14 | schema.record_mismatch /neighbors/* time_remaining_seconds | 根汇总、重复对象与嵌套结构存在；time 缺失。下一次实际请求已核对完整反馈和 records。 | `00000000-0000-0000-5c7c-cf330410437d` |
| S5 | 1235.8/13 | schema.record_mismatch /neighbors/* time_remaining_seconds | 仅更换数值捕获仍未消费单位；time 缺失。 | `00000000-0000-0000-b97a-eab541d44afa` |
| S6 | 1403.0/12 | schema.record_mismatch /neighbors/* auto_negotiation,chassis_id,enabled_capabilities,management_addresses,media_attachment_unit_type,physical_media,port_description,port_id,system_capabilities,system_description,system_name,time_remaining_seconds | 候选缩减为少量字段，新增 12 个必填字段缺失。 | `00000000-0000-0000-ab3e-9a4bd4b94d2e` |
| S7 | 1452.8/11 | accepted | 恢复完整候选并匹配单位；accepted。最终 description 用字面反斜杠+n 代替换行，非结构错误。 | `00000000-0000-0000-8331-daf58fd7c03d` |
### Power P1

Trace `5b0489fc-bfe0-3eaf-f40f-50574c4ba14d`。

| 工具 | 秒/剩余提交 | 结果与诊断 | UI 归因 | Span |
| --- | --- | --- | --- | --- |
| X1 | 82.3/18 | parse-only 成功 | 仅表格行/列变体实验，没有完整根尾字段。 | `00000000-0000-0000-07e3-9d59b620ea4a` |
| S1 | 183.9/17 | schema.record_mismatch / power_supplies | 根起点只匹配多列表头的首个词；完整输入无该独立标题行。 | `00000000-0000-0000-d36f-0bbab65d6160` |
| X2 | 246.9/17 | parse-only 成功 | 实验人为缩短标题，且分离根与数组，无法证明真实完整结构。 | `00000000-0000-0000-03d9-99c0664db1df` |
| S2 | 293.1/16 | schema.record_mismatch / power_supplies | 仅去除 XML wrapper，错误根起点保留。 | `00000000-0000-0000-2a15-8b3f5415005f` |
| S3 | 534.6/15 | ttp.invalid_record (受控模板路径未投影)  | 拆出独立匿名根尾字段组，结果不符合单根 object。 | `00000000-0000-0000-c841-f6b034bdab94` |
| X3 | 563.7/15 | parse-only 成功 | 只测试重复父子数据行，没有表头和根尾字段。 | `00000000-0000-0000-328c-45f6092be284` |
| S4 | 627.9/14 | ttp.invalid_record (受控模板路径未投影)  | 具体修改因果未知；SQL 确认本次拒绝类别，未以同类诊断自动归因。 | `00000000-0000-0000-2cb2-073810cfe327` |
| S5 | 643.4/13 | schema.record_mismatch / power_supplies | 具体修改因果未知；SQL 确认本次拒绝类别，未以同类诊断自动归因。 | `00000000-0000-0000-e69a-d68d8b3c920f` |
| S6 | 706.3/12 | schema.record_mismatch / power_supplies | 具体修改因果未知；SQL 确认本次拒绝类别，未以同类诊断自动归因。 | `00000000-0000-0000-2aba-65f3610e026b` |
| S7 | 766.7/11 | schema.record_mismatch / power_supplies | 具体修改因果未知；SQL 确认本次拒绝类别，未以同类诊断自动归因。 | `00000000-0000-0000-1134-e901802a42e6` |
| S8 | 952.6/10 | schema.record_mismatch / power_supplies | 具体修改因果未知；SQL 确认本次拒绝类别，未以同类诊断自动归因。 | `00000000-0000-0000-b4d6-c1e7e1fdb5c4` |
| S9 | 1013.5/9 | schema.record_mismatch / power_supplies | 具体修改因果未知；SQL 确认本次拒绝类别，未以同类诊断自动归因。 | `00000000-0000-0000-6810-c67321c1f6c7` |
| S10 | 1060.3/8 | ttp.invalid_record (受控模板路径未投影)  | 具体修改因果未知；SQL 确认本次拒绝类别，未以同类诊断自动归因。 | `00000000-0000-0000-1b3d-56dfc4b1592b` |
| S11 | 1100.2/7 | ttp.invalid_group_name (受控模板路径未投影)  | 具体修改因果未知；SQL 确认本次拒绝类别，未以同类诊断自动归因。 | `00000000-0000-0000-c82a-f66222508cd5` |
| S12 | 1119.8/6 | schema.record_mismatch / power_supplies; schema.record_mismatch /  | 具体修改因果未知；SQL 确认本次拒绝类别，未以同类诊断自动归因。 | `00000000-0000-0000-0f11-dd23d753a72c` |
| S13 | 1140.5/5 | ttp.invalid_group_name (受控模板路径未投影)  | 具体修改因果未知；SQL 确认本次拒绝类别，未以同类诊断自动归因。 | `00000000-0000-0000-1cd1-9a3a14dfa08d` |
| S14 | 1219.1/4 | schema.record_mismatch / power_supplies | 具体修改因果未知；SQL 确认本次拒绝类别，未以同类诊断自动归因。 | `00000000-0000-0000-7c6d-5c2f908e39d9` |
| S15 | 1239.6/3 | schema.record_mismatch / power_supplies | 具体修改因果未知；SQL 确认本次拒绝类别，未以同类诊断自动归因。 | `00000000-0000-0000-ce7d-6fd98dca1fab` |
| S16 | 1278.4/2 | accepted | 删除根结构及 warning 捕获；唯一 accepted 候选，缺 /warning。 | `00000000-0000-0000-cbb6-bbd3b23772e9` |
| S17 | 1311.5/1 | schema.record_mismatch / power_supplies | 尝试恢复完整结构，根起点仍只匹配表头首词；拒绝，保留候选 S16。 | `00000000-0000-0000-4807-aebdaa29d4b1` |

LLDP L4 的一次零工具回复持续 273.72 秒，模型反复推测根组合并与重复起点行为；
这些推测不是 TTP 实现事实。三次测试用尽后仍进行了无新增证据的结构推理。
最终候选根汇总、数组、尾字段已保留，最终严格差异位于
`/neighbors/*/system_description`。数值单位与字符串转义不纳入本轮修复。

Power P1 在 S17 后实际请求中可见 accepted=false、remaining_submissions=1、
retained_candidate_submission_index=16，以及完整空 record 和 /warning 缺失事实；
随后 finish 使用 S16。相关 LLM span `00000000-0000-0000-1e1a-8c10eed129a5`。
因此不能归因为“模型没收到失败反馈”，也不能把 retained candidate 等同于内容正确。
S16 的完整捕获包含重复对象及子数组，缺少原文根警告；其模板没有警告捕获。

## 机制对照与选择

- 失败 trial 中，根起点问题至少影响 2 个 trial。直接 UI 确认 Power S1、S2、S15、
  S17 共 4 次完整提交的起点不匹配完整表头；LLDP L4 主要体现在早期独立实验。
  不把其他同类 Schema 拒绝全部算作已证实的相同机制。
- 成功 L1 的 S1/S3 使用不完整分隔行失败；S4 采用完整根分隔行后 accepted，
  S5 修复另一个可选内容后严格通过。重复分隔行并不必然产生多个根 object。
- 成功 L2 相邻修改同时调整了正则和声明顺序，不能用它证明声明顺序的单因素收益。
- 本轮未取得比根起点更完整的新嵌套结束边界证据。正则竞争、字符串转义、缩减候选
  与 finish 决策另列为观察，不自动叠加产品修改。

独立通用合成实验使用不同名称、数值与章节，根→items*→features→capabilities*，
父级 tail、根 total；三个实体依次有/无/有可选章节。经白名单、spawn worker 和
Schema 校验：无根起点时子组和父尾字段保留但可选 total 丢失，Schema 仍通过；
仅增加完整重复分隔行匹配时所有值精确符合独立预期；仅缩短匹配字面量时根为空，
以 required 拒绝。去掉中间 tail 或根 total 的输入仍准确省略，未串组。
反例：相同分隔行出现在嵌套章节内部时，父尾字段可以丢失，即使 Schema 通过。
因此示例适用范围必须限定为实体之间的分隔行，不能推广为任意位置的分隔符。
这些探测随后固化为独立离线测试；不会把评测模板或 Trace 正文转成产品示例。

此次小样本基线不是稳定 75% 准确率的统计证明。后续严格门槛只用于本轮采用决策。
