# M5d：只读排空预检报告

> **历史记录**：本文保留当时的设计、命令与验证结果，不代表当前独立应用的部署结构或最新测试结论。当前操作见[运行说明](../operations.md)，新库结构见[初始化说明](../architecture/fresh-schema.md)；历史类名、版本号和外部导出命令仅用于理解证据。

日期：2026-10-15。实现基线为本地提交 `3736aac`（M5c，未推送）。本阶段复用 M5b 的受理/排空状态和 M5c 的分页明细，提供 [inspect-drain.py](../../scripts/inspect-drain.py)，将运维观察保存为本地 JSON，供人工复核和留档。

脚本只调用 `GET /v1/operations/drain` 和 `GET /v1/operations/drain/items`，没有暂停、恢复、审批、取消、重投、清理或服务切换操作。它不创建会话或 Turn，也不调用模型、MCP 和飞书。已有隐私 GET 会初始化 owner，因此不纳入只读预检；隐私待清理状态直接读取排空报告中的 `pending_forget`。

## 使用

先按 [M5b](m5b-admission.md) 在需要时暂停当前主体受理，再对已授权的部署执行：

```bash
python3 scripts/inspect-drain.py \
  --base-url http://127.0.0.1:8080 \
  --token-file .secrets/api-token
```

也可通过已有 `FINANCECLAW_API_TOKEN` 环境变量提供 token；不提供命令行明文 token 选项。远程地址要求 HTTPS，HTTP 仅允许 localhost/127.0.0.1/::1；拒绝 URL 内的账号、密码、查询参数和路径。请求不跟随重定向，也不继承代理配置，避免将凭据转发到其他地址。目标不是已授权测试部署时，不运行该命令。

默认每页 50 条、每种明细最多 20 页，可通过 `--page-size`（1–100）、`--max-pages`（1–100）调整。`--timeout`（1–30 秒，默认 10）控制 HTTP socket 超时，不是整个扫描的总时限。每次 HTTP 响应最多读取 256 KiB。

默认生成 `target/m5d/preflight-<随机ID>.json`；可用 `--output` 指定一个尚不存在的文件。报告完整写好后才发布，文件权限为 0600，已有文件不会被覆盖。终端仅显示状态和路径，不打印 token 或响应正文。保存失败不会留下看似成功的半份报告。

## 结果如何判断

| assessment | 退出码 | 含义 |
| --- | --- | --- |
| `ready_for_review` | 0 | 本次读取中受理已暂停、所有计数为零、明细完整、前后受理状态和计数相同，供人工复核 |
| `blocked` | 2 | 读取完整且前后观测相同，但仍开放受理或有阻塞项 |
| `incomplete` | 3 | 分页截断、计数与明细不匹配、观测变化、无效响应、HTTP/连接错误等，不能据此判断排空 |
| 无报告 | 1 | 本地配置、凭据读取或报告保存失败；参数解析错误沿用 argparse 的退出码 2 |

有积压时退出码 2 是正常业务结果，不表示执行了任何修复。脚本不会通过删除记录、放大页数直到不受限，或自动重试请求来把结果变成通过。

报告包含开始/结束时间、固定 `scope=current_owner`、`consistency=separate_live_reads`、扫描前后的受理/计数、四类明细、错误码、脚本 SHA-256，以及两个判断：

- `observations_match`：扫描前后的 admission 与 counts 相同；不代表扫描期间没有发生变化。
- `inventory_matches_counts`：任务明细的状态/命令/残留计数、通知/绑定/记忆作业数量与最后一次排空计数吻合。`pending_forget` 是直接读取的 owner 级计数，没有另一份分页记录。

明细只保留允许的 ID、状态、revision、错误码和数量。忽略 API 中未使用的字段，不导出 tenant/subject、用户正文、模型输出、工具参数/结果、通知正文、飞书用户标识、记忆内容、凭据或 lease token。无效响应只记录固定错误码，不保存原始错误正文。

分页必须单调前进；重复/伪造游标、错误 scope、超限响应、重复 JSON 键或错误类型都会拒绝通过。记忆 ID 按整数比较。超过页数上限时保留已读部分，并明确标为 incomplete。

## 验证范围

这是一组独立实时查询，不是事务快照、全局写入围栏、备份清单或线上切换许可。状态在两次查询之间变化后再变回也可能无法观测到。其他主体仍可运行；隐私 epoch、历史遗忘审计、唯一渠道消费者、历史导入、真实供应商质量和线上回退仍由原专项流程核对。`ready_for_review` 不替代这些验收，也不会解除紫微普通启用门禁。

M5d 不修改 Java 运行代码或依赖；继续使用 M5c 已验证的 JAR。验证分两层：

```bash
# 合成响应、本机 HTTP 和 CLI；不连接真实服务
python3 scripts/test-inspect-drain.py
python3 scripts/test-live-acceptance.py
python3 scripts/collect-evidence.py

# 随机隔离的当前默认部署；使用离线模型和合成数据
python3 scripts/init-local.py
python3 scripts/validate-compose.py --ziwei --admission --preflight
python3 scripts/check-delivery.py
```

[契约测试](../../scripts/test-inspect-drain.py) 覆盖空闲/开放/有积压、受理和计数变化、明细缺失、截断、游标、脱敏、仅 GET、重定向拒绝、异常响应、文件权限和 CLI 退出码。结果写入 `target/m5d/preflight-tests.json`。

Compose 验证在排队/记忆积压、刻意截断、暂停且排空、还原后保持暂停、恢复受理五个时点运行实际 CLI，并核对报告、退出码、脚本指纹与权限。部署结果位于 `target/m5d/deployment/compose-results.json`，单次报告在其 `preflight/<随机项目>/` 下。CI 已加入上述测试和报告归档；未执行远程 CI。

本轮 **12 项预检契约测试通过**，零失败、零错误、零跳过。沿用 M5c 已通过的 **659 项 Java 回归**与同一份 JAR，另有既有的 **8 项真实验收器离线契约测试**；M5d 没有修改 Java 构建输入，因此未重复运行 Java 全套。汇总和 181 个源码/配置指纹见 [本地证据](evidence/m5d-local-results.json)。

实际四角色 Compose **19 项检查通过**，包含既有部署/恢复实验及五个 CLI 观察点。35 张表备份还原一致，12 次离线请求全部成功，P95 **1.44 秒**；不推导真实模型性能或生产容量。见 [Compose 记录](evidence/m5d-compose-results.json) 和 [五份合成预检报告](evidence/m5d-preflight-reports.json)。三者的脚本/JAR 摘要已核对，随机项目与卷已清理。

第一次 Compose 演练在原有断库后的即时 readiness 断言处失败，日志显示连接池获取连接超时；脚本改为在原 45 秒窗口内确认实际 HTTP 503，超时/断连仍不算通过，并保存实际状态码。重跑同时观测到 readiness 和业务 API 返回 503，恢复检查通过。没有修改服务健康检查逻辑或放宽成功状态。

M5d 已本地提交 `993e7ac`，未推送。没有连接真实模型/MCP/飞书租户，没有切换已有部署。后续 [M5e](m5e-restore-privacy.md) 补充旧备份恢复前的全主体隐私核对和隔离演练。
