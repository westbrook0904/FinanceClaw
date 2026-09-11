# Stage 9 能力探针

在仓库根目录、已同步 `dev` 依赖的 Python 3.13 环境运行。HTTP 探针仅使用合成模型与假向量，隔离运行目录中的业务 SQLite、Artifact 和 dev Agent Server state；不会读取真实模型密钥。必须每次指定一个新的临时目录：

```bash
.venv/bin/python -m experiments.stage9.native_probe \
  --directory /tmp/financeclaw-stage9-native-run \
  --output /tmp/financeclaw-stage9-native.json
```

PostgreSQL 探针另需 `agent-server` 依赖组的 `langgraph-checkpoint-postgres`，以及已安装 pgvector 的测试数据库。通过进程环境提供 `FINANCECLAW_TEST_POSTGRES_DSN`，不要把凭据写进证据文件或命令历史。探针创建随机 schema、验证实际表归属，结束时只删除自己的 schema：

```bash
.venv/bin/python -m experiments.stage9.postgres_probe \
  --output /tmp/financeclaw-stage9-postgres.json
```

不创建数据库或安装数据库扩展。`index=False` 不擦除当前 PostgreSQL Store 已有向量，探针分别检查状态过滤和原生删除行为。失败时不覆盖旧的成功证据；运行退出码必须与输出文件一起检查。

业务并发回归使用独立变量 `FINANCECLAW_TEST_POSTGRES_URL`，写入前先验证 SQLAlchemy 连接的 `current_schema()`：

```bash
.venv/bin/pytest tests/stage6fix/test_postgres_concurrency.py -q
```

已执行结果及未验证的真实模型质量范围见 [Stage 9 实现与验证](../../.redesign/stages/stage-9-实现与验证.md)。
