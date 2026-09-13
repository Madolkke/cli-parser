# v40 Schema-only 首轮真实基线

本轮完成 14 个用例各 4 次、全局并发 4 的 56 次 Schema-only 评测，并在 Laminar UI 逐条审阅全部最终提案及 7 次初次拒绝。没有补跑，没有修改产品提示、解析规则或标准资产。

## 核心结果

- 生成成功及评测侧复验：均为 **56/56（100%）**。首次提交通过 **49/56（87.5%）**。
- 63 次 Schema 提交中接受 56 次、拒绝 7 次；7 个 trial 受影响。Python 关键字、字符命名、标量保留名拒绝均为 **0/63**。这证明本批命名合规，不能证明门禁提升了语义准确率。
- 语义审阅：**可接受 49/56、需修正 7/56、无法判断 0/56**。这是基于所展示默认输入的人工判断，不是与人工 Schema 的逐字一致率。
- 14 个用例仅 1 个四次结构一致；全部 84 对成功提案中 16 对一致（**19.0%**）。结构签名忽略 description 和约束细节，因此结构一致仍可能存在业务值解释差异。

## 运行与工程验收

实现提交 `6f6d52e`，产品提示仍为 `ttp-generator-v40-python-identifier-field-names-zh-cn`。运行 ID `20260913T153031.599662Z`；北京时间 2026-09-13 23:30:31 至 23:34:51，trial 执行窗口约 259.4 秒。

数据集 ID 并集为 `1,3,5,6,7,9,10,11,13,14,16,17,18,19`，每例均使用登记的 `inputs/001.txt`。排除 Hard、禁用 Juniper/Huawei ONT 和非 complete 的 Huawei VRP。直接由标准 `scripts/run_test_sets.py --mode schema-only` 调用公共 `propose_schema()`，未使用 TTP estimator 适配器。

模型为 deepseek-v4-flash，temperature 0、非流式、max_tokens 8192、context 128000、HTTP timeout 120 秒、内部重试 2；总预算 1800 秒、26 轮。其他 model/policy 字段与最近 Easy/Medium 运行逐项一致，完整脱敏配置见结果 JSON。TTP 提交 18 次、独立测试 3 次的额度未被本模式使用。

完整离线 pytest 1079 passed / 3 live skipped；此后新增独立复验用例，相关 Schema 测试 15 passed。JavaScript 31/31，Ruff、格式及 diff 检查通过。默认和 full-scope preflight 均无失败；baseline 均 16/16，full Huawei smoke 1/1。产品源码与 v40 基线没有改动。

运行明确记录 `dirty=true`：已有 registry、禁用目录、扩展用例和相关文档改动仍在工作区。本轮能力提交未夹带这些资产，也未重置它们。Laminar Docker 服务、探针写入、flush、SQL 回读及 UI 正文访问均在运行前验证。

退出时出现一次 `RuntimeError: async generator ignored GeneratorExit` 清理警告。进程返回 0；全部 56 份 trial、56 个成功根 span、63 个提交 span 和 63 个 LLM 用量 span 可回读，observer 提交计数与产品/SQL 一致。没有发现本批产物或观测缺失；清理警告的内部原因本轮未定位，也没有据此重跑。

## 按用例结果

所有行生成/复验均为 4/4。变体与一致率只比较成功提案；语义列依次为可接受/需修正/无法判断。

| 用例 | 首次通过 | 结构变体 | 主导占比 | 两两一致率 | 语义 | 平均秒 | 输入/输出/推理 Token |
| --- | --- | --- | --- | --- | --- | --- | --- |
| broadcom_icos.show_version | 4/4 | 3 | 50% | 16.7% | 4/0/0 | 10.30 | 6440 / 5722 / 3815 |
| fortinet.get_system_status | 3/4 | 4 | 25% | 0.0% | 2/2/0 | 22.23 | 11053 / 18871 / 14931 |
| cisco_s300.show_lldp_neighbors | 4/4 | 3 | 50% | 16.7% | 4/0/0 | 10.17 | 6172 / 7674 / 6074 |
| paloalto_panos.show_interface_hardware | 2/4 | 3 | 50% | 16.7% | 4/0/0 | 12.43 | 15753 / 9879 / 7140 |
| hp_procurve.show_interfaces_status | 4/4 | 4 | 25% | 0.0% | 4/0/0 | 22.14 | 6948 / 21264 / 19449 |
| oneaccess_oneos.show_voice_mos | 3/4 | 4 | 25% | 0.0% | 4/0/0 | 14.07 | 12592 / 11558 / 8986 |
| cisco_nxos.show_interface_status | 4/4 | 1 | 100% | 100.0% | 3/1/0 | 5.17 | 11916 / 4014 / 2266 |
| cisco_ios.show_sdwan_control_connections | 4/4 | 4 | 25% | 0.0% | 3/1/0 | 83.02 | 6804 / 79198 / 76554 |
| mikrotik_routeros.system_resource_print | 4/4 | 2 | 75% | 50.0% | 4/0/0 | 13.63 | 6308 / 12258 / 9522 |
| arista_eos.show_version | 3/4 | 3 | 50% | 16.7% | 4/0/0 | 8.26 | 9410 / 7328 / 4681 |
| oneaccess_oneos.show_tacacs | 4/4 | 2 | 75% | 50.0% | 4/0/0 | 4.89 | 6900 / 3636 / 1796 |
| cisco_ios.show_ip_ospf_database_router | 3/4 | 4 | 25% | 0.0% | 4/0/0 | 10.50 | 16531 / 10180 / 6373 |
| cisco_nxos.show_port-channel_summary | 3/4 | 4 | 25% | 0.0% | 3/1/0 | 14.76 | 14001 / 12580 / 10189 |
| cisco_asa.show_vpn-sessiondb_anyconnect | 4/4 | 4 | 25% | 0.0% | 2/2/0 | 16.80 | 7304 / 15781 / 12862 |

## 语义问题及证据

| 机制 | 用例与 trial（1 起算） | 受影响 trial | 证据路径 |
| --- | --- | --- | --- |
| 根对象错误地描述单个实体 | Port-channel #3；VPN #3、#4 | 3，覆盖 2 例 | `/` |
| 明确独立的版本/日期/状态计数保留为复合字段 | Fortinet #1、#4 | 2，覆盖 1 例 | `/version`、`/virus_db`；#4 另有 `/virtual_domains_status` |
| description 要求把已有占位字符串改为空字符串 | NXOS interface #3 | 1 | `/interfaces/*/name`、`/interfaces/*/vlan`、`/interfaces/*/type` |
| 多行表头的业务限定未进入名称或说明 | SD-WAN #1 | 1 | `/peer_connections/*/id` |

根粒度问题最严重：输入包含多个聚合组或会话，但对应 Schema 把组号、名称或用户名直接放在根层，没有承载重复实体的数组。它符合 JSON Schema 语法，却不能在一个根 record 中忠实表达整份已展示输入。成功 trial 中的容器数组提供机制对照，不作为标准答案。

Fortinet 的版本信息包含多项独立身份/版本值，数据库条目也明确包含版本与更新时间。两次提案进行了拆分，另两次按整行复合值保留；#4 还把多个模式计数合并为一条字符串。这里按本轮“独立逻辑值合理拆分”维度判为需修正。该判断不意味着所有带标点的字符串必须拆分：设备完整型号、带单位的单个量、持续时长和自由描述可以作为一个逻辑值。VPN 的算法部分存在带隧道注释与无注释列表两种形式；完整字符串保留了全部信息，本轮未将该回退本身认定为明确业务错误，但应在后续拆分专项验证关联关系。

NXOS interface 的四次字段路径、类型和 required 完全一致，但 #3 的说明改变了已有占位字符串含义；其余三次明确保留。故结构签名无法发现此问题，必须保持语义审阅。

SD-WAN #1 将末列仅命名为 id 且无 description，在多个 ID 共存时无法从契约明确其业务身份。其余三次保留了完整业务限定。该问题与 Python 标识符合规不同。

其余合理差异包括：同义字段名称、ID 的 string/integer 选择、固定两小时章节采用两个 object 或带 period 的 array、图例是否单独建模、Arista uptime 的字符串或组成量对象。仅凭差异不扣语义分。HP 空槽均允许 string；部分提案将空槽字段设为可选，较宽松但不等于已要求丢弃它们。

MOS #3 的数组最小数量限制缺少跨输出证据，Port-channel #3 同样有最小成员数限制；对应维度标为证据不足。前者没有当前可见业务错误，仍可接受；后者因根粒度明确错误判需修正。并未用隐藏输入或历史提案推定约束错误。

## 拒绝、成本与观察边界

7 次拒绝全部包含 `schema.forbidden_keyword`：`/$id` 4 次，MAC 字段 `pattern` 2 次，`/$defs` 1 次。最后一项同时伴随 2 条 `schema.invalid_type` 和 1 条 `schema.no_leaf_fields`；合计 10 条诊断，仍只影响 7 次提交、7 个 trial、6 个用例。全部下一次提交冻结。它们是受限 Schema 子集兼容问题，不是 Python 属性命名错误。

63 次模型轮次/LLM span；输入 Token 138132，输出 Token 219943，其中推理 Token 184638（83.9%），推理观测率 63/63。Laminar 记录费用合计 $0.27263136，仅作为服务记录的辅助指标。trial 耗时总和 993.47 秒，中位数 10.60 秒、最大 163.72 秒；并发窗口约 259.4 秒。

`context.fit` 56 次，没有发现 compression span；TTP 工具调用为 0。SD-WAN 四次耗时均值 83.02 秒，其中 #1 推理 Token 38990、总输出 39814，单轮总耗时 163.72 秒。配置中的 max_tokens 与 HTTP timeout 不应被误读为“包含一切推理和整轮工作的硬上限”；本轮仅报告观测，未调整参数或展开 provider 语义专项。

每次成功提案的属性/叶子数量、最大深度、类型分布、required 数、description 覆盖率及人工 Schema 差异数量保存在随附 JSON。这些都是描述统计：例如 Fortinet 属性数 27–50、MOS 12–22，不能以多者为优；description 从 0% 到 100% 也不是质量分数。人工 Schema 覆盖多输入，未展示的可选字段差异不判遗漏。

逐条七维审阅、总体分类、受控问题路径及提交 span 标识均见 [脱敏结果](schema-only-v40-results.json)。Trace 正文仅在 Laminar UI 只读查看，本地未保存提案、原文、模型文本、description、enum 值、结构签名或内容哈希。

## 下一步建议

1. **先明确整份输入与重复实体的根粒度。** 两个用例、3 个 trial 有直接证据，属于会使后续 TTP 无法完成的契约问题。建议只做通用章节/表格的根对象与重复数组指导和合成回归，不用字段名黑名单。
2. **统一受限 Schema 子集的生成指导。** 六个用例、7 个 trial 产生禁止关键字后又自行修复；可减少重复成本，但本批不是最终失败来源，不宜把零拒绝当语义提升。
3. **将值保真明确用于 Schema description。** 一个 trial 明确指示错误清洗；优先禁止将原有状态/占位值改写为空槽，再验证阶段提示是否保持一致。
4. **再定义结构粒度和业务限定的命名原则。** Fortinet 的拆分粒度及 SD-WAN 的含糊 id 有明确问题；其余 13 个用例的结构漂移不全是错误。先确定何时拆复合值、何时保留整值以及如何继承多行表头限定，再考虑词典或更强一致性策略。

本轮首选第 1 项：根粒度约定。尚未实施任何优化。以上为 v40 Schema 首轮基线，不与既有 TTP-only 56/60、32/32 比较；每例四次不足以宣称稳定性或统计显著提升。
