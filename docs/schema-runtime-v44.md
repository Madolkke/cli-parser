# v44 Schema 运行契约修复

共同对照底座保持 v40 的业务建模政策，提示版本改为
`ttp-generator-v44-schema-runtime-contract-zh-cn`。受限 Schema 能力说明直接使用
校验器的关键字声明，不扩大校验能力或修改外部注入协议。

无工具、错误阶段工具、框架参数拒绝及产品参数拒绝参与连续协议失败计数。
最多三次固定修复提醒，第四次停止；原无工具预算仍独立生效。合法工具入参重置序列，
业务校验拒绝、worker 异常和内部异常分开记录。错误轮的调用及结果成对移除，
模型下一轮收到固定参数结构，初始任务和已合法的业务反馈保留。
公开失败分类沿用既有类型，观察事件额外记录 `protocol_retry_limit`。

## DeepSeek 输出预算映射

2026-09-16 核对实际端点为 `api.deepseek.com`。锁定 AgentScope 将 `max_tokens`
发为 `max_completion_tokens`；DeepSeek [官方接口说明](https://api-docs.deepseek.com/api/create-chat-completion)
规定使用 `max_tokens`，其范围包含 8192，thinking 模式未配置时默认为 64K。
历史 v43 的 85 次请求中 10 次输出用量大于 8192，最大 63032；这些历史数值不能
单独证明供应商如何处理未知参数，但与已核实的接口映射缺口一致。

适配器仅针对精确官方 hostname 将预算发送为 `max_tokens`，用 OpenAI SDK 的
NOT_GIVEN 排除 `max_completion_tokens`；不改变模型配置或其他端点的序列化。
官方端点禁止 extra_body 覆盖两个输出预算键。模拟 HTTP transport 验证最终 JSON
恰好包含一个正确的预算字段、值为 8192，并验证相似恶意域名不触发适配。
没有为此运行额外模型探针。新实验三组都使用修正映射，历史成本不能作为严格同期对照。

相关离线 runner、协议及生成测试通过；实际 SDK 序列化与覆盖拒绝五项测试通过。
这证明接口适配和止损机制成立，不构成业务合理性提升证据。正式模型运行仍需全量离线验收。
