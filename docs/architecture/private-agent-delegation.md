# 私有叶子 Agent 委派：P3a.3

**状态：2026-10-24 已推送 `d433423`，远程 CI 成功。** 本模块让父 Agent 同步调用同 owner 的已发布叶子 Agent，最大深度为一。没有新增子队列、租约、心跳或调度生命周期；提交前的[本地验收证据](evidence/p3a3-implementation.json)与后续远程结果分别记录。

## 配置目标与父 Agent

目标必须有明确的 `agent-release/3` 发布，且请求能力、实际能力和 scopes 全部为空。省略 `composition` 的继承式发布没有显式空计划，会被拒绝。目标与父请求必须属于同一 `(tenant, subject)`，父不能选择自身。

先按 [Agent 管理说明](agent-definitions.md)创建并发布叶子，示例定义文件：

```json
{
  "displayName": "文字整理助手",
  "systemPrompt": "只整理这次收到的消息，保留事实与不确定性，不调用工具。",
  "composition": {"modelProfile": "primary", "capabilities": []}
}
```

随后创建或更新父 Agent，在能力选择中引用目标 UUID：

```json
{
  "displayName": "任务助手",
  "systemPrompt": "需要整理文字时，将明确的待整理内容发送给已授权的叶子助手。",
  "composition": {
    "modelProfile": "primary",
    "capabilities": ["agent:11e8e638-0689-40df-99ed-b22de07c1172"]
  }
}
```

UUID 替换为实际已发布目标。继续使用现有 `agent-admin create/update/publish` 和预期 revision，不新增管理 HTTP 接口。目录用一条 owner 范围集合查询返回候选 ID/hash，不逐个读取全部目标清单；发布只解析真正选中的目标，并校验摘要、归属、空计划和运行工件兼容性。

父发布将目标 Agent ID 和当时的不可变 release hash 写入 `agent-leaf/1` 能力契约。目标以后发布 B2 不会改变父发布已冻结的 B1；父重新发布并创建新会话后才采用新目标版本。执行和恢复只读取冻结 hash，缺失、损坏或不兼容时阻断，不回退到目标 current。

## 模型实际可以传什么

注册的工具名为 `delegate_` 加去掉连字符的目标 UUID。例如上述目标对应 `delegate_11e8e638068940df99edb22de07c1172`，输入只有：

```json
{"message":"请把以下已经核实的三条信息整理成一段摘要：……"}
```

不接受额外字段、空白消息、目标 ID、发布 hash、owner、父记忆引用或权限参数。Schema 的字符串上限为 16,384 字符，实际派发还受 UTF-8 字节预算限制，不能把字符上限当作模型可用窗口。

子 Agent 使用目标发布自己的 system prompt、主模型与可选备用模型，不复用父模型实例；离线模式只回显子消息。它只收到明确的 `message`，不会自动注入父画像、长期记忆快照、会话历史、WorkingContext 或工具结果。父模型可以在消息中明确提供完成子任务所需的信息；这里不对这些显式内容作语义推断。

子 Toolkit 固定为空，权限模式为非 BYPASS 的 `DEFAULT`。模型若仍返回工具调用，会触发不可继续的错误，不能把它包装成普通观察后继续生成合法答复。零工具也禁止再次委派。默认离线父模型不会根据自然语言自行选工具，配置成功不等于每条输入都会触发委派。

## 共享预算、回放与大结果

| 动作 | 当前契约 |
| --- | --- |
| 子实际模型请求 | 子调用最多 4 次，同时受目标发布上限和根 Turn 剩余额度约束；主备模型每次物理请求分别预留 |
| 子工具 | 0 次，不装配 Function、MCP、Skills、Artifact reader 或人工审批 |
| 父调用委派工具 | 正常占用父工具预算；命中子最终结果缓存也不免除这次父工具派发计数 |
| 已提交的子业务结果回放 | 父门禁和绑定检查后返回原 `{"answer":"…"}` 回执，不再调用模型 |
| 当前有效父 attempt 内，SDK final 已保存而子业务回执未提交 | 从同一 scope 的完成检查点收尾；即使 4 次模型额度已耗尽，也不再调用模型 |

调用身份仍为 `(root_turn_id, call_id)`；更换父 attempt 不重置子计数。结果继续保存于 `capability_steps`，最终回执与完成标记原子提交。开始、模型预留、缓存、检查点和结果访问都使用父 Claim，取消、失租、撤权或遗忘后的迟到回调不能读写旧状态。

父能力绑定依赖 `function:read_artifact_range` 和 `artifact:read`，用于延续既有大结果归档：过大工具回执由父上下文保存为隐私绑定文件，再向模型提供引用。这项父侧依赖不进入叶子的零工具计划，产物读取仍核验原权限与隐私版本。

叶子与主运行时使用同一套上下文配置：默认用户消息最多 8 KiB UTF-8、固定系统内容最多 8 KiB，可通过 `FINANCECLAW_CONTEXT_*` 调整，并继续受实际模型窗口限制。真实模型还经过完整 HTTP 请求预算；执行超时沿用 `financeclaw.worker.model-timeout-ms`。Schema 的 16,384 字符是外层输入上限，不保证在默认字节预算下可派发。

## V8 与遗忘边界

[V8](../../src/main/resources/db/migration/V8__private_agent_delegation.sql) 扩充既有 `child_invocations` 的目标类型，增加 `target_agent_id`、`target_release_hash` 及绑定约束；私有目标固定 scopes `[]`、工具计数 0，身份字段不能原地 UPDATE。固定市场研究保留原目标、权限、历史 scope 和 4/10 上限。主库仍为 **41 张应用表**。

私有 SDK scope 的会话部分为 `H(root.key(), "private_agent", callId, agentId, releaseHash)`，Agent 与 release 使用目标自身身份。V8 的集合映射使用相同公式覆盖全部历史 attempt，并检查同 owner 与显式空计划的元数据；不读取当前部署指针。该 SQL helper 仅用于隐私定位和残留检测，不认证发布摘要、运行工件或父调用授权，执行仍需 Java 边界完整校验。先墓碑后删除的顺序继续沿用 [P3a.2 清理契约](execution-privacy.md)，无法定位或失效绑定的 live 状态继续阻断恢复。

SDK scope 仍包含父 attempt。**跨 attempt 只回放已提交的子业务结果，不自动复制或续跑旧 SDK 检查点。** 旧 attempt 的 SDK final 已保存但子业务回执未提交时，当前实现不会迁移该 final 到新 attempt 补写回执；这属于后续可独立验收的恢复扩展。

根 [RecoverTaskService](../../src/main/java/com/financeclaw/app/execution/RecoverTaskService.java) 仍要求封存的 attempt 回执；没有该回执的未知执行进入 `blocked/unconfirmed_execution`。子调用即使标为完成，也不能自动解除这个根阻断或授权重新执行。持久子调用身份、子结果与 SDK 状态均不替代父 Turn、Command、fence、隐私及授权权威。

## 当前验收状态

提交 `d433423` 的 [run 40065079071](https://github.com/westbrook0904/FinanceClaw/actions/runs/40065079071) 已整体 `completed/success`：verify 的 **1,242 单元 + 460 集成 = 1,702 项 Java 测试**零失败、零错误、零跳过，另有 **46 项脚本测试**，delivery 的 **44 项检查**通过。该远程基线不包含后续 P3a.4。

[提交前本地证据](evidence/p3a3-implementation.json)保留完整初跑、精确复跑与部署检查的历史过程：

提交前本地 Java 的分次结果为 **1,242 项单元／契约／架构 + 460 项独立集成用例 = 1,702 项通过**，包含全部 23 项私有委派用例。完整初跑集成 458 项通过、2 处失败（迁移次数旧断言及 CLI 30 秒等待），仅修正迁移常数 6→7 后，两个原失败用例的精确定向复跑均通过；生产代码、SQL、JAR 和 CLI 超时／业务断言未改。这是按用例身份合并的分次结果，`single_full_verify_success=false`，不声称单次完整 verify 全绿。

当时 **46 项脚本测试、44 项 Compose 检查全部通过**，隔离部署进程正常退出并完成清理。Compose 与 Java 验证使用同一生产 JAR，11 项部署输入摘要均与现场文件一致。

Compose 新增的三项检查覆盖候选目录与父发布冻结目标、叶子重新发布后父目标不变，以及恢复后双方绑定保留。它们不证明离线父模型会根据自然语言触发委派；23 项 PrivateAgentDelegationIT 等测试覆盖直接进入子适配器的 SDK 链路。父 HTTP／Worker 完整入口及取消传播修复已由 [P3a.4 专项](private-agent-runtime-validation.md)完成，`467ba8f` 的[远程运行](https://github.com/westbrook0904/FinanceClaw/actions/runs/40072812866)为 1,711 Java／46 脚本／44 部署全部通过；提交前 1,404 项本地范围另行保留。P3a.5 的[真实暂停恢复](private-agent-resume-validation.md)已本地验收，未推送，52 唯一用例的分次结果通过；保留根检查点中的完成结果，不重入子业务缓存或续跑旧子 SDK。

P3a.2 提交 `c96b4e7` 的 [run 40055663280](https://github.com/westbrook0904/FinanceClaw/actions/runs/40055663280) 已整体成功，verify 为 1,226 单元 + 437 集成 = 1,663 项、delivery 为 41 项检查；该基线不包含本轮 P3a.3。

完整权限子 Agent、跨 owner 分享、递归、独立调度、子会话记忆、RAG 和通用 Workflow DSL 不属于本模块。

源码入口：[PrivateAgentTarget](../../src/main/java/com/financeclaw/domain/agent/PrivateAgentTarget.java) 保存纯领域身份与 scope，[PrivateAgentContracts](../../src/main/java/com/financeclaw/support/PrivateAgentContracts.java) 编解码规范契约；[调用服务](../../src/main/java/com/financeclaw/app/execution/ChildInvocationService.java)管理父门禁与回执，[SDK 适配器](../../src/main/java/com/financeclaw/infrastructure/agentscope/capability/PrivateAgentDelegation.java)使用中立模型层的 [ModelFactory](../../src/main/java/com/financeclaw/infrastructure/agentscope/model/ModelFactory.java)和 [BudgetedTransport](../../src/main/java/com/financeclaw/infrastructure/agentscope/model/BudgetedTransport.java)。
