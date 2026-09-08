# Stage 8A 正式服务验收

使用正式迁移、BFF、Ingress、Worker、LangGraph Adapter 与业务仓储。
在独占 PostgreSQL 新库上启动两个独立 Worker；BFF 只接收创建和用户决定，随后退出。
直接读取数据库判断验收结果，不用 GET／SSE 推进。查询真实 backend 的 operation metadata
独立确认没有重复远端 Run。

图使用正式发布和治理代码。市场研究的离线模型固定调用发布的提问工具；Workflow 使用
明确标识为合成的当前时点行情，避免演示数据固定日期使审批验收随日历失效。
没有真实用户资料、LLM 调用或 LangSmith tracing。

## 复现

安装项目及 `experiments/stage8/requirements.txt` 的探针依赖，准备独占本机 PostgreSQL 集群。
脚本只接受 localhost／127.0.0.1，创建随机名称的新数据库，不清空既有数据库。
将验证目录放到新建的临时目录，结束后自行移除该目录与本次新建数据库／独占容器。

```bash
.venv/bin/python -m experiments.stage8a.run \
  --postgres-url postgresql://postgres@127.0.0.1:55438/postgres \
  --directory /tmp/financeclaw-stage8a-verification \
  --report /tmp/financeclaw-stage8a-verification.json

FINANCECLAW_STAGE8_TEST_POSTGRES_URL=postgresql://postgres@127.0.0.1:55438/postgres \
  .venv/bin/python -m pytest -q \
  tests/stage8/test_coordinator.py tests/stage8/test_coordinator_edges.py
```

第二条命令为每个测试创建、最终删除独立数据库。包括四个 OS 进程同时领取、事务回滚、
取消、授权、旧驱动隔离、Inbox 关联、只读 HTTP/SSE 和破坏性 downgrade 保护。

`--cases` 可选择 `root child_question lost_callbacks lost_receipts worker_restart cancel
workflow_approve workflow_reject`。未传时执行全部八项。

自托管 PostgreSQL／Redis Agent runtime 可额外指定：

```bash
.venv/bin/python -m experiments.stage8a.run \
  --postgres-url postgresql://postgres@127.0.0.1:55438/postgres \
  --agent-image financeclaw-langgraph-api:latest \
  --redis-url redis://host.docker.internal:56389 \
  --license-env /approved/path/agent-license.env \
  --directory /tmp/financeclaw-stage8a-container-verification \
  --report /tmp/financeclaw-stage8a-container-verification.json
```

该模式面向 Docker Desktop，保持镜像的许可检查。`--license-env` 只读取
`LANGSMITH_API_KEY`／`LANGGRAPH_CLOUD_LICENSE_KEY`，不向镜像传递该文件内其他配置，
不记录凭据到证据；运行前须确认允许将指定许可项交给选定镜像。
缺少有效许可时 Agent Server 会拒绝启动，不能据此声称该运行时验收通过。

只有测试记录的 runtime/version 能被视为已验证，dev/inmem 结果不能替代自托管部署验证。
证据只输出计数、最终状态和代码摘要；原始日志留在指定临时目录，不纳入仓库。
