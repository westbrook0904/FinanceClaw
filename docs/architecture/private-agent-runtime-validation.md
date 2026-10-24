# 私有委派完整链路验收与取消传播修复：P3a.4

**状态：2026-10-24 已推送 `467ba8f`，远程 CI 成功。** [run 40072812866](https://github.com/westbrook0904/FinanceClaw/actions/runs/40072812866) 的 **1,242 单元 + 469 集成 = 1,711 项 Java、46 项脚本和 44 项部署检查**全部通过。

提交前本地的一次指定范围 verify 曾通过 **1,242 单元 + 10 套选定集成 162 项 = 1,404 项**，新增 9 项包含在 162 中。[原本地证据](evidence/p3a4-implementation.json)保留该范围、两次失败和候选过程，不改写为后续远程成功。

## 完整入口验证了什么

P3a.3 实现了同 owner、发布冻结、显式输入、4 次子模型／0 次子工具的叶子委派。原 PrivateAgentDelegationIT 直接进入子适配器，Compose 验证目录、发布和恢复绑定；本轮补齐正常父入口链路。

测试使用独立拥有的真实 `SpringApplicationBuilder`、随机 HTTP 端口、PostgreSQL 和自动运行的 TaskWorker。父子模型均经过生产 `ModelFactory`、`BudgetedTransport` 和原生 ReAct；供应商 HTTP 端点由本机 OpenAI 兼容 SSE 服务替代，不替换生产 Model 或直接调用子适配器。AgentService／ReleaseService 负责正常发布，执行、取消、画像、撤权和下载使用真实 HTTP。

```text
HTTP 提交 → 正常受理/Command → TaskWorker → 父 ReAct
  → SSE delegate tool_calls → 受治理工具边界
  → 冻结叶子发布 → 子 ModelFactory/SSE → 子业务回执
  → 父消费绑定 call ID 的工具结果 → 父最终回答 → 根封存与提交
```

这是实际 Spring 应用的运行时集成验证，不是独立应用 JAR fork，也不认证真实供应商质量或生产容量。

## 验证覆盖

新增 suite 为 **8 个测试方法、9 个用例**，最终全部通过：

| 类别 | 已验证行为 |
| --- | --- |
| 正常委派 | 父只携带发布允许的工具，实际选择 delegate；子答复由父消费，调用表、结果、计数和根最终提交相符 |
| 上下文隔离 | 子仅收到显式消息与叶子提示词，不隐式收到父画像、会话历史或工具轨迹 |
| 发布冻结 | 叶子重新发布后，既有父继续使用原提示词和冻结 hash |
| 根模型预算（2 项） | 根额度为 1、2 时，下一次模型 HTTP 请求在预留边界被拒绝，父子共享计数不能绕过 |
| 子违规工具调用 | 伪造 tool_calls 不能成为父成功答案 |
| 大结果 | 长答复先归档为文件引用，再由父通过受治理的 range reader 读取片段 |
| 执行中取消 | 子 HTTP 在途时取消根，根完成持久取消确认后，迟到响应不能写入检查点、工作上下文、子回执或父最终结果 |
| 执行中撤权 | 子 HTTP 在途时撤权，迟到响应继续受当前 grant 与根栅栏检查 |

本机服务记录实际消息、Schema、call ID 和请求次数，并以同步信号控制在途场景；数据库断言核对本 fixture 生成的根 Turn、子调用及状态。

## 取消传播修复

严格取消用例发现原 stop 只接入父 guarded model。父在等待 delegate 时模型流已经结束，停止该流不能取消子工具 Mono 及其独立 HTTP。本轮生产变化仅有 [TaskAgentRuntime](../../src/main/java/com/financeclaw/infrastructure/agentscope/execution/TaskAgentRuntime.java)：

- 完整 `streamEvents` 订阅增加 `takeUntilOther(stop.asMono())`，通过 SDK 原有取消处置释放在途工具批次及子请求。
- 每 attempt 的 Active 保存独立 AtomicBoolean；interrupt 源端先标记中断，在 SDK interrupt 的 finally 中保证发出 stop。
- blockLast 返回后，已中断的 attempt 抛 Fenced，跳过旧检查点解释，避免将取消流的正常结束视为 waiting／completed。

**业务终态仍由持久取消确认推进。** 事件流结束不代表业务成功，迟到回调继续受原 fence、隐私及授权门禁约束。SQL、队列、权限和超时未改，也未放宽 sealed root receipt 或未知执行阻断。

## 验证记录与范围

首次新增 9 项为 8 过、取消版本断言 1 失败。加强测试时序后，第二轮仍为 8 过、取消 1 失败，确认完整事件流传播缺口。最小传播候选曾通过 4 项定向检查；这份中间证据不认证随后补上中断标记的最终版本。最终冻结源码重新完成上述 **1,404 项指定范围验证**，两次失败、候选记录及最终日志分别保留。

最终命令：

```bash
scripts/mvn.sh -B -ntp -o spotless:apply '-Dit.test=PrivateAgentRuntimeIT,TaskLifecycleIT,ExecutionHeartbeatIT,BusinessIT,WorkingContextIT,RecoveryIT,StateAndIsolationIT,PrivateAgentDelegationIT,ChildInvocationIT,ExecutionPrivacyIT' verify
```

集成范围覆盖新增委派、生命周期、心跳、业务工具、工作上下文、恢复、状态隔离、既有私有委派、研究子调用及执行隐私。**提交前该次本地验证没有重跑其余集成 suite、Compose 或 Python 脚本，不是全项目集成重跑。** 305 项生产输入对比只有 TaskAgentRuntime 变化，新增测试为 PrivateAgentRuntimeIT 与 PrivateAgentLoopback 两个文件；冻结清单记录源码、逐 suite 报告和 JAR 摘要。

P3a.3 提交 `d433423` 的 [run 40065079071](https://github.com/westbrook0904/FinanceClaw/actions/runs/40065079071) 为独立远程基线：**1,702 Java、46 脚本、44 部署检查**全部通过，未将这些数量加入本轮。[P3a.3 提交前证据](evidence/p3a3-implementation.json)保持原样。

同 owner、冻结 release、深度一、空工具叶子及根共享预算仍按 [P3a.3 契约](private-agent-delegation.md)执行。跨 attempt SDK final 补回执、有工具叶子、共享授权、递归、独立子调度、RAG 和 Scheduler 不在本模块范围，P3a 整体仍未完成。


后续 [P3a.5 真实暂停恢复专项](private-agent-resume-validation.md)已推送 `a4a6ef4`，远程 1,716 Java／46 脚本／44 部署通过；原本地 52 唯一用例分次通过；恢复根检查点中的已完成结果，不重入子业务缓存，也不把旧子 SDK final 复制成新 attempt 的完成依据。
