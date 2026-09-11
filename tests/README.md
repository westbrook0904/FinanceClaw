# 测试

```bash
.venv/bin/pytest -q
.venv/bin/ruff check financeclaw tests scripts
.venv/bin/ruff format --check financeclaw tests scripts
```

- `architecture`、`stage5`：服务依赖、发布一致性、认证、网络策略、审计、日志脱敏与制品隔离。
- `stage1`～`stage3`：叶子 Tool、模型调用、会话 Journal、上下文预算、Memory HITL 与隔离。
- `stage4`、`stage7`：受 Worker 执行域保护的 Workflow／领域 Agent、审批、制品、输入输出与领域规则。
- `stage6`、`stage6fix`、`stage6fixc`：飞书准入、资源门控、批次治理与原生交互。
- `stage8`：持久通知意图、投递分片、回执不确定性、撤销与并发发送。
- `stage8_hotfix`：当前 BFF 受理／恢复／取消／Webhook／Journal 事务、原生子图、共享根预算与释放租约；空库初始迁移和并发控制。
- `stage9`：原生摘要与 Turn 保护、无标注工具归档、画像/事件 embedding 次数、一次 HITL、索引重入、审计/删除恢复、租约隔离、数据库参数与保留回收边界。

领域单元测试使用固定 Worker scope；生产根集成测试保留完整发布和权限校验。
本机 HTTP 探针见 [实验说明](../experiments/stage8_hotfix/README.md)。PostgreSQL 多进程测试需要专用数据库，真实 Provider 与飞书 WebSocket 测试需显式注入对应测试配置；未满足条件时跳过。
[Stage 9 探针说明](../experiments/stage9/README.md)包含真实 HTTP/Store/重启及隔离 pgvector 验证。它们使用合成模型，不证明真实中文语义质量或生产 HA 行为。
