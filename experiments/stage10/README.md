# Stage 10 持久运行验证

探针只操作 `financeclaw-stage10-probe` 项目内的 PostgreSQL/Redis，使用合成用户与离线模型。它不连接现有本机业务数据库，不发送真实飞书消息。结果写入 `.redesign/evidence/stage10/`，实施结论见 [验证报告](../../.redesign/stages/stage-10-实现与验证.md)。

## 前提与空库初始化

构建根目录的 `Dockerfile` 为 `financeclaw:stage10`。当前探针的 `env_file` 指向本机私有 `.env.agent-server.local`，其中需要官方持久 AgentServer 所需的 LangSmith key 或 license；可将该路径替换成自己的私有凭据文件。该文件不是提交内容。产品用户和集成令牌均为 Compose 中的合成值，不能用于部署。

```bash
docker build -t financeclaw:stage10 .
docker compose -f experiments/stage10/compose.probe.yml up -d postgres redis
docker compose -f experiments/stage10/compose.probe.yml run --rm product-migrate
docker compose -f experiments/stage10/compose.probe.yml run --rm \
  -e FINANCECLAW_DATABASE_URL=postgresql+psycopg://probe:probe@postgres:5432/stage10_migration \
  product-migrate
```

`init.sql` 只在全新探针 PostgreSQL 初始化时创建两个空应用库。`probe` 是原生数据库，`stage10_product` 用于产品端到端验证，`stage10_migration` 用于迁移与 SQL 并发验证。已有卷不会再次执行初始化脚本。不要把这些命令改成现有用户数据库。

## 产品受理与持久恢复

普通受理已经用统一镜像的正式角色入口验证。这里为嵌套审批额外使用 `fixture_entrypoint.sh`，仅把图工厂换成 `product_fixture.py`：替换演示行情的固定旧日期，产生带当前时间的合成行情。API、Turn 服务、审批、预算、真实图及原生 checkpoint 均使用镜像中的产品实现，未绕过发布校验。

当前 Compose 将 `FINANCECLAW_TURN_JOIN_SLOTS` 设为 1，以便重现观察槽满；测正常容量时改为 128，并重新创建 API 容器。

```bash
# 此时不要启动 generic api/worker，也不要启动 product-api-2。
docker compose -f experiments/stage10/compose.probe.yml up -d product-api
docker compose -f experiments/stage10/compose.probe.yml exec -T product-api \
  python /project/experiments/stage10/product_scenarios.py queued

docker compose -f experiments/stage10/compose.probe.yml up -d product-worker product-integrations
docker compose -f experiments/stage10/compose.probe.yml exec -T product-api \
  python /project/experiments/stage10/product_scenarios.py waits

# 验证等待中的原生 checkpoint 可以跨两个角色重启。
docker compose -f experiments/stage10/compose.probe.yml restart product-api product-worker
docker compose -f experiments/stage10/compose.probe.yml stop product-worker
docker compose -f experiments/stage10/compose.probe.yml exec -T product-api \
  python /project/experiments/stage10/product_scenarios.py resume

# 恢复命令已受理，再重启 API 并启动 Worker。
docker compose -f experiments/stage10/compose.probe.yml restart product-api product-worker
docker compose -f experiments/stage10/compose.probe.yml exec -T product-api \
  python /project/experiments/stage10/product_scenarios.py finish
```

`waits` 覆盖根图提问和真实嵌套 Workflow 审批；`resume` 重放相同幂等键；`finish` 检查同一 Turn 收尾、两条 Journal、原生外部权限和限定 Store CRUD。审批选择 reject，验证拒绝能穿过原中断包装工具返回，同时不发布报告。

## 数据库、并发与多 API

```bash
docker compose -f experiments/stage10/compose.probe.yml exec -T \
  -e FINANCECLAW_DATABASE_URL=postgresql+psycopg://probe:probe@postgres:5432/stage10_migration \
  product-api python /project/experiments/stage10/postgres_probe.py

# 单 API 测量；capacity 要使用 128 个 join 槽，join_saturated 要使用 1 个。
docker compose -f experiments/stage10/compose.probe.yml exec -T product-api \
  python /project/experiments/stage10/performance_probe.py capacity
docker compose -f experiments/stage10/compose.probe.yml exec -T product-api \
  python /project/experiments/stage10/performance_probe.py join_saturated

# 多 API 同键竞争、跨副本 SSE、真实 integrations → Store 索引。
docker compose -f experiments/stage10/compose.probe.yml up -d product-api-2
docker compose -f experiments/stage10/compose.probe.yml exec -T product-api \
  python /project/experiments/stage10/replica_probe.py
```

性能脚本记录所有样本，不会把超出建议预算的结果伪装为通过；其完成条件只保证任务正确完成并收到 SSE。原生终态时间取隔离 `probe.public.run.updated_at`，这是探针的只读测量，应用代码不读取原生数据库表。批量快照 SQL 数量由 `tests/stage10/test_events.py` 在 SQLAlchemy 边界实测。

`contract_app.py` / `contract_probe.py` 是 S10-0 独立框架契约探针，使用 `financeclaw-langgraph-api:latest` 镜像别名及 generic `api/worker` 服务。必须与产品运行分开启动，避免两个 Worker 消费同一队列中的不同图定义。框架结果已保存在 `native-contract.json`；产品探针不依赖其假图。

## 清理

```bash
docker compose -f experiments/stage10/compose.probe.yml --profile product stop
```

停止保留验证数据。只有明确不再需要这些隔离测试数据时，才自行删除对应探针容器或卷；不要删除现有 `financeclaw-local` 的资源。
