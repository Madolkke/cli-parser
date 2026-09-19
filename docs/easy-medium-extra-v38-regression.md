# Easy / Medium 扩展回归：2026-09-13

## 范围与配置

本轮共 15 个用例、60 次 trial，每例重复 4 次、全局并发 4；使用默认输入 `001.txt`。
原有 Easy/Medium 八个用例和新增 extra-easy/extra-medium 七个用例一起运行；Hard 和已禁用 Huawei ONT 不参与。
从当前未提交资产工作区运行，Git 提交为 `597dc85bd147ebc4ec2b05ff380c57bf921249a4`，dirty=true。
采用 estimator 观察适配器委托标准 `scripts/run_test_sets.py` 执行 ttp-only。
模型、policy 和提示全字段与历史 v38 Easy/Medium 配置比对一致：deepseek-v4-flash、temperature 0、非流式、
max_tokens 8192、context 128000、HTTP 120 秒、重试 2、总预算 1800 秒、26 轮、18 次提交、3 次测试。
Juniper 已拆分 uptime/用户数/三项负载，OSPF 已补齐 router_id/process_id/area_id；产品提示仍为 v38。

默认及 full-scope preflight/baseline 在禁用 Huawei ONT 后均通过，17/17 complete；full-scope template smoke 1/1。
没有新增真实 trial 或补跑失败 trial。

## 严格结果

总体 **56/60（93.33%）**；原有 Easy 16/16、Medium 16/16，合计仍为历史同范围的 32/32。
新增 extra-easy 8/12、extra-medium 16/16，合计 24/28。

| 分组 | 用例 | 严格通过 | 平均秒数 |
| --- | --- | --- | --- |
| extra-easy | arista_eos.show_version | 3/4 | 152.57 |
| easy | broadcom_icos.show_version | 4/4 | 196.52 |
| extra-medium | cisco_asa.show_vpn-sessiondb_anyconnect | 4/4 | 80.48 |
| extra-medium | cisco_ios.show_ip_ospf_database_router | 4/4 | 78.00 |
| medium | cisco_ios.show_sdwan_control_connections | 4/4 | 44.15 |
| medium | cisco_nxos.show_interface_status | 4/4 | 96.69 |
| extra-medium | cisco_nxos.show_port-channel_summary | 4/4 | 121.47 |
| medium | cisco_s300.show_lldp_neighbors | 4/4 | 34.62 |
| easy | fortinet.get_system_status | 4/4 | 47.54 |
| medium | hp_procurve.show_interfaces_status | 4/4 | 104.75 |
| extra-easy | juniper_junos.show_system_uptime | 1/4 | 80.98 |
| extra-easy | mikrotik_routeros.system_resource_print | 4/4 | 23.75 |
| extra-medium | oneaccess_oneos.show_tacacs | 4/4 | 88.77 |
| easy | oneaccess_oneos.show_voice_mos | 4/4 | 298.12 |
| easy | paloalto_panos.show_interface_hardware | 4/4 | 32.58 |

60/60 trial 均产生有效候选、显式 finish 成功、产品生成成功和 runner 独立验收通过。
四次严格失败均为最终 records 与 expected 不完全相等；没有以结构验收代替严格评分。

| 失败用例 | Trial（从 1 编号） | Trace ID |
| --- | --- | --- |
| arista_eos.show_version | 2 | `22cb6cd5-1fe2-6c17-5013-6ac1481ad937` |
| juniper_junos.show_system_uptime | 2 | `f11facf1-2036-10b5-063b-63016a1f6c7e` |
| juniper_junos.show_system_uptime | 3 | `91a3f9f1-eddd-dec1-4706-d6e00b400dd2` |
| juniper_junos.show_system_uptime | 4 | `bc1e4681-7aaa-1138-0774-cc71c501728a` |

本轮仅使用脱敏结果与结构统计，未审阅 Trace 正文，具体差异字段及机制尚未归因。
Juniper 三次失败均无 Schema 拒绝：前两次各提交一次后 finish，另一条两次提交均 accepted。
Arista 失败 trial 经 5 次提交、3 次独立测试后 finish，其中 3 次提交 Schema 拒绝，最终仍有内容差异。

## 执行与观测

- 共 85 次提交，其中 69 次 accepted、14 次 Schema 错误；另 2 次拒绝不在本聚合中细分。
- 独立测试 31 次，29 次 parse-only 成功。提交 worker 错误 0/85，测试 worker 错误 0/31；SystemExit 和裸管道错误均为零。
- 177 次模型调用；Laminar 与本地 LLM/提交/测试计数均一致，60/60 Trace 有模型和工具观测。
- 输入 Token 4,227,817；输出 Token 1,169,733，其中推理 Token 1,133,352（已包含于输出，96.89%）。
- 整批 trial 墙钟 1552.13 秒（约 25.9 分钟）；平均请求 98.73 秒，首次有效候选平均 82.20 秒。
- context.fit 60 次、原生压缩 0 次、旧提交结果折叠 25 次。
- sidecar 60/60 唯一匹配，无观测省略；177/177 实际请求初始任务完整、工具配对和顺序检查通过。
  无显式 tool_choice 或 reasoning_content 发送。初始任务完整不等于逐字证明全部历史候选完整。

运行前启动 Docker 恢复 Laminar；`127.0.0.1:8000` 被另一 Python 服务占用，
仅在评测进程覆盖 `LMNR_BASE_URL=http://localhost`。probe 写入、flush、SQL 回读通过，
探针 Trace 为 `02a149b2-029f-1c08-ba4a-65fce54f5b21`。后续汇总使用 IPv6 直连 transport 读取数值，绕开环境代理，未变更服务或产品配置。

## 结论与产物

原有 Easy/Medium 的本轮四重复保持全通过；新增字段更细的 Juniper 和 Arista 暴露内容保真问题，
下一步应在 Laminar UI 核对这四条 Trace 的最终 records 与模型可见反馈，再决定修复位置。
本轮未修改产品或标准答案来消除失败；四重复不代表长期稳定正确率为 100%。

后续已单独完成四条失败 Trace 的 UI 审阅，见 [失败诊断](easy-medium-extra-v38-diagnosis.md)。
该诊断不改变本报告运行时的统计与观察范围。

数值产物：
.artifacts/easy-medium-20260913/evaluation/20260913T045709.518354Z/summary.json
.artifacts/easy-medium-20260913/aggregate.json
.artifacts/easy-medium-20260913/estimator.json
.artifacts/easy-medium-20260913/mechanisms.json
