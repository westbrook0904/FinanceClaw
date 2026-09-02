# Stage 3：长期记忆实施说明

## 1. 阶段目标

本阶段在 LangGraph Store 之上实现一层薄的、面向业务的长期记忆治理，使 Agent 能跨会话召回稳定的用户偏好、目标、约束和已确认决策，同时确保记忆可追溯、可纠正、可撤销，并与实时金融事实严格分离。

本阶段不建设通用知识库、独立向量平台、复杂 Memory DSL，也不把行情、持仓市值、财务指标等时效性数据写成长期记忆。

## 2. 前置条件

- Stage 0 已验证 LangGraph Store/PostgresStore、LangSmith 和 Agent Server 的兼容性。
- Stage 1 已提供可信 `ExecutionContext`、顶层 Agent、ToolPolicy 与统一工具边界。
- Stage 2 已提供永久会话日志、上下文装配和 `ModelContextManifest`。
- `financeclaw_app` 与 `financeclaw_agent` 的数据库边界已经确定。

## 3. 交付范围

### 3.1 长期记忆模型

实现最小 `MemoryRecord`：

| 字段 | 说明 |
|---|---|
| `memory_id` | 全局唯一标识 |
| `tenant_id` / `subject_id` | 强制作用域，由系统写入 |
| `memory_type` | `preference`、`goal`、`constraint`、`decision_note` |
| `content` | 面向用户、可解释的记忆内容 |
| `status` | `active`、`superseded`、`revoked`、`deleted` |
| `source_message_ids` | 原始证据引用，不复制全部会话 |
| `created_at` / `updated_at` | 生命周期时间 |
| `supersedes_id` | 新记忆替代旧记忆时的关联 |
| `sensitivity` | 由系统策略判定的敏感等级 |
| `schema_version` | 模型迁移版本 |

模型产生的 `MemoryDraft` 只允许包含待确认的类型、内容和证据引用。租户、用户、敏感级别、状态和持久化命名空间必须由系统绑定，不能由模型自由填写。

### 3.2 薄服务层

实现 `LongTermMemoryService`，职责限定为：

- 根据可信上下文绑定 Store namespace；
- 校验记忆类型、来源和敏感策略；
- 写入、检索、替代、撤销和遗忘；
- 将 Store 记录投影为稳定的业务模型；
- 为 Audit 和 `ModelContextManifest` 返回引用信息。

存储、检索和向量能力优先复用 LangGraph Store/PostgresStore。只有在 Spike 证明其不能满足隔离、审计或检索需求时，才允许增加自有存储实现。

### 3.3 记忆写入流程

记忆写入采用“提议—确认—持久化”流程：

1. Agent 或 Middleware 从当前对话生成简单 `MemoryDraft`；
2. 系统校验类型、证据、作用域和敏感等级；
3. 对需要用户确认的内容触发 LangGraph interrupt；
4. 用户确认后由专用服务写入 Store；
5. 写入 Audit，并记录被替代记忆的关联。

明确表达且低风险的偏好是否可以自动写入，由配置化规则决定；财务授权、风险承受能力、账户范围等关键约束默认需要显式确认。

### 3.4 记忆召回流程

通过 LangChain Middleware 在模型调用前执行：

- 按 `tenant_id + subject_id` 限定命名空间；
- 根据本轮意图和语义相关性检索候选；
- 过滤非 `active`、低可信或不适用于当前场景的记录；
- 在独立 token 预算内选择少量记忆；
- 以结构化、可区分于用户原话的区域注入 Prompt；
- 将 `memory_id`、版本和注入原因写入 `ModelContextManifest`。

不得将召回结果当作最新市场事实，也不得因为记忆与工具结果冲突而覆盖实时工具结果。

### 3.5 Agent 可用工具

提供最小工具集：

- `search_memories`：检索当前主体的长期记忆；
- `propose_memory`：提交待确认的记忆草稿；
- `confirm_memory`：由受控执行路径完成确认写入；
- `forget_memory`：撤销或逻辑删除指定记忆。

这些工具均使用 LangChain `BaseTool`，纳入 `ToolCatalog` 和 `ToolPolicy`，不再建设独立 Capability 接口。

## 4. 金融事实边界

下列内容不能作为长期记忆的权威事实：

- 实时或历史行情快照；
- 当前持仓、市值、现金余额；
- 公司最新财报、估值和新闻结论；
- 当前汇率、利率和宏观指标；
- 会过期的交易规则或产品参数。

这类信息必须在请求时经受治理的金融工具或服务获取，并保留 `as_of`、来源和质量信息。长期记忆最多记录“用户关注某只股票”“用户偏好低波动资产”等稳定偏好。

## 5. 迁移与删除

在新链路通过验收后：

- 删除 `harness-memory` 中自研的 Store、Provider、Gateway、Phase 与 SQLite/InMemory 双实现；
- 删除旧 Memory capability、选择器和重试抽象；
- 将仍有价值的领域字段迁移到 `MemoryRecord`；
- 将与 Tool 授权无关的通用 Memory Policy 收敛为服务内校验函数；
- 删除只验证旧接口形状、没有业务语义的测试和文档。

不保留旧 Memory Runtime 的兼容适配层。需要回滚时使用 Git 历史分支和数据库迁移回滚方案。

## 6. 测试要求

### 6.1 确定性测试

- namespace 必须由可信上下文绑定，模型输入不能越权修改；
- 只允许四种记忆类型和合法状态迁移；
- supersede、revoke、delete 后旧记录不再注入；
- 没有证据引用或作用域的草稿不能持久化；
- 金融时效性事实会被拒绝或转换为非权威备注；
- Prompt 中的记忆区域和 `ModelContextManifest` 引用一致。

### 6.2 集成测试

- 跨 thread 召回同一用户已确认偏好；
- 不同租户、不同用户之间完全隔离；
- HITL 确认、拒绝、超时和恢复；
- PostgresStore 重启后记录仍可访问；
- LangSmith trace 可定位召回候选、最终注入引用和写入操作；
- 删除请求完成后，不再被检索、注入或导出。

### 6.3 回归数据集

在 LangSmith 建立至少以下样本：

- 正确召回稳定偏好；
- 新偏好替代旧偏好；
- 不召回无关或跨租户记忆；
- 工具实时结果优先于旧对话或记忆；
- 对敏感或高影响记忆发起确认。

## 7. 验收标准

- Agent 能跨会话使用已确认的稳定记忆；
- 原始会话、摘要和长期记忆三者边界清晰；
- 所有记忆均有可信作用域、证据和生命周期；
- 当前金融事实只由工具/服务提供；
- 记忆召回和写入在 LangSmith、Manifest 与 Audit 中可追溯；
- 旧 Memory Runtime 已删除，生产链路不存在双写或双读。

## 8. 阶段结束决策点

只有当真实数据量证明 pgvector/PostgresStore 无法满足召回质量或延迟目标时，才评估外部向量数据库。评估必须包含隔离、删除一致性、成本和运维复杂度，不以“可能将来需要”为引入理由。
