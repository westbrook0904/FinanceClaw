# 运行维护

当前支持新库初始化及现行 Java V1 基线的 V2–V15 前向升级，主库共 **52 张应用表、15 个迁移**，独立遗忘账本不计入主库。现有迁移保持校验，新版本由 `init-db` 前向升级；不导入 Python 或历史实验库，不支持任意新旧二进制混跑，操作见[数据库初始化与升级](architecture/fresh-schema.md)。交付基线 `ad0b3ca` 的 [Actions 40361051811](https://github.com/westbrook0904/FinanceClaw/actions/runs/40361051811) 已通过 2,110 Java、53 脚本与 45 Compose 检查，真实外部服务及生产容量另行验收；该结果不覆盖此后的文档或代码改动。自定义 Agent 的 CLI 见[Agent 管理说明](architecture/agent-definitions.md)，当前 HTTP 与多主体配置见[身份与资源授权](architecture/resource-authorization.md)。

## 配置与启动

先执行 `scripts/init-local.py` 创建本地凭据，配置非密钥参数后启动 Compose。`init-db` 只持有结构所有者身份，业务进程使用 `financeclaw_app`，Memory Worker 使用 `financeclaw_memory`。结构所有者凭据不挂载到运行进程。

```bash
docker compose up -d --build --wait --wait-timeout 180
docker compose ps
curl --fail http://localhost:8080/actuator/health/readiness
```

就绪检查包含必要存储和 worker 本地注册状态，不证明模型、供应商或业务任务已完成。启用 worker 时，首次注册、数据库暂挂、注册过期和关闭均不可就绪；显式禁用 worker 的受理/管理入口仍可就绪。默认 worker TTL 为 15 秒：截止前控制重试成功可恢复新领取，旧 attempt 不复活；超过截止或注册已 stopped 时，数据库恢复也不会复活旧 UUID，须启动新进程身份。重启前后都不能用新幂等身份重发未知写入，未封存效果保持阻断。详细边界和验证过的 15 秒/显式 60 秒实验见[运行协调](architecture/runtime-coordination.md#worker-注册失联与恢复)。

旧单 token 只提供普通调用权限。管理 HTTP 需在服务端 `financeclaw.auth.identities-json` 中配置对应角色；真实 token 通过 configtree 文件提供。Agent 共享与凭据资源授权分别管理，不能通过提交 owner 参数提升身份。目录不是热更新接口，修改部署配置后需启动使用新配置的进程。

## 受理与排空

`PUT /v1/operations/admission` 接受 `paused`、预期 `revision` 与 `Idempotency-Key`。暂停只阻止当前主体的新任务，已有审批、回答、取消和恢复仍然可用。

`GET /v1/operations/drain` 返回当前主体积压计数；`/drain/items` 按任务、通知、未生成通知的绑定和记忆作业分页。不存在旧 Outbox 或不完整报告字段。独立外部通知的未知/未验证结果仍然是阻塞项。

等待问题全部过期后，不再计入 `notification_bindings` 与 `unprojected_notifications`。原问题仍计入 `pending_interactions`，任务仍为 waiting，因此即使已暂停受理也不会被报告为 drained；通知队列为空不能代替任务排空。范围见[通知候选查询](architecture/projection-discovery-queries.md)。

```bash
python3 scripts/inspect-drain.py --help
```

预检脚本只读 HTTP，核对前后观测与明细数量，分别报告阻塞、检查不完整或可供复核；它不改变受理状态，不自动停止服务。

## 画像与记忆

`GET /v1/memory/profile.md` 返回当前认证主体的 Markdown 和 ETag。编辑后用 `PUT`、`Content-Type: text/markdown; charset=utf-8` 和原始 `If-Match` 保存。不能跨主体、跨遗忘 epoch 或覆盖别人的并发编辑。

自动证据提取默认关闭；手工画像默认启用，两者是不同配置。画像只能由认证 API 或隔离隐私恢复入口处理，受限 Worker 不具备画像表读取权限。

P3a.2 已将普通遗忘和独立账本重放接入同一执行副本清理并完成本地验收。清理先覆盖根／研究子 Agent 全部历史 attempt 的 SDK 正文并保留 CAS 墓碑，再删除旧步骤、子调用和隐私绑定工具文件；普通业务 Turn、Journal、报告及非隐私绑定 Artifact 保留。未知且有正文的状态会阻断清理，不能通过仅更新 epoch 解除门禁，详见[执行数据隐私清理](architecture/execution-privacy.md)。

## 备份和隐私恢复

当前应用的正常备份恢复继续保留。恢复旧主库备份可能遗漏其后发生的遗忘，必须在隔离库中与独立遗忘事实或最新权威快照核对，不能只比较 epoch，也不能先启动业务进程再补清理。

```bash
# 停止本应用的写入者；不会自动停止其他项目或手工进程
docker compose stop api memory-worker

python3 scripts/check-restore-privacy.py --help
python3 scripts/user-profile.py --help
```

隐私快照由 [SQL](../ops/restore-privacy-snapshot.sql) 导出，不包含记忆、SDK、步骤或工具文件正文。当前协议为 **Snapshot v3**，强制要求每个 owner 的 `unsafe_sdk_states`、`unsafe_capability_steps`、`unsafe_child_invocations`、`unsafe_tool_artifacts`，以及顶层 `orphan_runtime_rows`。任何残留都阻断恢复；v1/v2、缺字段或不支持的结构返回 `incomplete`，不能在旧 JSON 中补零。先在隔离环境应用 V7，再从最新权威库与恢复库分别重导。

独立遗忘账本由[账本 SQL](../src/main/resources/privacy-ledger/V1__forget_ledger.sql)单独创建，启用前使用 `privacy-recovery` 的显式隔离 `bootstrap`；恢复库使用 `replay` 重放，再重新检查快照。`unsafeRows` 包含执行副本残留，epoch 与账本一致本身不能证明清理完成。需要未知 live 状态修复时保持隔离，不自行推测归属或删除其他用户资料。

恢复顺序为：保持全部写入者停止 → 升级隔离库结构 → 核对或重放最新独立遗忘事实 → 用当前 SQL 导出双方 v3 快照 → 运行比较器 → 继续业务完整性、发布身份和渠道唯一消费者检查。快照相符不等于已批准启动，参考事实是否最新仍须由操作者保证。

保留的恢复边界与完整参数示例见历史操作记录中的[独立遗忘账本](history/m5f-forget-ledger.md)和[恢复隐私核对](history/m5e-restore-privacy.md)。它们记录过去验收批次，当前表结构和入口以本文件、新库初始化及代码为准。新的初始化程序不会修改或清空任何已有实验库。

## 发布版本管理

P1 使用 PostgreSQL 保存不可变发布清单和 `default` 指针。启动配置只登记为候选；指针不存在时首次初始化，已有指针不会随重启或环境变量变化而切换。新会话创建时固定版本，已存在会话与等待审批的 Turn 不随发布切换。

发布命令是隔离的一次性 JVM，沿用业务数据库连接与发布配置，不启动 HTTP、Worker 或 Flyway。先完成[数据库升级](architecture/fresh-schema.md)，再使用当前镜像：

```bash
# 使用 api 服务的环境/configtree 登记候选，返回 candidateHash 和 current(releaseHash,revision)。
docker compose run --rm --no-deps api release-admin prepare

# 替换为上一步实际返回的候选 hash 和 current.revision；不要猜测 revision。
RELEASE_HASH=替换为64位候选hash
RELEASE_REVISION=替换为当前revision
docker compose run --rm --no-deps api release-admin activate \
  --hash "$RELEASE_HASH" --expected-revision "$RELEASE_REVISION"

docker compose run --rm --no-deps api release-admin history --after-revision 0 --limit 20
```

成功退出码为 0；参数、版本冲突或不可用清单等失败返回 2 和不含凭据的 JSON。`release_revision_changed` 表示预期 revision 不匹配，也可能是默认指针尚未初始化。已有目录应先查询历史并复核最新 revision，再作出新的发布决定；环境配置有效时也可用 `prepare` 读取 current。回滚使用 `activate` 指定已登记的旧 hash 和预期 revision，生成新的变更历史，不修改旧清单。请求超时后先核对历史回执，不能自动换成新 revision 重试。

`history` 返回 `nextAfterRevision`，将其传入下一页的 `--after-revision`，直到 items 为空；默认 limit 为 20，允许 1–100。`history` 只读取已存历史；`activate` 只解析指定的已登记清单，两者均不解析当前环境候选，因此候选配置有误时仍可查询和回滚。运维命令中只有 `prepare` 登记环境候选，并在默认指针缺失时执行首次 bootstrap。输出只包含标识和回执，不打印配置正文。

候选登记与激活是两个步骤：已有指针时，`prepare` 可以留下尚未激活的清单。`activate` 的指针更新与历史追加在同一事务内完成，CAS 冲突不会产生切换历史，也不会删除候选。首次 bootstrap 才将清单登记、缺失指针和首条历史共同提交。

显式 `agent-release/3` 冻结生产 class 工件、所选 Function／Skill 和 SDK 有效 MCP 契约；旧格式没有这份完整快照，不能事后补造。运行仍限定可兼容的 AgentScope 2.0.3 工件，历史 hash 缺少清单或工件不匹配时保持阻断。凭据继续从运行环境解析，发布不恢复已撤销权限；完整边界见[能力组装](architecture/agent-composition.md)。

历史 P3a.3 已交付同 owner 空工具叶子委派，`d433423` 远程 CI 成功。operator 先以显式 `composition.capabilities: []` 发布叶子，再在父定义中选择 `agent:<UUID>` 并发布；父自动带上 Artifact reader 支持依赖，子工具集仍为空。父冻结选定目标 release，目标后续发布不自动升级父；采用新目标需要重新发布父并创建新会话。运行工件严格兼容检查保持，不允许换成 current 绕过旧版本阻断。定义示例、输入隔离和预算见[私有叶子 Agent 委派](architecture/private-agent-delegation.md)。该阶段 44 项 Compose 检查验证发布冻结与恢复绑定，SDK 委派由运行时测试验证；分次 Java 结果和部署摘要见[本地证据](architecture/evidence/p3a3-implementation.json)。

当前叶子调用已扩展至精确共享发布、选定 Function/只读 MCP/Skill，以及受控补充输入和报告审批；仍固定父发布选中的子版本，详见[当前受控叶子语义](architecture/non-rag-completion-plan.md#p3a-受控叶子与结果读取语义)。内置默认发布继续使用 `release-admin`；自定义 Agent 同时支持操作员 `agent-admin` 和有显式管理角色的 owner 范围 HTTP，草稿与发布均要求 CAS。尚无完整 Deployment 资源或原地会话升级，采用新版本时创建新会话。共享撤销与归档会阻断旧发布后续使用，不因历史清单仍在而绕过授权。


## 心跳连接资源

业务进程在原 Hikari 总连接预算内预留续租连接，默认总数 8 分为业务 6、心跳 2；持久事件 LISTEN 长期占用业务池中的 1 个槽位，不另开池或借心跳资源。调整总池大小时应同时检查预留配置和数据库连接上限；受限 Memory/管理进程仍使用各自原连接预算。配置、超时与指标见[并发资源运行配置](architecture/concurrent-recovery-resources.md#运行配置)。
