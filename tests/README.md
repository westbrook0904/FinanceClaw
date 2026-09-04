# Tests

当前回归基线覆盖 Stage-1 Execution Spine、Stage-2 Conversation Context、Stage-3
Long-term Memory、Stage-4 Published Workflows、Stage-5 Production Hardening 和 Stage-6
Feishu P2P Channel。旧 Spike、Trace、Events 与混合 Contracts 测试已随对应实现删除。

```bash
.conda/envs/stage0/bin/python -m pytest -q
```

`tests/stage1` 重点验证：

- ToolGovernance、不可变 Catalog、Policy 与 TargetResolver；
- Direct READ retry/failure 和 WRITE interrupt/approve/reject/edit/reapproval；
- Agent Tool Calling、模型可见性过滤、执行时二次鉴权、model fallback；
- MCP 治理覆盖；
- BFF 认证上下文、idempotency、run/status/resume/SSE；
- 新代码与已删除旧 Runtime/Registry/SPI 的依赖隔离。

`tests/stage2` 重点验证：

- Journal append-only、branch、idempotency、owner/tenant 与 Thread/Profile 固定映射；
- 跨进程恢复、Agent Server turn reconciliation 和 Conversation API；
- token budget、分段/分层摘要、相关历史召回与可重建 provenance；
- 每次模型调用的 Manifest、完整 Prompt 调试开关和 Secret 脱敏；
- Artifact offload/hash/owner/scope；
- Alembic upgrade/downgrade/re-upgrade 及旧 Context Runtime 依赖隔离。

`tests/stage3` 重点验证：

- 可信 namespace、Journal evidence、四种类型和金融时效事实边界；
- propose/confirm HITL、跨 thread 召回、租户隔离和独立 memory token budget；
- supersede/revoke/delete 后不再检索或注入；
- Prompt data-only 区域与 Manifest 版本/原因引用一致；
- 永久 Audit、Alembic schema、LangSmith dataset seed 和旧 Memory Runtime 删除。

`tests/stage4` 重点验证：

- WorkflowCatalog、固定版本/拓扑、严格输入输出 Schema 和受控 Tool 集合；
- 行情来源/时间、确定性分析、数据过期分支和只读瞬时故障重试；
- durable interrupt、批准/拒绝/过期、原参数 hash 复验和永久 Audit；
- 业务 run/thread/server run/version/artifact 映射、租户隔离和跨服务实例恢复；
- 请求级与报告节点级幂等、新旧版本并行和普通 Agent 路径隔离；
- Alembic `0003_stage4`、LangSmith dataset seed 和旧 Planner/DAG Runtime 删除。

`tests/stage5` 重点验证：

- OIDC/JWT issuer、audience、时效、非对称算法和可信 tenant/subject/scope 投影；
- 生产配置 fail-closed、外部 HTTP allowlist 与跨租户资源隔离；
- Audit/Outbox 原子写入、租约发布和 S3 加密租户 key；
- 结构化日志凭据脱敏、OTel 低基数字段和组合健康检查；
- 版本化回归数据集、Alembic `0005_stage5`、锁文件、SBOM 与漏洞扫描门禁。

`tests/stage6` 重点验证：

- Agent Server `runs.join_stream` 的 run 级订阅与稳定脱敏事件投影；
- 飞书 P2P/用户/文本准入、可信 tenant/open_id 映射和机器人回声过滤；
- 单聊 Conversation 绑定、多轮复用、message_id 幂等、同 chat 串行与跨 chat 隔离；
- 流式 Markdown 最终 Journal 校正、CardKit 失败文本降级和默认关闭配置；
- Alembic `0006_stage6` 升级、回滚与再次升级。

真实 Provider、LangSmith 与 Agent Server 网络联调由显式 smoke/probe 命令执行，不用 mock 结果
冒充在线证据。飞书 WebSocket 连接探针在未注入 `FINANCECLAW_FEISHU_E2E_*` 专用凭证时显式
skip；连接 ready 也不替代 canary 用户实际发消息的 P2P 验收。
