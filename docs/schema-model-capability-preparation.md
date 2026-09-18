# 模型能力实验的离线准备

本记录对应[待确认方案](schema-model-capability-proposal.md)。没有发起真实模型请求，
没有修改产品默认；Pro 请求仍等待此前的模型切换确认。现有 v51 结果不与新实验拼接。

## 固定实现与环境

- 主工作区 revision：`ddebc33db7e45f0b2d569fddbbc1189eea4d3485`。
- 隔离目录：`C:/Users/madol/.codex/worktrees/schema-model-capability/cli-parser`。
- 固定 revision：`c5ecd82152f02049f29089a321d26bcb924cdafd`。
- 实际导入的 generator 和 prompt 均来自隔离目录，提示版本为
  `ttp-generator-v51-schema-length-recovery-zh-cn`；没有导入主工作区的回退版本。
- `uv sync --locked` 成功；两目录均为 Python 3.12.9，114 个已安装 distribution
  的版本全部相同，`uv.lock` 字节相同。AgentScope 为 2.0.4.post1、OpenAI 为
  2.46.0、lmnr 为 0.7.56。
- 复制并在内存逐字核对101个标准资产文件及注册表，共102个文件；未保存内容哈希，
  未复制 `.env`。这只证明当前两个目录一致，不补证历史 v48 输入正文一致。
- 隔离目录的已跟踪源码无改动；dirty 仅来自复制的注册表和六组现有未跟踪测试资产。
  主工作区已有无关修改保留。

## 已执行的离线核对

使用实际公共 `propose_schema()`、OpenAI SDK 和 `httpx.MockTransport`，以独立合成
输入模拟两种路径：首轮合法提交，以及连续三次纯推理 `length` 后第四轮合法提交。
分别使用 Flash 和 Pro，共四次模拟公共调用、十次模拟 HTTP 请求。显式关闭 Laminar
初始化，未加载凭据；没有供应商请求。

模型与 policy 从 v51 的脱敏配置重建。逐轮比较实际序列化请求，**唯一差异键为
`model`**；客户端 timeout 和重试设置也相同。两种模型均成功冻结合成 Schema。

- 每次发送 `max_tokens=8192`，没有 `max_completion_tokens`。
- temperature 为0、非流式、`parallel_tool_calls=false`，唯一工具是
  `submit_result_schema`。
- 正常请求均省略 `thinking`、`tool_choice`；三次纯截断后，两种模型均执行固定
  实现已有的关闭 thinking 与强制唯一工具分支。
- 完整 messages、工具定义及其余请求字段在内存相等；不导出请求正文。

隔离目录执行 `scripts/run_test_sets.py preflight --registry evals/datasets.toml
--input-scope full`：通过，16个 complete、1个 template、0个失败。底层模板仍产生
已存在的转义 SyntaxWarning，未导致验收失败。固定实现此前的完整工程验收记录见
[v51协议](schema-length-recovery-v51-protocol.md)；本次未改源码或重复完整测试。

这些检查只证明本地调用路径和实验隔离，不证明 Pro 的真实行为、语义质量或稳定性。
实际获准启动前仍须重新检查有效环境配置、资产与源码状态，以及 Laminar 的写入、
flush、回读和授权只读正文访问。

## 逐例保护门槛

以下展开既有 v51 门槛，不新增一致率要求。每例四次；业务、政策、联合列是最低
通过数，机制列是最多受影响 trial 数。同一机制在一个 trial 中只计一次。

| ID | 用例 | 业务 | 政策 | 联合 | 既有业务机制上限 |
| ---: | --- | ---: | ---: | ---: | --- |
| 1 | Broadcom version | 4 | 4 | 4 | 0 |
| 3 | Fortinet status | 0 | 4 | 0 | 独立业务值未拆分4 |
| 5 | Cisco S300 LLDP | 4 | 4 | 4 | 0 |
| 6 | Palo Alto hardware | 3 | 4 | 3 | speed/duplex/state未拆分1 |
| 7 | HP interfaces | 4 | 4 | 4 | 0 |
| 9 | OneAccess MOS | 4 | 4 | 4 | 0 |
| 10 | NXOS interface | 4 | 4 | 4 | 0 |
| 11 | SD-WAN（首阶段） | 4 | 4 | 4 | 0；政策未知不通过 |
| 13 | MikroTik resource | 4 | 4 | 4 | 0 |
| 14 | Arista version | 4 | 4 | 4 | 0 |
| 16 | OneAccess TACACS | 3 | 0 | 0 | 无依据的非空集合约束1 |
| 17 | OSPF database | 4 | 4 | 4 | 0 |
| 18 | Port-channel summary | 0 | 4 | 0 | 独立业务值未拆分3；无依据的非空集合约束2 |
| 19 | ASA AnyConnect | 4 | 4 | 4 | 0 |

业务指 `overall=acceptable`；政策指八项检查均通过或确实不适用；联合指同一trial
生成、复验、业务及政策均通过。联合下限由既有业务、政策门槛蕴含，不是新增门槛。
全部用例另须生成及复验4/4，已运行trial和有效pair审阅完整。

ID11先完成并审阅四次，全部过线才发起其余13例的52次请求。ID10、13、14还须各
6/6 pair合理、完整契约及说明语义一致；SD-WAN及其他十例的pair差异完整报告，
不增加全一致门槛。保护13例业务/政策/联合下限合计为42/48/39，仅用于核验，
不能用合计抵消某个用例的劣化。

业务机制按“用例、业务位置及错误机制”人工对应，不能用粗分类额度容纳新错误。
例如新的拆分遗漏不能因为同属 `split_merge` 就计入旧问题额度。所有失败、新机制
和边界项独立复核；分歧未解决时保留未知。

TACACS历史政策问题为容器限定遗漏4次、字段限定遗漏1次，单独跟踪。本方案继承的
门槛只有每例政策通过数不得下降，并未规定逐政策机制上限；不能在运行后把诊断
计数解释成事先约定的门槛。历史输入正文和当前快照的逐字一致性也未获证明。

未达标或证据不足时停止、不补跑，主工作区默认保持现状；隔离候选无需混入主工作区。
准备结果尚不能证明 SD-WAN 稳定或其他用例未劣化，目标保持未完成。
