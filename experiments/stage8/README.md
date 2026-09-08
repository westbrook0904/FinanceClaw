# Stage-8.0 可复现实验

这里验证 Coordination 的基础推进，使用 `ProbeDriver`、正式 Journal／operation 仓储、
真实 PostgreSQL、真实 LangGraph HTTP API 和两个独立 OS Worker。
探针图使用合成输入与原生 interrupt，不调用 LLM、不发送飞书消息、不读取项目 `.env`。

实验实现不会打进 FinanceClaw wheel，不由 BFF／AgentServer bootstrap 导入。`stage8_*` 表使用
独立 SQLAlchemy metadata，只在本机实验集群中新建的 `financeclaw_stage8_<随机值>` 数据库创建。
不要把这些表或实验 Driver 直接作为 8A 的产品迁移。

## 准备

在仓库根目录，使用项目的 Python 3.13 创建独立环境：

```bash
.venv/bin/uv --cache-dir /tmp/financeclaw-stage8-uv-cache venv \
  --python .venv/bin/python /tmp/financeclaw-stage8-repro-venv
.venv/bin/uv --cache-dir /tmp/financeclaw-stage8-uv-cache pip install \
  --python /tmp/financeclaw-stage8-repro-venv/bin/python \
  -e '.[dev]' -r experiments/stage8/requirements.txt
```

启动独占的实验 PostgreSQL。端口只映射到 loopback，trust 认证只用于这个无真实数据的临时容器：

```bash
docker run --detach --rm --name financeclaw-stage8-postgres \
  --label financeclaw.experiment=stage8 --publish 127.0.0.1:55438:5432 \
  --env POSTGRES_HOST_AUTH_METHOD=trust --env POSTGRES_DB=financeclaw_stage8 \
  postgres:16@sha256:f1c3376c26f2609ab9f29f71f824103fe2fcd8ee0346485cb6122a4f93df6f94
```

## 执行

```bash
/tmp/financeclaw-stage8-repro-venv/bin/python -m experiments.stage8.run \
  --output /tmp/financeclaw-stage8-repro-evidence
```

`--database`／`STAGE8_DATABASE_URL` 可指定其他本机实验集群；脚本总是新建随机数据库，
不清空已有库。Agent Server、Ingress 端口自动分配，
回调 token 每次随机生成，既不写入配置文件，也不写入报告。

报告 `report.json` 仅在全部断言通过后生成。日志中预期会出现合成图错误、HTTP 503、
`LostReceipt` 触发的 Worker 接管；这些是故障用例。

| 文件 | 验证内容 |
|---|---|
| `transaction_probe.py` | 6 个受理／4 个完成 flush 故障；4 进程唯一受理和 SKIP LOCKED；过期 epoch、新唤醒、driver 版本保护 |
| `webhook_probe.py` | 成功、错误、静态头认证、503 重试、回执查询；实际启动验证 outbound allowlist |
| `backend.py` / `graphs.py` | 原生委派／用户 interrupt、精确 checkpoint 恢复、匹配原结果的 ToolMessage 证据 |
| `run.py` / `processes.py` | 7 个基础闭环用例，加不可查明回执＋取消；全部 Worker 杀死后恢复；不启动 BFF 或 SSE |

`run.py` 的用户决定是测试程序显式写入；只读投影查询不调用 Driver。回调丢失用例让 HTTP
接收器丢弃事件，通过持久化到期责任恢复。无法查回执用例在领取命令后模拟崩溃，验证查不到
不导致重发，取消也不能伪造远端停止。该用例保留未解决业务状态；结束
Worker 进程不会被记作业务取消确认。

运行结束后，脚本关闭自身的 Agent Server、Ingress 和 Worker。移除独占数据库容器；按需保留 `/tmp` 中的证据文件：

```bash
docker stop financeclaw-stage8-postgres
```

已验收证据与边界见 [Stage-8 实施与验证](../../.redesign/stages/Stage-8-实施与验证.md)。
