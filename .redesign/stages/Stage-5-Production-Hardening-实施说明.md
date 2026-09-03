# Stage 5：生产加固实施说明

## 1. 阶段目标

本阶段将已经跑通的 Agent、Tool、Conversation、Memory 和 Workflow 纵向链路加固为可上线、可审计、可恢复、可控制成本的生产系统，并完成剩余旧框架代码清理。

本阶段不是继续扩展抽象层，而是用安全、评测、容量和运维证据验证新架构。

## 2. 部署基线

### 2.1 运行组件

- FinanceClaw API/BFF：唯一公开入口与业务安全边界；
- LangGraph Agent Server：内部执行平面；
- PostgreSQL `financeclaw_app`：业务日志、上下文、审批、Audit 和制品元数据；
- PostgreSQL `financeclaw_agent`：Agent Server 管理的 thread/run/checkpoint/store 数据；
- Redis：Agent Server 队列与发布订阅；
- S3 兼容 Artifact Store：报告和大对象；
- LangSmith：Agent/Workflow/Model/Tool/Middleware 观测与评测；
- OpenTelemetry backend：HTTP、数据库、Redis 和外部服务基础设施观测。

生产部署前必须确认 LangGraph Agent Server 的部署形态、许可证、数据驻留、升级兼容与支持边界满足组织要求。

### 2.2 Python 与依赖

- Python 固定为 `>=3.13,<3.14`；
- 使用锁文件固定 LangChain、LangGraph、LangSmith SDK 与 Provider 包；
- 升级通过独立分支和回归数据集验证；
- 禁止在生产自动漂移到未验证的小版本；
- 生成 SBOM 并执行依赖漏洞扫描。

## 3. LangSmith 生产体系

### 3.1 环境隔离

- development、test、staging、production 使用独立 LangSmith project；
- trace metadata 只保存受控标识、版本、哈希和引用；
- 输入输出 masking/sampling 按环境配置；
- production 默认不记录完整 Prompt 和工具结果；
- 调试临时放开必须有审批、范围、到期时间和 Audit。

### 3.2 评测门禁

建立版本化 LangSmith datasets，至少覆盖：

- 工具选择与参数正确性；
- 自然语言路由、slash 指令、缺参提槽与多轮补槽；
- Workflow/领域 Agent 委派的父子 run 关联和根会话所有权；
- Policy 拒绝和审批行为；
- 上下文召回、摘要与记忆注入；
- 金融数据来源和 `as_of` 使用；
- Workflow 分支、恢复和制品质量；
- 提示注入、越权工具和跨租户攻击；
- Provider 故障、降级和超时。

每次 ModelProfile、Prompt、Tool Schema、Middleware、Graph 或依赖升级均运行离线实验。关键指标未达到基线不得发布。

### 3.3 在线评测

- 对生产 trace 抽样运行质量、安全和成本评估；
- 高风险失败进入人工复核队列；
- 评测结果关联 trace、版本和业务请求，但不替代正式 Audit；
- 在线 evaluator 失败不能阻塞核心请求，应降级并告警。

## 4. 安全加固

### 4.1 身份与隔离

- BFF 验证 OIDC/JWT，并生成不可由请求体覆盖的 `ExecutionContext`；
- 所有数据库查询、Store namespace、Artifact key 和 LangSmith metadata 都带可信租户范围；
- Agent Server 只允许来自内部网络和服务身份的请求；
- 对 conversation、workflow run、approval 和 artifact 做对象归属校验；
- 使用自动化测试验证跨租户读写均失败。

### 4.2 工具与外部访问

- 所有工具进入 `ToolCatalog` 前完成治理元数据登记；
- `ToolPolicy` 在模型可见工具过滤与实际执行前各校验一次；
- 高影响工具使用 LangGraph interrupt 和业务审批；
- 外部 HTTP 通过 allowlist、超时、连接池和出站代理控制；
- MCP Server 按信任等级隔离，返回内容视为不可信输入。

### 4.3 凭据与隐私

- 凭据仅存 Secret Manager，运行期以句柄解析；
- 禁止进入 Prompt、State、checkpoint、conversation、memory、artifact、trace 或普通日志；
- 完整 Prompt 调试只允许开发/测试环境，并实施字段脱敏；
- 建立用户数据导出、撤销、删除和证据链流程；
- 定期扫描日志与 trace 的敏感信息泄露。

## 5. 可靠性与灾难恢复

- 为 PostgreSQL、Redis 和 Artifact Store 定义 RPO/RTO；
- 验证备份恢复，而不只验证备份任务成功；
- 为 BFF 与 Agent Server 设置健康检查、优雅关闭和滚动升级；
- 验证运行中 Workflow 在实例替换后恢复；
- 使用 outbox 保证业务状态、Audit 与异步事件的最终一致性；
- 对 LangSmith、评测服务和非关键摘要任务设计非阻塞降级；
- 定义 Provider 不可用、数据过期和部分工具失败时的用户可见行为。

## 6. 性能与成本

### 6.1 目标指标

至少定义并验证：

- API P50/P95/P99 首字节和完成时间；
- Agent 每轮模型调用次数、工具调用次数和 token；
- Context 各分区 token 使用；
- 摘要和长期记忆召回延迟；
- Agent Server queue wait、run duration、checkpoint 延迟；
- Workflow 成功、恢复和人工审批等待率；
- 单租户与单请求成本。

### 6.2 控制手段

- `ContextBudget` 对各上下文分区设置硬上限；
- 简单工具调用不经过不必要的模型或 Planner；
- 模型 fallback 由 LangChain 配置并限制最大尝试次数；
- 摘要、embedding 和评测使用异步队列和明确预算；
- 大型工具结果落 Artifact，不反复进入 Prompt；
- LangSmith sampling 与保留策略按风险和环境调整。

## 7. 最终旧代码清理

完成新链路和迁移门槛后：

- 删除 `harness-trace` 中被 LangSmith/OTel/Audit 取代的实现；
- 删除 `harness-events` 中被 Agent Server 事件与业务 outbox 取代的通用事件总线；
- 删除 `harness-bootstrap` 中旧 Runtime 装配；
- 删除 `harness-policy` 中剩余通用 PolicyEngine，只保留明确的 ToolPolicy 与领域规则；
- 将 `harness-contracts` 收敛到仍有业务价值的 API/domain contracts，或迁入 `financeclaw` 后删除空壳模块；
- 清理示例 plugin、旧测试、构建配置、README 和依赖声明；
- 确认生产镜像中不存在旧 Runtime、Planner、Registry、Selection、SPI 或 Capability 依赖。

清理必须遵循[旧模块删除映射](../migration/旧模块删除映射.md)与[依赖与迁移顺序](../migration/依赖与迁移顺序.md)。

## 8. 上线验证

### 8.1 自动化测试

- 单元、契约、集成、端到端和数据库迁移测试；
- LangSmith 离线回归与安全评测；
- 跨租户、提示注入、工具越权和凭据泄露测试；
- 禁止公开 Target、禁止未授权直连、指令不静默替换和错误 slot 不执行测试；
- 负载、容量、长会话和超大工具结果测试；
- PostgreSQL、Redis、Provider、LangSmith 和网络故障注入；
- Workflow 中断恢复和幂等副作用测试。

### 8.2 发布门禁

- 数据库迁移可前滚并有明确回滚策略；
- 关键 SLO、告警、值班手册和 Runbook 就绪；
- LangSmith datasets 基线全部通过；
- 备份恢复演练通过；
- 安全评审与威胁模型关闭高风险项；
- 新旧链路切换完成，旧 Runtime 不再承载流量；
- 所有临时 feature flag 有所有者和删除日期。

## 9. 验收标准

- 生产请求只经过 BFF 与 Agent Server 新链路；
- LangSmith、OTel、结构化日志和 Audit 各自边界清晰且能关联同一请求；
- 多租户隔离、ToolPolicy、审批、Secret 与出站访问通过安全测试；
- 长会话、记忆、工作流和故障恢复满足既定 SLO；
- 依赖锁定在 Python 3.13 兼容版本并通过供应链扫描；
- 旧框架代码、依赖和兼容层已从主分支删除；
- 发布和回滚不依赖保留双 Runtime。
