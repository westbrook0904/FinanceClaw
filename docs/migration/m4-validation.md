# M4：长期记忆与 AgentScope 原生飞书接入

日期：2026-10-15。基线为本地提交 `abfb808`（M3 原生 Skill/MCP 与业务能力）。JDK 21.0.12+8、Spring Boot 3.5.16、AgentScope 2.0.3 保持不变。

当前代码结构已在 [R5c 记忆与发布分层](../architecture/r5c-implementation.md) 中更新：HTTP/任务调用 [记忆用例](../../src/main/java/com/financeclaw/app/memory)，[领域规则](../../src/main/java/com/financeclaw/domain/Memory.java) 与 [AgentScope 适配](../../src/main/java/com/financeclaw/infrastructure/agentscope/memory/AgentScopeMemoryModels.java)、[MyBatis 存储](../../src/main/java/com/financeclaw/infrastructure/memory/MyBatisMemoryRepository.java) 分离。API、数据库和配置契约不变；本页下方的 M4 验证数字保留为阶段历史，最新完整回归见 R5c。

## 本阶段范围

长期记忆使用 SQL 管理事实、来源、候选、隐私版本及处理意图，模型提取和 embedding 使用 AgentScope。飞书按用户要求采用固定版 SDK 已有能力：原生验签/解密、入站消息映射、token 管理和文本发送。**不自建 CardKit、卡片表单或飞书 HTTP 客户端**。

| 层次 | 实现与边界 |
| --- | --- |
| 记忆提取 | 原生 `ReActAgent.call(..., Extraction.class)` / `getStructuredData`；只输入当前可信 USER 原文，没有历史、Skill、MCP 或业务工具 |
| 向量模型 | 原生 `OpenAITextEmbedding`，请求 1024 维；应用再次检查实际维度、有限数值、非零向量 |
| 记忆事实 | MyBatis + Spring 事务；模型产物是候选，仅精确 `/remember` 枚举指令可自动确认 |
| 召回 | Turn 受理时冻结 fact/revision/epoch 引用；原生 `onSystemPrompt` middleware 投影；进入模型请求预算 |
| 飞书协议 | `FeishuCrypto`、`FeishuInboundMapper`、`FeishuChannelProperties`、`FeishuAccessTokenProvider`、`FeishuOutboundClient` |
| 业务适配 | 一次性身份绑定、持久事件去重、已有 TaskService / GovernanceService 的调用、通知去重与状态 |

没有使用 SDK 的默认 `FeishuCallbackController → Gateway.run` 整条示例链路：其内存去重先于执行，且直接同步执行 Agent，无法接入本项目已经实现的 SQL Turn/Command 事务。应用回调只补强认证、持久受理和业务映射，协议解析与收发仍由 SDK 完成。没有额外 Agent/会话引擎。

## 记忆如何工作

1. 开启记忆后，创建真实业务 Turn 的事务同时保存 `memory.sources` 用户原文与当时的隐私 epoch，并冻结最多 16 条确认偏好的引用。历史工具、模型回复、Skill 资源不会成为提取输入。
2. 仅 `completed` 任务在完成事务中将来源置为 pending，并写入唯一提取 job；事务回滚不会留下独立处理意图，失败/取消任务不提取。
3. 独立 Memory Worker 领取带租约 token 的 job，在事务外调用原生模型；结果写回前重新核对租约、owner、epoch。提取不超过 8 条，key 白名单、value 长度和逐字来源引用均校验。
4. 普通提取进入候选，通过记忆 API 确认后才生效。只有精确指令 `/remember response_language=中文`、`/remember response_language=English`、`/remember response_style=简洁`、`/remember response_style=详细` 自动确认。确认记忆不会恢复业务 Interaction。
5. 同 key 偏好由新版本取代；迟到的旧来源不能覆盖更新的明确偏好。索引 job 独立于事实提交，模型故障保留处理意图；索引故障不回滚事实。
6. 目前偏好种类只有语言、表达风格、常住城市、出行偏好、投资偏好。默认召回读取当前已确认的有限偏好集合，避免在 Turn 受理事务中调用远端模型。可选语义搜索使用同一 SQL 事实和 pgvector 投影。

### 启用与独立 Worker

API/任务进程设置 `FINANCECLAW_MEMORY_ENABLED=true`。运行 API 的普通业务账号先执行 Flyway V5；新增 `memory`、`channel` schema，不改写或删除 V1–V4 表。

```bash
export FINANCECLAW_MEMORY_ENABLED=true
export FINANCECLAW_MEMORY_MODEL=<结构化提取模型>
export FINANCECLAW_MEMORY_MODEL_BASE_URL=<OpenAI兼容地址>
export FINANCECLAW_EMBEDDING_MODEL=<支持1024维的embedding模型>
export FINANCECLAW_EMBEDDING_BASE_URL=<embedding地址>
export FINANCECLAW_EMBEDDING_REVISION=<模型及配置的固定版本>
# 提取和 embedding 使用各自的 FINANCECLAW_MEMORY_MODEL_API_KEY、FINANCECLAW_EMBEDDING_API_KEY。
# 不复用业务工具凭据；由环境或密钥管理注入。
```

由数据库管理员创建独立 LOGIN `financeclaw_memory` 并设置密码，然后执行 [memory-worker-grants.sql](../../ops/memory-worker-grants.sql)；数据库名不是 `financeclaw_java` 时先调整脚本中的数据库名。Worker 仅有 `memory` schema 的读写权限，没有 `business` / `agent_runtime` / `channel` 的读取和执行权限。启动时检查数据库角色，权限过大的账号会被拒绝。

```bash
scripts/mvn.sh -B -ntp package -DskipTests
export FINANCECLAW_MEMORY_DATABASE_URL=jdbc:postgresql://localhost:5432/financeclaw_java
export FINANCECLAW_MEMORY_DATABASE_USER=financeclaw_memory
# FINANCECLAW_MEMORY_DATABASE_PASSWORD 单独注入
scripts/memory-worker.sh
```

Worker 由同一 Boot jar 的独立入口启动，关闭 Web 和 Flyway，只扫描记忆 Mapper，不装配 TaskService、业务工具或飞书。API 进程不会同时运行记忆轮询。未配置提取模型时，精确 `/remember` 指令仍可处理；自然语言提取保持 `extraction_failed`，不会伪造成功。索引需要配置 embedding。

### pgvector 与模型版本

V5 使用 `real[1024]` 约束的可移植向量存储，实际为 `real[]` 加 `array_length=1024` 检查。需要语义搜索时，在安装了 pgvector **0.8.1** 的 PostgreSQL 上执行 [memory-vector-index.sql](../../ops/memory-vector-index.sql)，增加 `vector(1024)` 的 HNSW cosine 索引，然后设置 `FINANCECLAW_MEMORY_VECTOR_SEARCH=true`。普通 PostgreSQL 仍可运行事实治理和有限偏好召回。

模型名、地址和维度形成配置指纹。相同 `FINANCECLAW_EMBEDDING_REVISION` 下改模型/地址会被拒绝；更换模型必须使用新 revision，调用重建 API。不同模型版本的向量分开存储，旧 Worker 不会覆盖新版本投影。搜索连接事实表和 owner epoch，并核对 fact revision、模型 revision 和状态；不把向量表当事实源。

### 记忆 API

均使用现有 Bearer 身份。写操作带 `Idempotency-Key`；示例中的 ID、revision、epoch 从读取结果取得。

| 方法与路径 | 作用 |
| --- | --- |
| `GET /v1/memory` | 当前主体的有效事实和待确认候选，最多 100 条 |
| `GET /v1/memory/privacy` | 当前 epoch 与 purge_pending |
| `POST /v1/memory/{id}/decision` | `{"revision":1,"accept":true}`，确认/拒绝原候选 |
| `POST /v1/memory/forget` | `{"epoch":1}`，遗忘本主体全部长期记忆 |
| `POST /v1/memory/rebuild` | 为有效事实安排当前 embedding revision 的索引任务 |
| `GET /v1/memory/search?query=...` | 可选 pgvector 搜索；默认关闭 |

遗忘事务提升 epoch，清除记忆来源正文、候选/事实正文、引用、所有模型版本的索引，并废止旧 job。保留无正文 tombstone、审计和幂等回执。`purge_pending` 在 Worker 确认清理后解除；解除前新 Turn 受理返回 409，可稍后用原幂等 key 重试；迟到结果不能恢复内容。旧 Turn 的模型/工具调度被 `memory_privacy_changed` 阻断，跨 epoch 历史不会加入新请求；可取消旧任务并创建新任务。

此 API 清理的是**长期记忆域**。业务 Journal、任务结果和 Artifact 按业务留存策略管理，不隐式删除；SDK 旧快照不作为新 epoch 的可恢复输入。备份恢复必须保留最新遗忘事实，历史数据导入和完整保留策略仍属于 M5。

## 原生飞书接入

设置 `FINANCECLAW_FEISHU_ENABLED=true`，配置 `FINANCECLAW_FEISHU_APP_ID`、`FINANCECLAW_FEISHU_APP_SECRET`、`FINANCECLAW_FEISHU_VERIFICATION_TOKEN`、`FINANCECLAW_FEISHU_ENCRYPT_KEY`、`FINANCECLAW_FEISHU_TENANT_KEY`。生产 API 地址默认 `https://open.feishu.cn`；仅本地测试允许 loopback HTTP。

飞书事件订阅地址为 `POST /api/channels/feishu/financeclaw/callback`。启用机器人和 `im.message.receive_v1` 事件，并授予文本消息发送权限。回调要求签名、五分钟时间窗口、verification token、应用 ID 和租户一致；解密、验签算法、消息结构解析使用原生 SDK。当前只接入真人私聊，拒绝群聊和机器人消息。

通过已登录的业务 API 生成一次性绑定码：

```bash
curl --fail -X POST \
  -H "Authorization: Bearer $FINANCECLAW_API_TOKEN" \
  "http://localhost:8080/v1/conversations/$CONVERSATION_ID/feishu-link"
```

将返回的 `/bind ...` 命令发给飞书机器人。绑定码五分钟有效、仅可用一次；SQL 只保存 hash。后续普通文本受理为已有会话下的新 Turn；事件重复不会重复创建任务。

- 单字段文本澄清：直接回复答案，恢复原 Interaction。
- 审批：通知提供 `/approve <校验码>` 或 `/reject <校验码>`；校验码绑定当前会话绑定版本、Turn revision、问题版本和 action hash。旧任务、旧问题和切换后的指令不能执行新动作。
- `/cancel` 取消当前任务；`/new` 切换新会话；`/mute` / `/unmute` 控制该绑定的通知。
- 等待交互、暂停及终态通过原生文本发送。模型中间文本、工具参数/原始结果和异常不投影到飞书；复杂结构化输入继续走已有业务交互 API。
- `GET /v1/feishu/deliveries` 查看本主体的通知状态，不返回通知正文。

### 固定版 SDK 的明确限制

`FeishuOutboundClient.send()` 返回 `Mono<Void>`；HTTP 200 内的非零业务码仅记录日志，并不抛错，也不返回 message_id 或可配置的发送幂等 UUID。因此本项目将正常返回记为 **submitted_unverified**，异常或进程失联记为 **unknown**。二者都不会自动重发，也不冒充“送达”。通知按原 Turn/revision 唯一保存；发送前核对当前绑定、静音和终态，已发出的网络请求不能撤回。

SDK 2.0.3 没有 CardKit、卡片更新、卡片表单、消息查询回执接口。按用户要求，**这些能力保持未实现**，不另写一套飞书协议客户端。原计划中的流式卡片、表单回调、可靠回执核对，须待框架支持后再接入；现有 SSE 继续提供实时进度。SDK 的默认回调只支持 webhook，此阶段未引入自建长连接。

源码依据：[AgentScope 2.0.3 飞书扩展](https://github.com/agentscope-ai/agentscope-java/tree/v2.0.3/agentscope-extensions/agentscope-extensions-channel/agentscope-extensions-channel-feishu)。实际以下载的固定版本 Maven source jar 和运行测试核对。

## 验证与排障

```bash
scripts/mvn.sh -B -ntp clean verify spotless:check
python3 scripts/collect-evidence.py
```

结果汇总：`target/m4/results.json`；归档见 [M4 机器证据](evidence/m4-results.json)。`target/m4/environment.json` 保存实际 PostgreSQL/pgvector 版本；`target/m4/process.json` 保存独立受限 Worker 的进程证据。

2026-10-15 最终完整回归 **139 项通过**：35 项单元测试、104 项集成测试，0 失败、0 错误、0 跳过；`spotless:check` 通过。M4 新增 28 项（记忆原生适配 3、记忆治理/数据库 14、飞书 11），M0–M3 的 111 项继续通过。证据记录 109 个构建、源码、配置及脚本文件的 SHA-256；基线提交为 `abfb808`，该记录采集后已提交为 `1508802`。

M4 实际数据库为 PostgreSQL 16.12 + pgvector 0.8.1（镜像 `pgvector/pgvector:0.8.1-pg16`），独立 Memory Worker 使用 `financeclaw_memory` 角色启动并生成一条索引投影。M0–M3 延用其原有 PostgreSQL 测试镜像。

- 记忆：完成事务回滚、可信来源、候选确认、隔离/冻结、处理乱序、租约交接、遗忘与提取/embedding 竞争、索引独立重试/重建、实际 pgvector 查询、独立进程与受限数据库权限。
- 原生模型：loopback HTTP 验证 SDK 结构化提取和 1024 维 embedding 的实际请求；未调用真实供应商。
- 飞书：原生加密验签与文本收发、绑定/去重、并发领取、澄清/审批/取消、静音/切换、旧指令、未知发送状态与不重复发送；未连接真实飞书租户。
- 全量 M0–M4 回归结果以机器证据为准；不把本地模拟验收写为真实租户验收。卡片与可靠送达仍是上述明确的框架能力缺口。

排障：`extraction_failed` 检查独立提取模型配置及来源预算；`index_failed` 检查 1024 维响应、模型 revision 与网络；`memory_privacy_changed` 创建新任务；Worker 权限检查失败时使用专用角色；`identity_not_bound` 重新取得绑定码；`stale_approval` 使用当前通知的命令。通知 `unknown` / `submitted_unverified` 需要人工核查，不能通过修改数据库状态后重发来推断成功。
