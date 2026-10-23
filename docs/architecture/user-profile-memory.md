# 用户画像与跨会话证据

本文是当前实现说明。记忆分为两个独立写入入口，共用认证主体 `(tenant, subject)`、隐私 epoch 和遗忘恢复协议。

| 层 | 内容与维护方式 | 执行时的用途 |
| --- | --- | --- |
| 用户画像 | 每个用户一个 UTF-8 `profile.md`；用户下载、手工编辑、上传；数据库保存当前权威版本 | 稳定背景、表达偏好、长期目标；人工画像的偏好优先于冲突证据 |
| 跨会话偏好证据 | 真实用户消息作为来源，保存原文片段、事实 revision、确认状态；提取 Worker 只能写这一层 | 召回已确认且来源有效的偏好，供回答参考 |

当前证据层支持语言、回答风格、居住城市、旅行偏好、投资偏好五类 key。同 key 的旧事实会被 supersede；Turn 受理时选取最近最多 16 条 active facts 并冻结正文，后续事实变更只影响新 Turn。它还不是通用经历、事件或任意文档记忆，也没有在每轮执行时做向量搜索。候选不会进入模型上下文。

画像是用户资料，不能改变系统规则、工具授权或审批要求。本次用户明确表达优先于长期偏好。Markdown 与证据按独立段落、带 provenance 的 JSON 数据注入；任意自由文本的语义冲突由模型解释，不能声称是确定性的字段合并。提取、确认事实和向量重建都不会覆盖人工画像。

## 下载与手动维护

`FINANCECLAW_PROFILE_ENABLED=true` 默认开启手工画像；`FINANCECLAW_MEMORY_ENABLED` 仍控制自动证据提取，默认关闭。二者独立，画像不需要 embedding 或额外模型配置。

先启动业务服务，并通过环境注入 `FINANCECLAW_API_TOKEN`。命令行工具仅将凭据用于 HTTP 请求，不写入本地文件或命令参数：

```bash
mkdir -p /tmp/financeclaw-profile
chmod 700 /tmp/financeclaw-profile
python3 scripts/user-profile.py pull --file /tmp/financeclaw-profile/profile.md

# 使用你习惯的编辑器打开 /tmp/financeclaw-profile/profile.md
# 保存后显式上传
python3 scripts/user-profile.py push --file /tmp/financeclaw-profile/profile.md
```

首次下载为空文档。可以填写如下内容，只保留愿意让模型使用的资料：

```markdown
# 用户画像

## 背景
- 工作方向：Java 后端与金融系统。
- 常驻城市：宁波。

## 沟通偏好
- 默认使用中文，先给结论，再说明关键理由。
- 技术建议请区分已验证的结果和待验证的假设。

## 长期目标
- 提升后端系统设计和企业 AI 应用能力。
```

正文最大 **8192 个 UTF-8 字节**，不静默裁剪；中文通常占多个字节。空文档可以上传，用于清空当前画像；相同正文保存不增加 revision。若要求旧备份也不能复活资料，应使用受遗忘账本保护的 `forget`，单纯编辑文档不产生独立遗忘事实。服务端不执行 Markdown、链接、脚本，也不支持正文指定 owner 或文件路径。

CLI 默认服务地址为 `http://localhost:8080`；其他地址用 `--url`，远程只接受 HTTPS，不跟随重定向。文件旁的 `.profile.md.version.json` 保存服务地址和版本，不是第二份画像，也没有 token。两份文件初建权限均为 `0600`。本地文件不被服务端监听，只有成功上传才会生效。

下载默认拒绝覆盖已有文件。遇到 `http_409`（版本冲突）时，先另存本地编辑，用另一个 `--file` 下载最新版本，人工合并后再上传。`pull --force` 会显式覆盖本地内容。不要通过删除版本文件或自行替换 epoch 绕过冲突。上传结果不确定时先下载核对，不盲目重试覆盖。

## HTTP 契约

| 请求 | 响应与要求 |
| --- | --- |
| `GET /v1/memory/profile.md` | 原始 UTF-8 Markdown、强 ETag、`Cache-Control: no-store`、附件名 `profile.md` |
| `PUT /v1/memory/profile.md` | `Content-Type: text/markdown; charset=UTF-8`；正文是文件原始字节；必须带 GET 返回的 `If-Match`；返回保存后的正文与 ETag |
| `GET /v1/memory/privacy` | 当前 epoch、purge 状态、profile revision；任一记忆层启用即可查询 |
| `POST /v1/memory/forget` | 沿用 epoch 与 Idempotency-Key；同时遗忘画像和跨会话证据 |

ETag 绑定认证 owner、epoch 和 revision。另一个用户的导出版本不能上传到当前用户；请求不接受自行声明的 tenant/subject。缺失 `If-Match` 返回 428，非法版本或非法 UTF-8 返回 422，正文超限返回 413，旧 epoch/revision 返回 409。遗忘未完成期间读写返回 `purge_pending`。错误响应不回显正文。

## 执行、并发和查询成本

当前[新库初始化 SQL](../../src/main/resources/db/migration/V1__initialize_application.sql)创建 `memory.user_profiles` 保存当前人工画像；`memory.turn_snapshots` 保存每个 Turn 受理时已经格式化的长期记忆正文。快照包含 owner、epoch 和画像 revision，插入后不能被后续画像或证据替换。自动证据关闭时 source 不保存可供提取的正文，不产生提取作业；两层均关闭时仍保存空快照和隐私绑定。开关变化只影响新 Turn 的采样与编辑入口，已开始 Turn 沿用原快照。

受理对已存在的 owner 持 `FOR SHARE` 共享锁，允许同一用户的独立会话并发；画像编辑、遗忘及执行策略检查持 `FOR UPDATE` 排他锁。共享锁持续到受理事务提交，因此变更不能越过快照冻结边界。普通画像编辑不会中断已经排队、运行或等待审批的 Turn；审批恢复和进程重建读取同一快照。遗忘提升 epoch，清理快照与会话工作上下文，并使旧执行失效；授权撤销仍有独立的执行门禁。

AgentScope `onSystemPrompt` 注入 Turn 快照，每个 attempt 只读取一次，不重新召回、不把画像追加到 `AgentState.context`。会话中期工作上下文另行保存问答和工具过程，接近预算时压缩为结构化摘要；每个 Turn 的完整轨迹仍保留。长期快照不能被压缩模型改写，最终主/备模型 HTTP 请求还会经过完整预算检查。详见[上下文管理](context-management.md)。

稳定 owner 的静态 SQL 调用数（不是压测结果）：

| 路径 | 成本 |
| --- | --- |
| Turn 受理 | owner lock + source insert + snapshot insert，共 3 次；非空画像加 1 次读取 |
| 画像与证据均启用的受理 | 在以上基础加 1 次有界事实查询 |
| 执行策略的隐私检查 | owner lock + source，共 2 次；不读取画像正文或重复召回事实 |
| 每 attempt 的长期上下文 | owner lock + source + snapshot，共 3 次，与画像/事实条数无关 |
| 历史过滤 | 一次 owner lock + 一次批量来源查询，不逐 Turn 查询 |
| 每个 SSE 事件 / token | 不查询画像数据库 |

## 遗忘与备份恢复

两条遗忘路径都清除画像正文并推进 epoch：普通事务路径和启用独立遗忘账本的恢复路径。账本只保存遗忘事实，不保存 Markdown。恢复重放只删除遗忘之前的画像，保留后来 epoch 的新画像；同时检查旧/未来 epoch、画像版本不一致和缺失文档等残留。仅开启画像时，遗忘完成同步清理后会自行清除 pending 标记，不依赖记忆 Worker；证据层启用时保留原有 Worker 完成流程。受限 Memory Worker 对画像表无读写权限，不能把提取结果写入人工文档。

画像单独启用时，清除 pending 是遗忘提交后的第二个事务。若进程在两次提交之间崩溃，屏障会保留；使用原 epoch 和原 Idempotency-Key 重试同一遗忘请求，或通过隔离恢复流程完成清理，不应直接修改数据库标记。

当前隐私快照协议为 v2，包含画像正文、epoch 和版本残留检查。备份恢复时保持业务隔离，核对独立遗忘账本、完成重放和残留核验后再开放访问。检查点或任务缺少受理绑定时必须拒绝执行，不能补写当前 epoch 规避隐私边界。

独立应用只支持当前 V1 结构的新库初始化，不执行旧应用数据回填或跨版本升级。每个新 Turn 都在受理事务中写入自己的隐私和画像版本绑定；数据库初始化、隔离恢复的现行步骤见[运行说明](../operations.md)。

服务端遗忘无法删除用户已经下载的本地文件。旧版本文件携带旧 epoch，直接上传会冲突；如果用户重新下载并主动粘贴旧资料，这是新的人工输入。请自行管理本地导出副本。

## 源码与验证入口

- [画像用例](../../src/main/java/com/financeclaw/app/memory/UserProfileService.java)、[Markdown HTTP](../../src/main/java/com/financeclaw/api/UserProfileController.java)、[CLI](../../scripts/user-profile.py)。
- [执行记忆与证据](../../src/main/java/com/financeclaw/app/memory/MemoryService.java)、[AgentScope 注入](../../src/main/java/com/financeclaw/infrastructure/agentscope/execution/TaskAgentRuntime.java)。
- [遗忘恢复](../../src/main/java/com/financeclaw/infrastructure/memory/JdbcForgetRecoveryRepository.java)、[恢复快照](../../ops/restore-privacy-snapshot.sql)。

当前验证入口包括[画像 HTTP 契约](../../src/test/java/com/financeclaw/architecture/UserProfileHttpTest.java)、[上下文与版本边界](../../src/test/java/com/financeclaw/architecture/UserProfileContextTest.java)、[真实 PostgreSQL 画像并发](../../src/test/java/com/financeclaw/m4/UserProfileIT.java)、[画像隐私恢复](../../src/test/java/com/financeclaw/m5/ProfilePrivacyRepositoryIT.java)和[新库结构](../../src/test/java/com/financeclaw/m5/SchemaInitializationIT.java)。这些是可运行的覆盖入口，本说明不据此宣称当前构建已经通过完整 CI。

[最初画像验证记录](evidence/user-profile-verification.json)与[后续结构整理验证记录](evidence/schema-refactor-verification.json)保留各自当时的代码和环境范围，不代表这次 V1 初始化收敛、入口清理及编码规范调整已经完成复验。模拟模型也不能替代真实模型的偏好遵循验收。
