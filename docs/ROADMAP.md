# Roadmap

## 当前 Schema proposal 兼容基线

Schema proposal 的根 `$schema` 可以省略；显式提供时仍必须声明 Draft 2020-12，
系统不会修改或自动补全冻结 Schema。每个叶子路径必须有至少一条真实 evidence，
同一路径允许多条并逐条验证。evidence 总数默认上限为 `256`，可以通过
`GenerationPolicy.max_schema_evidence` 或 `CLI_PARSER_MAX_SCHEMA_EVIDENCE` 在
`1..256` 内向下收紧；该资源上限不暴露在 Agent 工具 Schema 中。

## TTP 转换结果的来源追踪

当前版本暂不执行“最终标量必须作为原文子串出现”的校验。原因是 TTP
白名单允许 `to_cidr`、`to_ip`、`to_net` 等确定性转换，而转换后的表示可能
与原文不同，例如 `255.255.255.0` 变为 `24`，或 IPv6 地址被压缩。直接对
最终 record 做文本子串匹配会误拒绝这些合法结果。

后续重新启用来源追踪时，应按转换链设计，而不是恢复全局字符串匹配：

1. 在 TTP 解析阶段保留每个结果叶子的原始 capture、输入索引和转换流水线，
   不把来源信息写入公共 `records` 或失败结果。
2. 为每个允许的转换定义确定性证明规则：直接捕获要求原文连续片段；
   `to_int`/`to_float` 使用无损数值转换；`to_ip`/`to_net` 比较规范化的地址
   对象；`to_cidr` 从原始 netmask 复算前缀长度；`joinmatches` 证明每个
   组成片段均来自同一输入。
3. 对不能安全证明的派生值返回结构化 issue，区分“来源缺失”和“转换不受
   支持”，不泄露原文、解析值或模板正文。
4. 保留现有的 Schema 回验、隔离执行、记录一一映射和空字符串占位检查；
   来源追踪恢复后不得改变缺失可选字段必须省略的语义。
5. 增加确定性测试覆盖 `to_cidr`、IPv4/IPv6 规范化、网络转换、前导零、
   `joinmatches`、数组路径和多输入索引，并确认 Laminar/observer 只获得
   既有安全调试通道中的来源快照。

恢复条件：转换规则完成、每种允许转换都有正反例、完整单元测试通过，并在
真实语料上确认不会把合法转换误报为 `ttp.scalar_without_source`。
