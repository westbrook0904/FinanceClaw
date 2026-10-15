# M5：部署、恢复与交付验证

日期：2026-10-15。实现基线是本地提交 `1508802`（M4 长期记忆与原生飞书）。本阶段按**新环境、新会话**交付，不导入 Python 历史，不接管原飞书消费者。真实模型、远端 MCP、真实飞书租户和生产负载仍须独立验收。

## 部署结构

同一 Java jar / 镜像启动四个运行角色，使用 Spring Boot profiles、Flyway、Actuator 和 Spring configtree。Agent、Skill、MCP、embedding 和飞书协议继续复用 AgentScope，没有新执行引擎或渠道协议客户端。

| 服务 | 入口 / 工作 | 数据库身份与 HTTP |
| --- | --- | --- |
| `migrate` | 一次性 Flyway 迁移、运行权限与向量索引初始化，完成后退出 | schema owner `financeclaw`；无 HTTP |
| `api` | 受理、查询、SSE、业务交互、飞书 webhook | DML 角色 `financeclaw_app`；仅绑定本机 `127.0.0.1:8080` |
| `worker` | AgentScope 执行、任务租约与恢复 | `financeclaw_app`；容器内只开放健康检查 |
| `integrations` | Outbox 投影和原生飞书文本通知 | `financeclaw_app`；容器内只开放健康检查 |
| `memory-worker` | 记忆提取、索引和遗忘清理 | 独立 `financeclaw_memory`；只装配记忆模块和健康检查 |

前三种业务角色共用业务 DML 权限，没有 schema CREATE 权限；Memory Worker 无 business / agent_runtime / channel 权限。角色配置与启用的消费者不一致时启动失败，后台角色不接受业务 API 或飞书回调。每个 JVM 有自己的连接池和健康检查。

[Dockerfile](../../Dockerfile)固定 Temurin **21.0.12+8** JRE 的镜像摘要；[Compose](../../compose.yml)固定 PostgreSQL 16.12 / pgvector 0.8.1 镜像摘要。应用容器以非 root 用户运行、只读根目录，临时文件写入独立 tmpfs。应用等待数据库健康及迁移成功，不在四个运行进程中并行执行 DDL。

## 从空环境启动

需要已运行的 Docker Compose v2、项目固定的 JDK 和 Maven。首次构建需要下载 Maven 依赖与容器镜像。

```bash
python3 scripts/bootstrap-jdk.py  # 已安装时可跳过
python3 scripts/init-local.py
scripts/mvn.sh -B -ntp clean verify spotless:check
docker compose build api
docker compose up -d --wait --wait-timeout 180
docker compose ps
curl --fail http://127.0.0.1:8080/actuator/health/readiness
```

预期 `migrate` 成功退出，四个运行角色和 PostgreSQL 为 healthy，API 返回 `{"status":"UP"}`。默认模型为 `offline`；MCP、长期记忆和飞书默认关闭。Memory Worker 可启动等待，但没有模型配置时不伪造提取或 embedding 成功。

`init-local.py` 创建被 Git 忽略的 `.env` 和 `.secrets/`，不覆盖已有文件、不输出 secret 内容。数据库与 API 使用随机本地凭据，供应商凭据文件留空。`.secrets` 目录只允许当前用户访问；文件只读，以便 Compose 在非 root 容器中挂载读取。此方式是本地文件挂载，不是加密密钥管理服务。

配置入口是 [.env.example](../../.env.example) 和各角色的 Compose environment。非敏感配置写 `.env`；凭据写对应 `.secrets` 文件后重建受影响容器。编辑只读文件前临时 `chmod u+w`，完成后恢复只读。生产应由部署平台提供等价 secret 挂载，不将文件内容写入 Compose、镜像或 Git。

| secret 文件 | 使用方 |
| --- | --- |
| `db-admin` / `db-migrator` / `db-app` / `db-memory` | PostgreSQL 管理员 / 迁移 / 三个业务角色 / Memory Worker |
| `api-token` | API Bearer 身份 |
| `model-key` | 执行 Worker 的原生模型适配器 |
| `hotel-token` / `taibu-token` | 执行 Worker；Spring 环境解析凭据引用，原生 MCP 客户端使用 |
| `memory-model-key` / `embedding-key` | 独立 Memory Worker；API 的可选向量搜索另读取 embedding-key |
| `feishu-secret` / `feishu-token` / `feishu-encrypt-key` | API 入站与 Integrations 出站 |

Spring 的 [configtree 配置](https://docs.spring.io/spring-boot/3.5/reference/features/external-config.html)将挂载文件名映射为配置属性。密钥不进入发布 hash 或持久 AgentState。数据库首次初始化后，直接改文件不会修改数据库密码；凭据轮换需先更新数据库角色，再替换挂载文件并重启相关角色。

API 使用示例：

```bash
export FINANCECLAW_API_TOKEN="$(cat .secrets/api-token)"
curl --fail -H "Authorization: Bearer $FINANCECLAW_API_TOKEN" \
  -H 'Content-Type: application/json' -d '{}' http://127.0.0.1:8080/v1/conversations
curl --fail -H "Authorization: Bearer $FINANCECLAW_API_TOKEN" \
  http://127.0.0.1:8080/v1/operations
```

模型/业务参数会形成固定发布 hash。API、Worker、Integrations 必须使用相同发布配置；更新模型、MCP 或行情快照之前先排空活动任务。旧版本发布当前只在原进程内保留，跨配置升级的旧任务恢复仍会 `release_unavailable`，不能靠改任务中的 hash 绕过。

## 健康、容量与故障核对

使用原生 `/actuator/health/liveness` 判断进程存活；`readiness` 加入数据库和本角色的数据访问检查。数据库断开时 readiness 返回 503，恢复后重新变为 UP。后台角色不在宿主机暴露端口，可执行 `docker compose exec worker curl -fsS http://localhost:8080/actuator/health/readiness`。

`GET /v1/operations` 需要 Bearer 认证，仅汇总当前主体的任务、排队任务年龄、累计模型/工具调用次数、Outbox 积压和飞书通知状态，不返回消息、参数或结果正文。`provider_usage` / `provider_cost` 明确返回 `unavailable`：业务调用次数不是供应商 token 账单，当前未采集真实计费数据。`oldest_queued_ms` 从 Turn 创建时刻计算；恢复任务包含之前等待审批的时间，不代表本次排队耗时。

[预先冻结的本地阈值](../../ops/m5-thresholds.json)使用离线模型、12 个独立会话、4 个并发客户端，要求全部完成、p95 < 15 秒、数据库恢复 < 45 秒、结束时 Outbox 积压为 0。它用于回归部署行为，不能推导生产吞吐量。真实模型成本、供应商限流、通知延迟和容量阈值须在真实环境测试前另行确定。

| 故障 | 当前处理与运维动作 |
| --- | --- |
| API 重启 | 原幂等 key 重试，已有 Command 不丢失 |
| Worker 正常关闭 | 停止领取并中断本机执行；持久状态按既有回执/租约规则核对 |
| Worker 被强杀 | 依据原 attempt 与状态恢复；等待审批可以在新 JVM 回答；不自动重放不确定写操作 |
| 数据库暂时不可用 | API 503 / readiness DOWN；恢复后连接池重连，队列继续 |
| 模型/MCP 超时 | M2/M3 有界超时及账本规则继续生效；写结果未知时 blocked，禁止换 key 重放 |
| 飞书未知结果 | 保留 unknown / submitted_unverified，人工核查；SDK 不支持可靠查询，不能擅自重发 |
| 记忆模型不可用 | job 保留并有界退避，事实不回滚；检查 error_code 和独立模型配置 |
| Artifact 故障 | 本阶段 Artifact 存在 PostgreSQL，覆盖数据库备份；未启用 S3/MinIO，不声明对象存储故障已验收 |

## 备份、恢复与回退

当前保留所有 Java 业务事实、Journal、Interaction、执行账本和 Artifact；长期记忆遗忘按 M4 清理正文、索引和来源并提升 epoch。没有自动 TTL 清理，也没有 Python 历史导入器。备份包括敏感业务数据，必须受限保存并按部署方保留策略管理。

为获得包括队列、记忆和通知的可核对快照，先停止本 Java 部署的全部写入者；备份保留 owner 和 ACL，恢复环境先准备相同数据库角色与 pgvector 扩展支持：

```bash
umask 077
mkdir -p backups
docker compose stop api worker integrations memory-worker
docker compose exec -T postgres pg_dump -U postgres -d financeclaw_java -Fc > backups/financeclaw.dump
shasum -a 256 backups/financeclaw.dump > backups/financeclaw.dump.sha256
docker compose up -d --wait
```

在隔离环境恢复到**不存在的新数据库**，不覆盖原库；示例的目标名应为本次恢复专用名称：

```bash
docker compose exec -T postgres createdb -U postgres -O financeclaw financeclaw_restore_check
docker compose exec -T postgres pg_restore -U postgres -d financeclaw_restore_check \
  --exit-on-error < backups/financeclaw.dump
```

核对每张业务表的行数/hash、owner、Flyway 版本、privacy epoch、待处理 Command、unknown 工具调用及渠道通知，再将隔离的四角色数据库 URL 指向恢复库验证。自动实验已实现该核对流程，源码见 [validate-compose.py](../../scripts/validate-compose.py)。数据库身份与 Flyway owner 也随备份恢复，不能用 `--no-owner` 随意改变权限归属。

此模式的 RPO 是最后一次备份时点；尚未配置生产 WAL 归档/PITR。恢复旧备份前必须补齐备份之后的遗忘事实与 Java 新增业务事实，不能直接恢复旧库后恢复发送。Java 接受新任务后的回退保留当前完整备份和副作用账本，先隔离写入并人工核对；尚未交付向 Python 的反向导入或在线切换演练。

## 自动验证与 CI

```bash
python3 scripts/check-delivery.py
scripts/mvn.sh -B -ntp clean verify spotless:check
python3 scripts/collect-evidence.py
python3 scripts/init-local.py
python3 scripts/validate-compose.py
```

Compose 实验始终生成随机项目名和全新数据卷，强制关闭真实模型/MCP/飞书配置，使用独立的合成行情；结束后只删除自己创建的容器和卷。不要把该清理行为套用到业务部署。

[CI](../../.github/workflows/ci.yml)分开执行静态检查、单元/原生 HTTP 契约、真实 PostgreSQL/独立 JVM 集成和 Compose 交付实验。证据分别上传；Compose 上传不包含数据库 dump 或 secret。M5 归档中的源文件指纹和测试列表原生成于 `target/m5/results.json`（当前 M6a 收集器生成 `target/m6/results.json`），容器故障/恢复与性能结果保存在 `target/m5/compose-results.json`。

2026-10-15 本地最终结果：**144 项 Java 测试通过**（39 单元、105 集成，0 失败/错误/跳过），格式及静态交付检查通过；**9 项容器场景全部通过**。

| 场景 | 实际结果 |
| --- | --- |
| 空库启动与角色边界 | Flyway V1–V5 成功；四角色健康；后台 HTTP 和数据库权限检查通过 |
| 离线并发与幂等 | 12 个请求、4 并发，完成率 100%，p95 **1.38 秒**，没有真实供应商调用或费用 |
| 关闭、重启与强杀 | API 重启保留原受理；Worker SIGKILL 后审批恢复，只有一个发布步骤和一个报告 Artifact |
| 数据库中断 | readiness/API 返回 503；数据库启动后 **10.87 秒**内恢复并完成新任务 |
| 记忆与恢复 | 受限 Worker 处理明确偏好，遗忘提升 epoch；恢复库仍保持遗忘状态 |
| 完整备份与新库恢复 | **33 张表**行数/hash 一致；四角色连接恢复库后可查询旧结果、继续受理新任务，无重复发布 |

归档：[Java 测试与 131 个源码/配置文件指纹](evidence/m5-results.json)、[Compose 恢复与性能证据](evidence/m5-compose-results.json)。两份证据的 jar SHA-256 相同；容器证据另核对部署输入文件的指纹。临时容器及卷已清理。M5 已提交为本地 commit `b9ff13d`，未推送；测试证据中的实现基线为其父提交 `1508802`；这些结果不代表远程 CI 或真实供应商验收。

## 真实环境验收入口与剩余范围

此工作区未配置真实服务凭据，当前不宣称真实模型、MCP 或飞书验收通过。可选 [live workflow](../../.github/workflows/live-acceptance.yml)只接受人工触发，使用 GitHub `live-acceptance` Environment 的 URL 变量和 API token secret。它创建新会话并将 scope 收窄为只读，要求目标工具成功事件和终态同时存在，不把纯文本回答当工具调用证据。

本地也可显式运行；测试 prompt 不包含个人信息，配置目标必须是准备验收的真实测试部署：

```bash
export FINANCECLAW_LIVE_ACCEPTANCE=true
export FINANCECLAW_LIVE_BASE_URL=https://your-test-api.example
# FINANCECLAW_LIVE_API_TOKEN 从测试环境密钥管理注入
python3 scripts/validate-live.py --kind model --expected-tool search_catalog \
  --prompt '请调用 search_catalog 查询上海，并说明结果来源。'
python3 scripts/validate-live.py --kind mcp --expected-tool getHotelSearchTags \
  --prompt '请调用 getHotelSearchTags 查询酒店检索标签，并说明来源。'
```

真实飞书需要人工完成租户事件订阅、签名配置、绑定与审批：按 [M4 接入说明](m4-validation.md)绑定新 Java 会话，从飞书发送合成测试任务，记录 event ID、Turn/Interaction ID、原生工具执行次数与终态，再重启 Worker 验证原审批恢复。发送和审批由测试人员操作；上述脚本和 workflow 不替用户发送飞书消息。SDK 未支持的卡片/表单/可靠回执仍是明确缺口。

**未验收项**：真实供应商质量与成本、真实 MCP/飞书租户、CI 远程执行、生产 IAM（当前仍为单个配置的 Bearer 主体）、生产容量与告警、WAL/PITR、Python 历史导入及线上渠道切换。M5 的本地交付部分与这些外部验收条件分别记录，不能将本地成功称为完整迁移上线完成。
