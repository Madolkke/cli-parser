# 真实命令输出语料测试计划

<!-- markdownlint-disable MD013 -->

## 1. 目的与边界

`testdata/real_command_outputs/` 保存公开项目中的真实命令输出夹具，用于检验同一 TTP 模板能否解析同一命令的多份完整输出。语料是开发期验收资产，不属于 `evals/` 或 `examples/`，不进入 pytest，也不随 `cli_parser_agent` Python 包发布。

独立脚本 `scripts/run_live_corpus.py` 负责语料检查和真实模型闭环。它不是产品 CLI，不改变 `TtpGenerator` 的公共 Python API，也不会执行夹具中出现的任何命令。

## 2. 固定语料

语料固定到以下上游版本，具体文件及本地 SHA-256 记录在 `testdata/real_command_outputs/corpus.json`：

| Source | Version | Commit | License |
| --- | --- | --- | --- |
| `networktocode/ntc-templates` | `v9.2.0` | `891746e659e3a25d5065ee9dac29e7de5760bdf7` | Apache-2.0 |
| `dmulyalin/ttp_templates` | `0.5.9` | `307f16812503f3470897020c2267101bcf7af5d5` | MIT |

完整语料包含 `13` 个 case、`40` 份文本：

| Case ID | Command | Samples |
| --- | --- | ---: |
| `ntc.cisco_ios.show_interfaces_status` | `show interfaces status` | 3 |
| `ntc.cisco_ios.show_ip_interface_brief` | `show ip interface brief` | 1 |
| `ntc.cisco_ios.show_cdp_neighbors_detail` | `show cdp neighbors detail` | 2 |
| `ntc.cisco_ios.show_interfaces` | `show interfaces` | 5 |
| `ntc.linux.ip_route_show` | `ip route show` | 3 |
| `ntc.arista_eos.show_interfaces_status` | `show interfaces status` | 4 |
| `ntc.huawei_vrp.display_version` | `display version` | 5 |
| `ntc.fortinet.get_system_status` | `get system status` | 5 |
| `ntc.juniper_junos.show_interfaces` | `show interfaces` | 3 |
| `ttp.linux.ip_address_show` | `ip address show` | 3 |
| `ttp.cisco_ios.show_inventory` | `show inventory` | 2 |
| `ttp.cisco_ios.show_running_config_pipe_section_interface` | `show running-config \| section interface` | 2 |
| `ttp.cisco_xr.show_bgp_neighbors` | `show bgp neighbors` | 2 |

`smoke` suite 是固定的快速闭环，共 `5` 个 case、`12` 份文本：

- `ntc.cisco_ios.show_interfaces_status`：3 份；
- `ttp.linux.ip_address_show`：3 份；
- `ttp.cisco_ios.show_inventory`：2 份；
- `ttp.cisco_ios.show_running_config_pipe_section_interface`：2 份；
- `ttp.cisco_xr.show_bgp_neighbors`：2 份。

## 3. 无模型凭据检查

列出 smoke case：

```powershell
uv run python scripts/run_live_corpus.py list --suite smoke
```

执行完整 preflight：

```powershell
uv run python scripts/run_live_corpus.py preflight
uv run pytest -m "not live" -q
uv run ruff check .
```

`list` 和 `preflight` 不读取模型配置，也不产生网络请求。Preflight 必须确认 manifest 恰好包含 `13` 个 case 和 `40` 份文件，并逐份检查：

- 文件存在、非空、严格 UTF-8、LF 换行且无 BOM；
- UTF-8 编码后小于 `1 MiB`；
- 不含 NUL、ANSI 控制序列、分页符、命令回显或设备提示符；
- 不匹配凭据模式；
- 文件 SHA-256 与 manifest 一致。

任何 manifest、文件或配置错误都必须使用退出码 `2`。

## 4. 真实模型闭环

先通过环境变量配置 OpenAI 兼容模型：

```powershell
$env:OPENAI_API_KEY = "..."
$env:OPENAI_MODEL = "..."
$env:OPENAI_BASE_URL = "https://example.com/v1" # optional
```

先执行 smoke，再在同一结果目录上继续完整语料：

```powershell
uv run python scripts/run_live_corpus.py run --suite smoke
uv run python scripts/run_live_corpus.py run --suite all --resume <smoke-run-dir>
```

默认 suite 为 `smoke`、并发为 `1`。需要诊断或控制成本时可筛选运行：

```powershell
uv run python scripts/run_live_corpus.py run --case ntc.cisco_ios.show_interfaces_status
uv run python scripts/run_live_corpus.py run --suite all --source ntc --platform cisco_ios --max-cases 2
uv run python scripts/run_live_corpus.py run --suite all --concurrency 2 --output-dir .artifacts/live-corpus/manual-run
```

`--case` 覆盖 suite 选择，之后仍应用 `--source` 和 `--platform` 过滤；`--concurrency` 只允许 `1-4`。`--output-dir` 与 `--resume` 互斥。`--resume` 只跳过语料哈希和 `GenerationResult.metadata.prompt_version` 均与当前运行一致、且再次通过独立全文验收的成功结果；旧提示版本、失败或缺失的 case 都会重新执行。

每个 case 只调用一次 `TtpGenerator.generate`，并按 manifest 顺序把该 case 的全部样例放进一个 `GenerationRequest`。运行结果默认写入 `.artifacts/live-corpus/<run-id>/`，每个 case 保存完整 `GenerationResult` 和独立验收结果，根目录保存 `summary.json`。

退出码约定：

- `0`：选中 case 全部生成并验收成功；
- `1`：至少一个 case 生成或独立验收失败；
- `2`：环境配置、命令参数或语料错误。

## 5. 验收标准

成功结果必须在 Agent 外重新使用全部原始输入完成 TTP 安全检查与全文解析，并确认：

- 每份输入恰好得到一个根 object；
- records 数量、顺序和内容与 artifact 中的 records 一致；
- 每个 record 都符合冻结的 Draft 2020-12 JSON Schema；
- 失败可归类为 `model`、`generation`、`schema`、`ttp` 或最终验收问题。

Smoke 阶段必须达到 `5/5` case 成功；完整代表集最终必须达到 `13/13` case 成功。修复问题后使用 `--resume` 仅重跑失败或缺失项。完成这两项前，不能把公开语料闭环声明为通过。

## 6. 隐私与第三方内容

这些文件是公开上游仓库中的 raw CLI 测试夹具，可能已由上游脱敏或整理，不应描述为未经处理的生产设备采集。公开文本中的 hostname、IP、MAC 和序列号按原样保留；把未来的私有采集数据发送给模型前必须先脱敏。

两份上游完整许可证和来源版本说明保存在 `testdata/real_command_outputs/licenses/`。仓库根 `LICENSE` 只声明本项目的 Apache-2.0 许可证，不替代第三方夹具各自的许可证和归属信息。
