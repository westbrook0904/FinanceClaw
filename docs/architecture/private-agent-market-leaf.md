# 固定行情私有叶子：P3a.6

**状态：2026-10-24 已推送 `13f73a7`，远程流水线通过。** [run 40105280949](https://github.com/westbrook0904/FinanceClaw/actions/runs/40105280949) 整体 `completed/success`：1,253 单元 + 490 集成 = 1,743 Java、46 脚本及 44 部署检查全部通过，包含本模块。下文的首次失败和分次结果为提交前本地记录，原证据不改写为远程结果。

## 配置与实际能力

在支持 `market:read`、已配置固定行情数据的基础发布上创建目标 Agent：

```json
{
  "displayName": "行情证据助手",
  "systemPrompt": "只处理这次明确收到的消息。需要行情时调用一次 market_snapshot，保留 symbol、price、provider、as_of 和发布来源，说明限制。",
  "composition": {"modelProfile": "primary", "capabilities": ["function:market_snapshot"]}
}
```

使用现有 [Agent 管理入口](agent-definitions.md)发布，然后在父 Agent 的 composition 中选择 `agent:<目标 UUID>`，再发布父 Agent 并建立新会话。父 systemPrompt 中应明确说明对应 `delegate_<UUID 去掉连字符>` 的行情用途和一次读取限制，模型不会从 UUID 自动理解业务角色。父基础发布必须允许 `artifact:read` 与 `market:read`；正常的一次委派加一次快照需要至少两次根工具额度。两次父模型与两次子模型组成的常规链路需要四次根模型额度。

目标必须同 owner、具有明确 v3 计划，请求能力精确为 snapshot，实际编译闭包精确为 snapshot 与 reader，scopes 精确为 `market:read`。原发布目录为行情工具添加 reader 依赖；新委派契约进一步缩窄执行，只向子模型提供 snapshot Schema，不装配 reader、MCP、Skills、写工具、人工交互或嵌套委派。目录保持一次 owner 集合查询，只有真正选中的目标完整解析清单。

父模型仍只能传 `{"message":"明确的子任务"}`。子 Agent 使用自己的提示词与模型，不自动继承父画像、记忆、历史或工具结果。默认离线模型仅回显明确消息；发布行情能力不意味着离线模型会自行选择工具，也不意味着接入实时行情。

## 冻结数据、预算与结果

`agent-leaf/2` 固定 `market-snapshot-only` 档位、工具白名单、结果权限、深度一、4 次模型与 1 次工具上限。目标自己的模型上限可进一步收紧；根预算继续共同约束父子。旧 `agent-leaf/1` 的规范字节、空工具及零工具计数保持不变。

真实父 Claim 负责运行状态、lease/fence、当前 grant 和隐私 epoch。行情读取使用 begin 时完整核验的目标冻结 definition，不读取父发布或全局 current，也不构造假的子 Claim。快照保留 provider、as_of、输入摘要与目标发布摘要。普通 Agent 草稿更新保留创建时 base，不新增 rebase 能力。

每次实际 snapshot 成功读取在同一短事务中提交子计数、根计数和 `capability_steps` 回执。同工具 ID／同输入回放不再读取或扣额；改输入冲突，另一个工具 ID 超出一次额度被拒绝。更换 attempt 不重置计数。模型网络请求始终在数据库事务外，没有逐 token 数据库访问。

工作预算计算完整 SDK 消息，含工具参数、结果、ID 与 metadata。固定快照字段有界，完整结果必须低于最小 4 KiB 归档阈值，子侧无需通用文件读取。子最终答案过大时由父既有机制归档，**required_scope 为 `market:read`**；保留 artifact 权限而撤掉 market 后，下载及新 Turn 的 reader 仍必须拒绝。行情回执不能降格为仅需文件权限的资料。

SDK 2.0.3 的无名称参数续片使用 `__fragment__`。适配器只在当前模型流已有合法 snapshot header 后接受这种续片，并在 SDK 合并后的实际 acting 边界再次检查工具白名单；伪造 reader、其他 delegate 或写工具均不可执行。

## 数据库、取消与恢复

[V9](../../src/main/resources/db/migration/V9__private_agent_market_leaf.sql)只扩展既有调用约束和集合绑定核验：空工具调用继续 `scopes=[]/tool_calls=0`，新行情调用为 `scopes=[market:read]/tool_calls<=1`。身份不可原地改绑，主库保持 41 张应用表。集合 helper 保留无效目标定位信息，不读取 current，不充当运行授权权威。

原 execution scope 哈希、全部历史 attempt 的遗忘墓碑顺序、unsafe 残留检测和清理函数继续复用。取消、撤权和遗忘后的迟到结果不能提交。根无 sealed receipt 时仍为未知执行阻断；子 SDK final 不会复制到新 attempt 补造完成回执。正常 waiting 重启只恢复根检查点中的已完成结果。

## 验证记录

Java 最新结果为 **1,253 单元 + 490 集成 = 1,743 个唯一用例通过**，来自完整首跑和三个完整套件的复测，不是一次完整 `verify` 成功。完整首跑的单元全部通过；集成有四项未通过：两个 CLI 首次 bootstrap 超过原 30 秒等待、一个只读隐私快照导出退出码为 1，以及 Role 初始化仍断言 schema 为 8。后者修正两处版本常量为 9 后，PrivacyRecoveryCliIT 10、ProfilePrivacyRepositoryIT 17、RoleAssemblyIT 8 共 35 项完整复测全部通过；其余套件沿用首跑通过结果，未重复计数初始化失败占位。

快照失败伴随约 30 分钟日志停顿及 Hikari 间隔警告，现有断言未保留 psql stderr，原因未确认；两个 CLI 超时发生在该停顿之前，不能归为同一原因。未增加超时或放宽业务断言。此前新增运行时单元 mock 错配 profile，以及沙箱禁止绑定端口的失败，也保留在验收历史中。

新增用例覆盖冻结数据来源、仅一次工具、父子预算原子性、伪造工具、真实参数 SSE 分片、取消、遗忘、waiting 重启以及行情归档撤权；旧空工具和固定研究回归保留。两个父子专项当前分别为 20 与 33 项，均通过。46 项 Python 脚本和 44 项 Compose 检查全部通过。

Compose 使用同一本地 JAR，覆盖既有空工具父发布装配、重启、备份和隐私恢复；新增行情父子调用由真实 PostgreSQL、Spring、Worker 与 SDK 的专项覆盖，尚未用独立启动的应用 JAR 验证该新调用链。全部 408 个生产 class 与 JAR 逐字一致，22 个资源与源码、编译目录及 JAR 一致；没有核对远程 CI JAR 与本地 JAR 的字节相等。分次用例来源、首次失败、35 项复测、部署输入与 JAR 摘要见[验收证据](evidence/p3a6-implementation.json)。`single_full_verify_success=false`，原始失败与复测汇总分别保留。

使用本机 OpenAI 协议模拟服务、真实 PostgreSQL 与 Worker；这些结果不代表外部真实模型选择工具的质量、生产容量或跨实例事件一致性。P3a 整体仍未完成。

## 剩余数据库热点

本模块没有新增队列或轮询，目标行情直接使用已解析的冻结正文。但实际读取仍沿用研究调用的双门禁：`ChildInvocationService.snapshotWithinDispatch` 校验父执行并预留子额度后，`MarketDataService.readFrozen` 经 `consumeTool` 再校验一次父执行并扣根额度。应用使用 MyBatis STATEMENT 级本地缓存，重复发布解析和 grant／隐私查询不能按缓存命中视为零成本。在 P3a.6 交付时，此处仅作源码识别，尚未实测 SQL 次数；后续通过独立计数用例验收同事务内复用已验证的不可变发布预算。跨事务的 grant、epoch 与 fence 不能缓存，回放必须保留实时门禁，根／子额度与回执仍须原子提交。

后续本地模块 [P3a.7](execution-budget-queries.md)已实测这条路径：仅消除预算事务中第二次根发布解析，首次行情窗口由 33 降至 29 条 SQL，回放保持 16 条。内外实时门禁均保留，跨事务权限不缓存。P3a.7 的状态与验证范围独立记录，尚未推送。
