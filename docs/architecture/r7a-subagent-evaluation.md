# R7a：SubAgentTool 的固定版本契约实验

实施日期：2026-10-21。基于本地检查点 `431c07d`，继续[整改方案的 R7 等价性评估](agentscope-cola-remediation.md#92-原生替换实验的通过标准)。本批仅增加测试原型与证据，不切换市场研究的生产路径、不升级 AgentScope，也不新增生产依赖。

## 决定

**AgentScope Java 2.0.3 的 SubAgentTool 不能直接替换当前市场研究入口。** 它的默认会话选择、非版本存储、错误处理和结果格式与现有业务协议不同。继续使用已有 [MarketResearchAgent](../../src/main/java/com/financeclaw/infrastructure/agentscope/capability/MarketResearchAgent.java) 驱动原生 ReActAgent。

现有适配器承担业务输入、根预算、工具范围、固定状态地址、fence/CAS、完成状态核对、取消和资源关闭。若再包一层 SubAgentTool，这些职责仍需保留，并且需要处理新增的 session 输入、输出转换及存储错误语义。因此本批不增加新的生产包装层。

## 六个可执行探针

测试执行真实 `SubAgentTool`，只将子 Agent 和状态存储替换为 Mockito 对象，以观察该 SDK 入口的契约。所有数据均为合成输入，不调用真实模型、供应商或数据库。

| 探针 | 固定版本实际行为 | 对当前入口的影响 |
| --- | --- | --- |
| 构造与权限接入 | 构造调用一次 provider，执行再调用一次；它实现 AgentTool，不能通过本项目要求 ToolBase 的权限工厂 | 不满足现有入口门禁，不能直接注册；这不表示 SDK 完全不支持权限，也未排除薄适配的可能 |
| 会话与存储地址 | 工具 Schema 暴露 session_id；测试传入的合成 input.session_id 决定工具外层存储的 session；user 为 null，调用 get/save；服务端 RuntimeContext 不覆盖该地址 | 外层调用契约不等价于当前固定 user/namespace 与版本 CAS；未实际驱动模型挑选会话 |
| 存储拒绝 | Mockito store 的 get/save 抛出异常后，执行仍可返回普通工具结果 | 普通返回不能证明状态已可靠保存；这些调用也不表示真实存储已发生越权读写 |
| 执行失败 | 子调用异常变成 ERROR ToolResult，异常消息进入工具结果；错误结果仍触发外层 save | 不能将 Mono 完成视为业务成功，仍需错误脱敏与回执核对；未验证该文本穿透 HTTP 或飞书到达用户 |
| 关闭事件转发 | `forwardEvents(false)` 可走 call、不向 emitter 转发；最终文本仍包含 session_id | SDK 确有关闭转发的能力，但输出仍需转换成业务结果 |
| 取消与关闭 | 取消订阅会取消受控 Mono 并调用子 Agent 的 interrupt；不产生结果保存，所测路径不调用 close | 确认取消传播；不表示远端调用或已产生的效果被撤销，也未据此证实资源泄漏 |

这些断言描述 **2.0.3 的观测行为**。未来升级 SDK 若使探针失败，应重新评估收益与协议，不应为了继续使用旧断言而保留已修复的 SDK 行为。

## 验证边界

本批不是完整的受治理 SubAgentTool 替换实现，也不宣称 R7 全部完成。Mockito ReActAgent 不执行推理循环，Mockito store 不提供持久性；六个入口探针不能证明：

- 两个真实研究任务并行使用相同 call id 时的状态、历史与 evidence 隔离。
- 子模型/工具预算与根计数一致，越权工具和 symbol 被实际阻断。
- PostgreSQL fence/CAS、晚写、隐私变更、取消竞争和持久完成回执等价。
- 真实模型输出质量、供应商行为或替换后的性能收益。

外层存储调用的观测不替代真实存储验收：现有绑定 scope 的存储会拒绝不匹配的身份和非版本写入，本批没有证明该保护被绕过。构造时额外调用 provider、所测路径未调用 close，只暴露需要核对的生命周期职责；没有测量真实资源的分配、回收或泄漏。事件转发关闭和取消传播是已确认的可复用行为，不能把六项探针全部归为失败反例。

上述治理仍由默认路径及既有 BusinessIT 等集成测试承担，完整重构验收另见[运行验证记录](post-refactor-verification.md)。当前已有明确不等价项，足以否决直接替换；没有必要先实现额外包装，再以测试通过作为采用理由。

## 复现

[SubAgentToolCompatibilityTest](../../src/test/java/com/financeclaw/architecture/SubAgentToolCompatibilityTest.java) 已并入正常 Maven 测试目录。实际执行以下命令，6 项探针与 7 项治理集成测试通过，失败、错误和跳过均为 0，Spotless 通过：

```bash
scripts/mvn.sh -B -ntp spotless:apply test -Dtest=SubAgentToolCompatibilityTest \
  failsafe:integration-test failsafe:verify -Dit.test=GovernanceBoundariesIT spotless:check
```

该命令只定向执行上述两个测试类。其余职责拆分与 HTTP 校验回归来自同批完整运行及失败修复，准确范围见[运行验证记录](post-refactor-verification.md)，测试报告与固定 SDK 摘要见[机器证据](evidence/post-refactor-verification.json)。本批测试原型与分析记录随 `codex/java-agentscope` 分支交付；生产源码、依赖和数据库迁移与 `431c07d` 保持一致，定向重跑前后的产品 Jar SHA-256 相同。
