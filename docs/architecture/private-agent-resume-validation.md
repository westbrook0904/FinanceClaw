# 私有委派真实暂停恢复验收：P3a.5

**状态：2026-10-24 已本地验收，未推送。** 三套选定集成的 **52 个唯一用例最新结果全部通过**，新 5 项包含其中。首轮 52 项有 1 项测试假设错误；修正后完整复跑 runtime suite 的 14 项全部通过，与原先通过且未变更的核心 38 项合并验收。`single_full_selected_run_success=false`，不称单次 52 项全绿。结果、报告来源和输入摘要见[本轮证据](evidence/p3a5-implementation.json)。

## 正常恢复链路

父先通过原生 delegate 提交子业务结果，再调用 request_city，正常封存根 waiting 回执。测试在同一 JVM 内关闭并重启 Spring 应用，保留 PostgreSQL、本机 SSE 服务及请求捕获，再经 HTTP 回答，由正常 Worker 创建新 attempt。

```text
父 delegate → 子业务结果已提交 → request_city → 封存 waiting
  → 同 JVM 内重启 Spring → HTTP 回答 → 新 attempt
  → 恢复旧根检查点 → SDK 跳过已完成的重复同 CALL → 父消费旧结果
```

**恢复的是根检查点中的已完成工具结果，不是重入子业务缓存，也不是跨 attempt 续跑子 SDK。** 未提交子回执的旧 SDK final 不会被迁移成新 attempt 的完成依据；根无 sealed receipt 仍保持未知执行阻断。

## 已验证的五项

| 用例 | 结果 |
| --- | --- |
| 正常恢复与结果保留 | 正常 waiting、Spring 重启、HTTP 回答；父显式请求相同 CALL／输入，SDK 跳过完成调用，唯一线上结果与暂停时已提交的 canonical 子回执逐字一致；重复 HTTP 回答不创建第三个 attempt |
| 预算连续性 | 根模型额度为 3 时，回答后的新 attempt 不重置额度或额外派发模型 HTTP |
| 暂停取消 | waiting 期间取消，重启后旧回答不能恢复执行 |
| 暂停撤权 | waiting 期间撤权，重启后旧回答不能重新取得执行权限 |
| 暂停遗忘 | 清理旧私有执行资料；重启后旧回答不能复活检查点、子回执或最终答案 |

正例原 Turn 保持画像 A，下一普通 Turn 才读取 B。叶子 current 重新发布为非空工具版本后，父冻结绑定和既有完成结果保持，不升级或重跑目标；本例不再次进入 beginPrivate 解析目标。

首 Turn 的实际计数为 **根模型 5、根工具 2、子模型 1、子工具 0**：线上父请求 4 次、子请求 1 次；随后普通 Turn 的一次父请求单独计算。同 CALL 已有完成结果，SDK 在派发前跳过，因此没有新增工具额度。真正再次进入业务缓存的直接回放仍由 PrivateAgentDelegationIT 单独验证，不将本例充作该分支证据。

## SDK 语义与测试修正

固定 SDK 2.0.3 的 CallExecution 从上下文全部 ToolResult 收集完成 ID，过滤相同 ID 的新工具请求；没有 pending 调用时直接进入下一次模型迭代。OpenAIChatFormatter 逐条输出，唯一结果并非 formatter 去重。

首轮正例错误地预期再次派发业务工具、根工具计数 3 及两份结果，SSE fixture 的断言因而返回 500。根据固定 SDK 主证据，只修正这项假设，并增加暂停时 canonical 子回执作为独立比较基准。显式同 CALL／输入、A/B、预算、子次数、attempt、权限和遗忘断言保持，未放宽超时，也未修改生产代码。

## 运行与证据范围

首次环境尝试在编译 147 个测试源码后停于 Ryuk，未进入 PostgreSQL 或业务用例，按环境中断记录；经此前已授权的 Docker 重启恢复后重跑。首业务运行 **52 项中 51 过、1 失败**；TaskLifecycleIT 25 与 RecoveryIT 13 全过。修正测试后仅完整复跑 PrivateAgentRuntimeIT 的旧 9 + 新 5 = **14 项，全部通过**。

精确复跑命令：

```bash
scripts/mvn.sh -B -ntp -o spotless:apply test-compile -Dit.test=PrivateAgentRuntimeIT failsafe:integration-test failsafe:verify
```

最终为 14 + 25 + 13 = **52 个唯一用例的分次结果**，不是 52 + 14，也不是单次全绿。本轮没有重跑单元、其余集成、Compose 或 Python 脚本。源代码改动限于 PrivateAgentRuntimeIT 和 PrivateAgentLoopback 两个测试文件，文档与证据同步更新，新增 SQL 断言只读；正常 HTTP／Worker 创建 attempt，没有手工重排 Command。

305 项生产输入保持不变，392 个生产 class 与本地验证 JAR 对应；证据冻结本地 JAR 摘要，不声明它与 CI 下载产物字节相同。本专项父子恢复链路只重启同 JVM 内的 Spring context，不据此宣称独立 JVM／JAR 进程恢复。

P3a.4 提交 `467ba8f` 的 [run 40072812866](https://github.com/westbrook0904/FinanceClaw/actions/runs/40072812866) 为独立远程基线：**1,242 单元 + 469 集成 = 1,711 Java、46 脚本、44 部署检查**全部通过，不包含本轮新 5 项。[P3a.4 原本地证据](evidence/p3a4-implementation.json)保持原样。

同 owner、显式输入、深度一、4 次子模型／0 次子工具、当前 grant／隐私 epoch、持久 fence 和根 sealed receipt 保持。工具叶子、共享授权、递归、未封存 SDK 补回执、RAG 及新调度生命周期不在本模块范围，P3a 整体仍未完成。
