# Agent 定义、发布与调用

本文描述当前 Agent 管理与调用入口。已交付基线 `ad0b3cacc873134acc80e26ba252b44ef954cde5` 的 [Actions 40361051811](https://github.com/westbrook0904/FinanceClaw/actions/runs/40361051811) 通过 2,110 Java、53 脚本和 45 Compose 检查；该结果不覆盖此后进行的文档或代码改动，也不代表真实外部服务或生产容量验收。

历史 P2a 于 2026-10-23 完成本地全量测试和隔离 Compose 验收，已推送 `851446f`：1,516 Java、44 脚本、36 Compose，见[P2a 原验证记录](evidence/p2a-implementation.json)。当时只开放提示词草稿与操作员 CLI；后续能力组装、多主体目录、管理 HTTP 与显式共享另行交付，当前状态见[平台化实施方案](platform-implementation-plan.md#7-实施状态与验证记录)。

## 当前可以配置什么

操作员可通过 CLI，认证主体可通过有显式管理权限的 HTTP 创建独立 Agent、修改名称与提示词、选择平台已允许的能力，并发布到该 Agent 自己的默认指针。每个 Agent 有稳定 UUID 和明确 owner；草稿更新不会进入执行。发布后，新会话绑定 Agent 与发布 hash，旧会话、后续 Turn 和审批恢复继续使用既有绑定。

自定义 Agent 创建时固定内置默认发布作为 `baseReleaseHash`，未指定 composition 时继承这份发布中的受控能力集及预算、权限约束。改变基础环境默认版本不会悄悄改变已创建 Agent 的基础能力。当前可以通过 composition 选择已有模型 profile、Function、MCP、Skill 和符合条件的子 Agent；能力来源仍受固定基础发布、目录和当前资源授权约束，见[能力组装](agent-composition.md)。没有切换基础版本、任意上传 Java Function、提交模型 URL 或密钥值的管理入口。自定义 Agent 不因输入包含 `/workflow` 前缀就自动切换内置业务 Workflow 驱动。

`finance_agent` 是保留的内置只读 Agent，草稿 revision 为 `0`；可读取元数据和历史，不可经 `agent-admin` 创建、编辑或发布。它的默认版本仍由 P1 的 `release-admin` 管理。

## 管理权限与运行身份

`agent-admin` 是本机可信 operator 入口，要求可访问程序和数据库连接配置。`--tenant`、`--subject` 由 operator 指定目标 owner；它们是资源归属信息，不是对普通请求身份的认证方式。CLI 的进程访问权不等于 HTTP 管理权限。

HTTP 使用服务端多主体目录，将每个 Bearer token 固定映射到 tenant/subject，并显式配置角色。兼容旧单 token，但普通 token 不自动获得管理权。`agent_manager` 授予 `agent:manage`：可管理本 owner 的创建、草稿、发布、归档、历史和能力目录；`agent_sharer` 授予 `agent:share`：可维护本 owner Agent 的精确发布共享授权。角色不绕过资源归属，正文不能指定资源 owner。详细 HTTP 路径、snake_case 正文与 CAS 参数见[身份与资源授权](resource-authorization.md#http-与-cas)。

Agent 归档通过 `DELETE /v1/agents/{id}?expectedDraftRevision=N` 做 CAS，保留历史并阻断后续调用；CLI 没有归档命令。共享调用始终以 caller 身份创建会话、Turn、记忆和产物，发布 owner 仅用于验证发布身份；共享授权不自动授予模型或 MCP 凭据。

独立 CLI 装配 Agent、发布目录及所需能力/资源授权依赖，不启动 HTTP、执行 Worker、业务集成、Flyway 或 SQL 初始化，不解析环境中的发布候选。数据库连接沿用 `FINANCECLAW_DATABASE_URL`、`FINANCECLAW_DATABASE_USER`、`FINANCECLAW_DATABASE_PASSWORD` 或既有 configtree 配置；业务应用角色当前仍拥有相关目录写权限，这不是完整的控制面数据库管理员隔离方案。

## 准备与命令入口

先按[数据库运行说明](fresh-schema.md)完成初始化或前向迁移，并用 P1 的 `release-admin prepare` 初始化内置发布目录。`agent-admin` 不会自动迁移或 bootstrap 内置发布。已有目录可用时，即使当前模型环境候选无效，读取和发布已有 Agent 也不依赖这份候选。

本机打包入口（先按 README 安装仓库锁定的 Temurin 21.0.12+8，示例显式选择该 JDK）：

```bash
scripts/mvn.sh -B -ntp clean package -DskipTests
agent_java="$PWD/.tools/jdk/bin/java"
if [ -x "$PWD/.tools/jdk/Contents/Home/bin/java" ]; then
  agent_java="$PWD/.tools/jdk/Contents/Home/bin/java"
fi
"$agent_java" -Dloader.main=com.financeclawagent.AgentAdministrationApplication \
  -cp target/financeclaw-java-0.1.0-SNAPSHOT.jar \
  org.springframework.boot.loader.launch.PropertiesLauncher \
  list --tenant local --subject developer
```

容器通过既有入口选择 `agent-admin`。以下示例将当前目录中的 `agent-definition.json` 只读挂载到一次性容器；数据库与应用镜像应已准备好：

```bash
docker compose run --rm --no-deps \
  -v "$PWD/agent-definition.json:/input/agent-definition.json:ro" \
  api agent-admin create \
  --agent-id 19a37cb0-fd86-4b59-8bdb-405150f32fe0 \
  --tenant local --subject developer --file /input/agent-definition.json
```

命令行只接受下表参数，不接受额外 Spring 参数、重复参数、未知选项或省略必填字段。UUID 必须使用小写标准格式；owner 不得为空、包含控制字符或超过 512 字符。

| 命令 | 参数 | 作用 |
| --- | --- | --- |
| `create` | `--agent-id UUID --tenant T --subject S --file PATH` | 创建 revision 1，并固定当时内置默认基础发布 |
| `update` | 上述参数及 `--expected-revision N` | 只有草稿 revision 等于 N 才追加下一修订 |
| `get` | `--agent-id ID --tenant T --subject S` | 返回最新草稿的安全元数据与内容摘要 |
| `catalog` | `--agent-id ID --tenant T --subject S` | 返回该 Agent 基础发布下可选择的能力目录 |
| `list` | `--tenant T --subject S` | 返回内置 Agent 和该 owner 的 Agent ID、草稿 revision |
| `publish` | `--agent-id UUID --tenant T --subject S --draft-revision N --expected-revision D` | 固定草稿 N，并以部署指针 revision D 做 CAS 发布 |
| `history` | `--agent-id ID --tenant T --subject S [--after-revision N] [--limit L]` | 分页读取该 Agent 的发布变更；默认 after=0、limit=20，limit 范围 1–100 |

`update` 的预期 revision 从 1 开始；`publish` 的草稿 revision 从 1 开始，部署指针第一次发布必须使用 `--expected-revision 0`，后续使用已确认的指针 revision。不同 Agent 的指针 revision 独立，内置应用的 `default` 不会被自定义 Agent 发布覆盖。

## 定义文件与内容确认

CLI 定义文件必须是 UTF-8 对象，包含两个必填字符串字段 `displayName`、`systemPrompt`，另可包含一个 `composition` 对象。下面是沿用基础能力的最小定义：

```json
{
  "displayName": "财务资料整理助手",
  "systemPrompt": "根据用户提供的资料整理要点，明确区分原文事实与推断。只能使用本次任务允许的工具。"
}
```

`composition` 的 CLI 形式为 `{"modelProfile":"primary","capabilities":["function:market_snapshot"]}`；字段大小写与 HTTP 的 `model_profile` 不同。省略 composition 创建继承基础发布的 Agent；CLI `update` 省略时保留原 composition。CLI 不接受 `composition:null`，需要恢复继承语义时使用 HTTP 完整草稿 PUT 的 null/省略规则。空 capabilities 明确选择零能力，不等于继承。

`displayName` 非空白，最长 200 字符；`systemPrompt` 非空白，最长 16 KiB UTF-8；文件最长 128 KiB。拒绝重复键、尾随 JSON、未知字段、非法 UTF-8、必填字段的非字符串值及非法 composition。CLI 保留字段中的原始空格和换行，不 trim 内容，也不会更改用户文件权限。

16 KiB 是定义存储上限，不代表所有提示词都适合当前运行预算。执行仍受 `FINANCECLAW_CONTEXT_SYSTEM_BYTES` 等上下文限制约束；默认 system 预算为 8 KiB，实际还需容纳运行系统内容。发布成功不绕过预算，operator 应用实际任务验证可运行性。

创建、更新和读取的回执返回 `agentId`、`draftRevision`、`baseReleaseHash`、`contentHash`；创建/更新另有 `inputSha256`。`inputSha256` 是所读取文件原始字节的 SHA-256，可用 `shasum -a 256 agent-definition.json` 核对。`contentHash` 是名称、提示词及存在时的 composition 组成规范 JSON 后的 SHA-256，因此只调整 JSON 排版会改变文件摘要，不会改变内容摘要。

CLI 回执不打印 displayName、systemPrompt、tenant、subject、完整 manifest、数据库连接或凭据；HTTP 草稿管理读取会返回获授权的草稿内容，不能套用 CLI 的摘要输出约定。发布回执仅返回 Agent ID、发布 hash 和指针 revision；历史项仅返回 revision、releaseHash、previousHash。Agent UUID 是可输出的资源 ID，不能单独作为访问授权。

## 更新、发布与超时处理

1. 创建时指定一个新的 UUID。相同 owner、UUID 和最初名称/提示词/composition 的重放返回原始 revision 1 与原基础版本；不能借重放更新内容。内容不同返回 `agent_id_conflict`。
2. `get` 获取当前草稿 revision 与摘要，使用本地受控文件做 `update --expected-revision N`。名称与提示词不从回执恢复，operator 应保留其原始文件。
3. `history` 读取最新发布回执；未发布时使用部署 revision 0，随后用 `publish --draft-revision N --expected-revision D` 发布。发布在一个事务中写入不可变发布、切换指针并追加历史。
4. 创建新会话使用新发布。已有会话不原地更换 Agent 或版本；需要新发布时建立新会话。

```bash
docker compose run --rm --no-deps api agent-admin publish \
  --agent-id 19a37cb0-fd86-4b59-8bdb-405150f32fe0 \
  --tenant local --subject developer --draft-revision 1 --expected-revision 0

docker compose run --rm --no-deps api agent-admin history \
  --agent-id 19a37cb0-fd86-4b59-8bdb-405150f32fe0 \
  --tenant local --subject developer --after-revision 0 --limit 20
```

历史下一页传响应 `nextAfterRevision`，直到 `items` 为空。命令超时不能判断事务是否提交：创建可以用同一 UUID 和原文件重放；更新先 `get` 比对 revision 与 contentHash；发布先读 history 核对 hash/revision。不要读取新的 revision 后盲目重试，避免覆盖另一位 operator 已发布的版本。

当前 `agent-admin` 没有任意 hash 激活或历史草稿发布命令。需要恢复旧提示词时，使用保留的原文件创建新的草稿修订，再按显式 CAS 发布。基础能力版本仍是 Agent 首次创建时固定的版本。

## 从普通对话 API 选择 Agent

调用方通过现有 Bearer 身份使用只读目录和会话入口：

```http
GET /v1/agents
Authorization: Bearer <业务调用 token>
```

响应为数组，每项包含 `agent_id`、`draft_revision`、`builtin`，仅包含内置 Agent 与当前 owner 未归档 Agent 的元数据，不包含提示词或完整发布。列表中存在草稿不代表已可调用，本人的未发布目标创建会话会返回 `agent_not_published`。另用 `GET /v1/agents/shared` 读取当前 caller 获授权的共享 Agent、精确 `release_hash` 和 `grant_revision`；这不授予该资源的草稿或管理访问权。

```http
POST /v1/conversations
Authorization: Bearer <业务调用 token>
Content-Type: application/json

{"agent_id":"19a37cb0-fd86-4b59-8bdb-405150f32fe0"}
```

空对象 `{}` 保留内置 `finance_agent` 行为。响应仍为 `conversation_id`，随后使用原有 Turns API 提交任务；每次提交不再传 Agent 或发布 hash。API 从可信认证上下文取得 caller；本人已发布目标或当前精确发布已获共享授权的外部目标可以创建会话。未获授权、已归档或外部未发布目标不会暴露对象详情；未知字段被拒绝。普通会话入口选择目标的当前部署版本：共享目录里仅有旧版本授权，不代表当前版本也可调用。已绑定的旧会话保持原发布，但继续受当前撤销/归档门禁约束。HTTP 目录读取与创建目标会话不授予创建/编辑/发布 Agent 的管理权。

## 常见结果与验证范围

成功退出码为 `0`；失败为 `2`，输出一行 `{"result":"blocked","error":"..."}`。常见错误如下：

| 错误 | 处理 |
| --- | --- |
| `invalid_agent_admin_arguments` | 修正缺失、重复、未知选项或非法 UUID/revision |
| `invalid_agent_definition` | 检查文件可读性、UTF-8、必填字段、可选 composition 结构及字节上限 |
| `not_found` | 检查 Agent ID 与 operator 指定 owner；跨 owner 不返回对象详情 |
| `agent_read_only` | 内置 `finance_agent` 不通过此入口修改 |
| `agent_draft_revision_changed` | 重新读取草稿并核对内容，决定是否提交新的修改 |
| `release_revision_changed` | 读取该 Agent 的历史，核对实际指针，避免盲重试 |
| `release_unavailable` | 检查持久发布是否存在；不能伪造缺失的历史清单 |
| `agent_admin_failed` | 未知故障被脱敏；检查独立数据库连通性与 schema 状态，不从输出获取原始异常 |

实现入口：[CLI](../../src/main/java/com/financeclawagent/AgentAdministrationApplication.java)、[AgentService](../../src/main/java/com/financeclaw/app/agent/AgentService.java)、[容器入口](../../ops/container-entrypoint.sh)。验证入口：[参数/文件与安全输出单测](../../src/test/java/com/financeclawagent/AgentAdministrationApplicationTest.java)、[真实打包 CLI 集成测试](../../src/test/java/com/financeclawagent/AgentAdministrationCliIT.java)。后者由 Maven `verify` 在 `package` 之后运行，需要隔离 PostgreSQL 容器；上述 CLI 用例已纳入文首交付基线的完整 CI，使用离线模型与隔离数据库，不代表真实外部服务验收；P2a 原批次数字仍单独保留。

创建后的 Agent 还可作为本人或显式共享的受控叶子目标。原[空工具契约](private-agent-delegation.md)和[固定行情叶子契约](private-agent-market-leaf.md)保留历史兼容；当前还支持选定 Function、只读 MCP、打包 Skill，以及 `request_city` / `create_report` 的精确交互桥接，见[当前受控叶子语义](non-rag-completion-plan.md#p3a-受控叶子与结果读取语义)。父发布固定目标版本，目标草稿更新不会改变已有会话或父绑定；执行按根 caller 复核当前目标授权，深度仍限一层。
