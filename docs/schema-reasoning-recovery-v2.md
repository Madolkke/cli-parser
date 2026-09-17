# Schema 长度截断保护与低强度推理恢复

目标仍是 SD-WAN 稳定生成且现有用例不劣化。v1 关闭 thinking 后四次仅三次生成，
没有业务可接受提案，结果见 [报告](schema-reasoning-recovery-v1-results.md)。该恢复方式
不采用。本候选保持 v48 提示、工具说明、8192 输出预算、采样、API 与 TTP 行为。

## 两个独立机制

1. Schema 原始供应商回复以 `length` 结束时，其中全部工具调用在框架 JSON 修复及
   执行之前丢弃。即使截断后参数碰巧合法，也不得冻结。流式工具片段先在适配器内存
   缓冲，结束原因可知后才交给 Agent；取消与异常不能释放未完成调用。该保护对所有
   Schema 供应商生效，外部注入和 TTP 不变。它只证明回复完成，不证明字段完整。
2. 仅官方 DeepSeek、默认推理配置，连续三个完整模型轮次出现含推理、无正文的
   `length` 后，使用下一次既有重试设置 `reasoning_effort=low`。被丢弃的截断工具
   同样视为未完成输出。普通文本或正常工具回复不满足条件；前三次请求不变。

恢复不关闭 thinking，不改变模型、工具、提示、输出预算、HTTP 超时、采样或重试次数。
只在 runner 已确认仍允许修复且未触发 deadline 后激活；显式 reasoning/thinking/
extra_body 配置不覆盖，状态限于一个 Schema 请求。恢复后该阶段继续低强度，新的公共
请求重新默认开始。首份合法且非截断的提交照常冻结，不读取 Trace 或回灌推理。

供应商依据（2026-09-18）：[Chat Completions](https://api-docs.deepseek.com/api/create-chat-completion)
与 [Thinking Mode](https://api-docs.deepseek.com/guides/thinking_mode) 明确默认启用推理，
支持 low。离线 transport 验证实际 JSON；文档支持不等于真实模型一定收敛。

运行时分别标记 `schema-truncated-submission-guard-v1` 和
`schema-reasoning-recovery-v2`；提示仍为 v48。观察只保存受控类别、数量、轮次。
runner v5/metrics v2/review v2 保持兼容；没有观察事件时标不可用，不补零。

## 离线与真实验证

覆盖流式/非流式截断、合法参数截断、取消、usage、跨请求隔离、显式设置、HTTP 重试、
阶段预算、前三轮 wire 保持不变与 TTP 隔离。完成完整 pytest、JavaScript、Ruff、格式、
diff、默认/full preflight 与 baseline。核对提示和标准资产未变，保留已有脏文件。

真实验证独立成批，不续接失败的 v1。统一入口 `scripts/run_test_sets.py run --mode schema-only`：
各默认 001，每例四次，全局并发四；deepseek-v4-flash，temperature0，非流式，8192/128000，
HTTP120秒、模型重试2、1800秒/26轮，其余配置与 v48 逐项匹配。启动前验证 Laminar
写入、flush、回读和正文可读，记录 revision、dirty、输入路径和采样事实。

- 先 ID11 四次；全部生成复验、业务可接受、适用政策通过且证据完整后才继续。
- 再 ID1,3,5,6,7,9,10,13,14,16,17,18,19，各四次，共52次。各例业务及政策通过数不低于
  v48，没有新增或扩大机制；ID10/13/14仍须各4/4联合通过、6/6合理契约及说明语义一致。
- 全部 trial/有效 pair 只读审阅；特别检查独立列、空槽、占位值、表头限定和完整覆盖。
  SD-WAN 不能只以生成成功通过；合理表达差异与业务错误分开。其他例一致性分别报告。

本候选最多56次公共请求，无失败补跑。首阶段失败停止候选，不开启保护批次；实现缺陷
导致行为变化时也停止原批次。未通过则撤下恢复行为，保留独立截断防护、诊断与结果，
继续依据证据推进目标。全部通过前不宣称已恢复稳定或已保护全范围。

比较范围为历史14例默认输入；没有同期对照，旧模型别名路由有变化，不能把结果差异
全部归因于本改动。Hard、额外输入与可解析性仍未测。本地不保存正文、Schema、description、
records 或内容哈希；完整内容仅在授权只读 Laminar 通道内存审阅。
