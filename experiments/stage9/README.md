# Stage 9 上下文与 Store 能力实验

本目录保留 Stage 9 的框架能力与历史验证材料。当前产品记忆已变为 Stage 11 的 SQL 事实源、独立 memory_worker 与 Store 检索投影；历史实验的 namespace、工具字段和业务装配不能直接作为当前生产契约。

## 各入口能证明什么

| 入口 | 范围 | 当前使用方式 |
|---|---|---|
| `native_probe.py` / `server.py` | 隔离 dev Agent Server、假模型/假向量、旧上下文和记忆工具流程 | **历史复现入口**；仍使用旧 `begin_turn` / 记忆装配接口，不能据此宣称当前 HEAD 可直接执行 |
| `postgres_probe.py` | 真实 PostgreSQL Store 的写入、状态过滤、连接重建和删除 | 独立框架探针；需隔离测试数据库和 `agent-server` 依赖 |
| `tests/stage11` | 当前记忆、上下文、索引、API 与后台任务契约 | 当前回归入口；真实 PostgreSQL 用例另需显式环境与权限 |

Stage 9 的已执行结果见[历史实现与验证记录](../../.redesign/stages/stage-9-实现与验证.md)。该记录中的通过数量和 dev 运行时能力只对应当时的代码与依赖，不是本次文档整理重新运行的结果。

## PostgreSQL Store 探针

从仓库根目录使用 Python 3.13。安装开发与 `agent-server` 可选依赖，并准备已启用 pgvector 的专用测试数据库。通过安全的进程环境注入 `FINANCECLAW_TEST_POSTGRES_DSN`；不要在命令历史或报告中写入凭据。

```bash
uv sync --frozen --extra dev --extra agent-server
.venv/bin/python -m experiments.stage9.postgres_probe \
  --output /tmp/financeclaw-stage9-postgres.json
```

探针创建随机 `stage9_probe_*` schema，验证实际表归属，结束时只删除自己的 schema；它不会创建数据库或安装扩展。测试账号需要创建/删除 schema 的权限。输出检查以下性质：`index=False` 是否保留旧向量、状态过滤是否屏蔽非活动内容、原生删除是否清除向量、重建连接后数据是否可读。

使用的是 3 维确定性假向量，`semantic_quality_verified=false`，不能证明真实 embedding 或中文召回质量。报告写入失败与断言失败都需检查退出码；不要只看到旧成功文件就当作本次通过。

## 当前记忆 PostgreSQL 回归

以下用例使用 `FINANCECLAW_TEST_POSTGRES_URL`，它是 SQLAlchemy URL，与上面的原生 psycopg DSN 变量不同。未设置时用例会 skip，不能统计为真实 PostgreSQL 验证通过。

```bash
.venv/bin/python -m pytest -q \
  tests/stage11/test_worker_postgres.py \
  tests/stage11/test_domain_postgres.py
```

这两份测试创建临时 `stage11_worker_*` schema，验证租约、并发、owner 串行化和初始迁移，再清理自己的 schema。数据库角色隔离另见 [`test_worker_role_postgres.py`](../../tests/stage11/test_worker_role_postgres.py)：该文件会创建/删除独立临时数据库及角色，需要额外的集群权限，不能与普通 schema 探针混为一谈。

旧文档中的 `tests/stage6fix/test_postgres_concurrency.py` 已移除。当前产品启动见[本地运行](../../docs/operations/local-full-stack.md)，记忆恢复与索引重建见[上下文运维](../../docs/operations/context-budget.md)。

## 历史 HTTP 探针复现

若需要审计原始 Stage 9 结论，使用与历史证据匹配的源码和依赖快照，在新的临时目录复现：

```bash
.venv/bin/python -m experiments.stage9.native_probe \
  --directory /tmp/financeclaw-stage9-native-run \
  --output /tmp/financeclaw-stage9-native.json
```

这是历史快照下的命令模板。将该探针迁移到当前 API/记忆契约需要单独更新实验代码；本次文档整理未修改探针实现。历史隔离方式使用目录内的 SQLite、Artifact 和 dev state，不使用真实模型，但也不提供持久生产运行的保证。
