# v43 业务值拆分实验状态

本轮只修改 v40 的 Schema 建模指导及必要的提交工具说明，版本为
`ttp-generator-v43-semantic-value-boundaries-zh-cn`。不恢复 v42 标签命名指导。
独立业务含义与可靠边界同时成立时拆分，完整版本号、时间戳、时长及带符号值默认保留整体。

## 验收和启动状态

已完成提示、直接提取示例的合成测试、模拟 transport 阶段隔离测试及文档。
AST 比较确认 TTP 系统提示、任务构造、重试提示正文不变。
相关测试 53 项通过；补充第二份缺项合成输入后相关复验 7 项通过。
JavaScript 31 项、Ruff、格式及 diff 检查通过。默认及 full-scope preflight 无失败，
baseline 均 16/16，full Huawei smoke 1/1。完整 pytest 1137 passed、3 live skipped（425.11 秒）；
补充示例文本后的相关复验已通过，全部离线验收通过。

2026-09-16 Laminar Docker 服务正常，探针写入、flush 和 SQL 标识回读通过：
`c2560df4-a4f0-7af3-0b6a-ed2079cb0240`。
浏览器审阅不可用：内置浏览器连接不支持当前认证方式；Windows 浏览器工具无法可靠
确认当前 URL，已停止本轮 Computer Use。没有改用 SQL 导出正文。
**真实 trial 尚未启动，完成数 0/56；没有模型效果结论，也尚未满足产品采用条件。**
当前 v43 是待评测实现，待 UI 正文审阅能力恢复后继续，不重新创建评测批次。

## 待完成的唯一回归批次

使用标准 `scripts/run_test_sets.py run --mode schema-only`，数据集 ID 为
`1,3,5,6,7,9,10,11,13,14,16,17,18,19`，默认 `001.txt`，各 4 次、并发 4。
deepseek-v4-flash，temperature 0，非流式，8192 max_tokens、128000 context，
HTTP timeout 120 秒、模型重试 2、总预算 1800 秒及 26 轮。
进程级启动器已逐项核对 model/policy 与最新 v40 基线一致；不修改产品默认预算。
运行前再次核对环境、探针、UI、Git revision/dirty 状态及输入选择。

56 份提案和最多 84 对比较沿用 metrics v2、七维审阅和受限 schema-review。
采用必须同时满足：生成/复验 56/56；审阅完整；至少 51/56 可接受；Fortinet
拆分问题最多 2/4、Port-channel 最多 1/4，合计最多 2/8；合理且一致至少 8/84，
合理 pair 中一致率至少 8/67；没有新增机制或过度拆分；HP 空槽问题最多 1/4，
SD-WAN 限定与覆盖问题各最多 1/4。跟踪 OSPF 覆盖缩减和 Port-channel 根粒度错误。

任一门槛失败，以新提交恢复 v40 提示、工具说明、版本和默认文档，保留独立诊断测试
与结果报告，移除依赖未采用提示的断言并重新运行全部离线验收。
不补跑、不自动叠加优化。可解析性未测。

Trace 正文仅在 Laminar UI 只读审阅，本地只保存受控分类、计数、有界问题路径及标识，
不导出输入、Schema、description、records、模板或内容哈希，不回灌模型。
保留既有无关工作区改动；历史报告与评测资产不变。
