# 应用运行结构

HTTP API、AgentScope 执行、任务恢复、SSE 和飞书投递运行在同一个 JVM。业务操作使用主连接池，续租从总连接预算内预留独立连接，见[并发资源配置](concurrent-recovery-resources.md)。项目是单个 Maven 应用，不提供旧的 API/Worker/Integrations 分体 profile。P3a.20 历史基线与本批新增运行协调的验证边界见[非 RAG 集中实施](non-rag-completion-plan.md)。

| 服务或入口 | 用途 |
| --- | --- |
| Compose `api`，入口 `standalone` | 统一业务进程 |
| `memory-worker` | 记忆提取、索引和清理，使用受限数据库身份 |
| `init-db` | 新库结构初始化与授权，完成后退出 |
| `privacy-recovery` | 停写隔离环境中的遗忘事实初始化与恢复重放 |
| `postgres` | 权威业务状态、结果和记忆事实 |

本批 SSE 先订阅进程内事件与 PostgreSQL 持久唤醒，再读取游标之后的持久事件，并按修订号去重。提交后的 `LISTEN/NOTIFY` 和修订号缺口触发补读；空闲心跳不查询消息表，但会重新核验当前权限与隐私门禁。每个业务 JVM 从既有业务池占用一条通知连接，不使用续租预留连接。连接数量、缓冲区和时长受限，连接结束不表示业务任务完成。监听失联时关闭旧流，客户端携带 `Last-Event-ID` 重连补齐持久事件，详见[运行协调](runtime-coordination.md)。

本批新增服务端可信多主体与角色目录，并保留默认 Bearer 映射一个主体的部署方式，均不能由请求正文选择用户。共享 Agent 和凭据使用独立的当前资源授权，见[身份与资源授权](resource-authorization.md)。持久事件具备跨 standalone 实例唤醒与回放机制；模型文本增量和临时工具进度仍仅在执行器所在 JVM 内传播，完整实时订阅仍需连接执行所在实例。默认 Compose 保持一个业务副本，本批验证结果与部署边界见[集中实施记录](non-rag-completion-plan.md)。

Worker 使用提交后的进程内通知唤醒，以最长五秒空闲退避扫描兜底。过期执行单独恢复，心跳使用专用调度器和预留连接池。资源隔离不能消除数据库锁争用，续租仍受语句/锁超时和不可复活的租约校验约束。

启动见[新库初始化](fresh-schema.md)。常规停写：

```bash
docker compose stop api memory-worker
```

数据库备份恢复、旧备份的隐私核对和独立遗忘事实重放是当前应用的安全运行需求，继续保留。操作入口见[运行维护](../operations.md)，不得让业务进程连接未经隐私核对的恢复库。
