# 身份、Agent 管理与资源授权

已交付基线：`ad0b3ca` 的 [Actions 40361051811](https://github.com/westbrook0904/FinanceClaw/actions/runs/40361051811) 已通过完整 Java、脚本与 Compose 验收；其中包括下述认证、HTTP 管理、共享授权和真实 PostgreSQL 竞争测试。[集中实施记录](non-rag-completion-plan.md)保留此前失败及本地 Docker 阻塞。该结果不覆盖此后的文档或代码改动；这里的服务端目录仍不是外部身份提供方或完整组织 RBAC。

## 可信身份与角色

每个 HTTP Bearer token 由服务端目录固定映射到一个 `tenant` / `subject`。请求正文、请求参数和客户端自报的角色都不能改变这个主体。普通调用权限与管理权限分开；配置旧的 `financeclaw.auth.token` 仍能调用业务接口，但不会自动获得任何管理权限。

`financeclaw.auth.identities-json` 是 JSON 数组，最多 128 项。每项只允许以下四个字段，字段名按配置 JSON 原样书写：

```json
[
  {"token":"REPLACE_WITH_CALLER_TOKEN","tenant":"example","subject":"caller","roles":[]},
  {"token":"REPLACE_WITH_PUBLISHER_TOKEN","tenant":"example","subject":"publisher","roles":["agent_manager","agent_sharer"]}
]
```

这些值仅为占位符。实际 token 应通过部署的 configtree 文件提供 `financeclaw.auth.identities-json` 属性，限制文件访问权限；不要提交包含真实 token 的 JSON，不要通过命令行参数传入秘密。旧单 token 的配置属性为 `financeclaw.auth.token`、`financeclaw.auth.tenant`、`financeclaw.auth.subject`。

| 角色 | 权限 | 范围 |
| --- | --- | --- |
| `agent_manager` | `agent:manage` | 本主体 Agent 草稿、发布、归档、发布历史和能力目录 |
| `agent_sharer` | `agent:share` | 本主体 Agent 的调用授权与授权审计 |
| `credential_manager` | `credentials:manage` | 本主体在服务端凭据目录中登记的资源及授权 |
| `scheduler_manager` | `schedules:manage` | 本主体调度规则、触发与调度策略 |
| `runtime_manager` | `runtime:manage` | 运行协调管理 API |

没有隐式 `admin` 或通配符角色。角色允许进入管理端点，资源 owner 校验仍然独立执行。未知角色、重复 token（包括与旧 token 重复）、重复 JSON 键、多余字段和非法身份会令配置初始化失败。目录实例仅保存 token 的 SHA-256 摘要；错误不会附带含 token 的原始配置。

这是由服务端部署控制的认证目录，不是自助注册、身份提供方或完整组织 RBAC。目录在进程启动时加载，变更后需重新部署/启动使用新配置的进程；没有热更新目录 API。数据库中的资源授权变更在后续授权检查处生效。

## 服务端凭据资源目录

`financeclaw.auth.credential-resources-json` 只能由服务端配置，最多 256 项。每项字段固定为 `resourceId`、`reference`、`tenant`、`subject`：

```json
[
  {"resourceId":"primary-model","reference":"@model","tenant":"example","subject":"publisher"},
  {"resourceId":"search-service","reference":"SEARCH_API_TOKEN","tenant":"example","subject":"publisher"}
]
```

`@model` 代表现有模型 API key 资源；其他 `reference` 必须是现有凭据解析器支持的大写环境变量引用。这里登记引用与所有者，密钥值仍由现有服务端凭据配置提供。HTTP 不能增加引用、提交密钥值或指定任意环境变量名；凭据目录响应只返回资源 ID。

凭据目录和多主体目录都为空时，仅旧认证配置的 owner 可以使用既有凭据，保留单主体部署兼容。启用多主体目录或显式凭据目录后，未登记的凭据引用会被拒绝；调用者必须是资源 owner，或持有当前有效的凭据资源授权。离线模型不需要模型凭据授权；无凭据的 MCP 引用不产生凭据授权要求。Agent 的共享授权不会顺带授予模型或 MCP 凭据。

凭据 grant 保存 `binding_hash`，它是凭据引用字符串的 SHA-256，不包含密钥值。相同资源 ID 被服务端改绑到不同引用后，旧授权不能自动取得新引用；资源 owner 需要按现有授权修订重新授权。原引用下的密钥轮换不会改变该绑定摘要。

## caller 与发布 owner

管理操作只允许访问认证主体自己的 Agent。调用共享 Agent 时，执行 caller 始终是调用者本人：Turn、Conversation、记忆、产物、SDK user/session、Command 和 Attempt 不改成发布者身份。

`AgentAccessGateway.requireInvocation(caller, agentId, releaseHash)` 返回独立的 `resourceOwner`、当前 `grantRevision`，以及该次检查已解析的不可变 `resolvedRelease`（内置目标可为空）。调用链复用该次清单可避免重复解析，但每次使用仍重新检查当前 owner/共享授权，不跨事务缓存授权结论。发布清单用 `resourceOwner` 核对不可变身份；业务数据和执行栅栏继续使用 caller。父 Agent 委派共享子 Agent 时，也使用根 caller 检查子目标，而不借用父发布者或子发布者身份。能力冻结和执行边界由调用链再次校验。

Agent grant 绑定一个具体 `release_hash`。发布者发布新版本，不会自动将旧授权扩大到新版本；已有会话仍固定旧版本，并需要旧版本授权继续有效。当前目标解析使用当前部署指针，因此当前版本没有授权时不能新建普通目标会话。`GET /v1/agents/shared` 返回获授权的精确版本，可用于展示或发布时冻结子目标。

授权通过修订更新为 `enabled=false` 撤销，不删除原事实。后续调用、模型/工具/检查点/结果或 SSE 门禁读取当前授权。撤销不会撤回已经交付的内容，也不声称能回滚已经完成的外部效果。

本地检查点事务中的授权检查对有效 Agent 和授权行取得 `FOR SHARE` 锁，直到本地结果、回执或产物写入一同提交。撤销及归档通过排他更新与其排序，不能在同一事务的授权检查和效果提交之间插入。事务外的网络调用前检查仍使用短读取，不跨网络请求保持这些锁。服务端配置目录的重新加载不属于数据库锁协议。

Agent 删除采用归档：CAS 设置 `agents.archived=true`，隐藏管理目录并拒绝该 Agent 后续调用，包括已有绑定的旧版本。不可变草稿、发布清单、发布历史和授权审计仍保留在数据库中；旧 Agent ID 不能通过重新创建复活。当前管理 GET/history 入口对已归档 Agent 返回未找到。

## HTTP 与 CAS

正文遵循项目现有 snake_case JSON 规则，拒绝未知属性和类型强制转换。`GET /v1/agents` 供普通认证调用者读取目录；下表其他管理端点要求 `agent:manage` 且 owner 匹配。`finance_agent` 内置目标保持只读。

| 请求 | 正文或参数 | 作用 |
| --- | --- | --- |
| `GET /v1/agents` | 无 | 普通调用者的内置目标和本人 Agent 简要目录 |
| `POST /v1/agents` | `agent_id`、`display_name`、`system_prompt`、可选 `composition` | 以客户端指定 UUID 幂等创建；相同 ID 不允许不同初始内容 |
| `GET /v1/agents/{id}` | 无 | 本人当前草稿 |
| `PUT /v1/agents/{id}` | `expected_draft_revision`、完整新草稿字段 | CAS 更新；`composition=null` 或省略时明确恢复继承基础发布语义 |
| `DELETE /v1/agents/{id}` | 查询参数 `expectedDraftRevision` | 按当前草稿修订 CAS 归档 |
| `POST /v1/agents/{id}/publish` | `expected_draft_revision`、`expected_deployment_revision` | 精确草稿与部署指针 CAS 发布；首次部署修订为 0 |
| `GET /v1/agents/{id}/history` | `after`、`limit` | 本人发布历史 |
| `GET /v1/agents/{id}/catalog` | 无 | 草稿基础发布下可选择的能力目录 |

`composition` 的格式为 `{"model_profile":"primary","capabilities":["function:market_snapshot"]}`。管理权限不绕过能力可用性、凭据授权、发布摘要或 CAS 检查。

资源授权端点：

| 请求 | 权限 | 作用 |
| --- | --- | --- |
| `GET /v1/agents/shared` | 普通认证 | 当前 caller 的有效共享目标及精确版本 |
| `GET` / `PUT /v1/agents/{id}/shares` | `agent:share` | 列出或更新本人 Agent 的调用授权 |
| `GET /v1/agents/{id}/shares/history` | `agent:share` | 指定接收主体的授权修订历史 |
| `GET /v1/credential-resources` | `credentials:manage` | 本人登记的凭据资源 ID |
| `GET` / `PUT /v1/credential-resources/{id}/grants` | `credentials:manage` | 列出或更新凭据授权 |
| `GET /v1/credential-resources/{id}/grants/history` | `credentials:manage` | 指定接收主体的凭据授权历史 |

PUT 正文示例：

```json
{
  "grantee_tenant":"example",
  "grantee_subject":"caller",
  "release_hash":"REPLACE_WITH_64_HEX_AGENT_RELEASE_HASH",
  "expected_revision":0,
  "enabled":true
}
```

`expected_revision=0` 创建首条授权；之后必须提交当前修订，成功加一，陈旧请求返回 `409 resource_grant_revision_changed`。撤销使用相同端点并设置 `enabled=false`，Agent 授权仍提交精确发布摘要。凭据授权必须省略 `release_hash` 或设为 null。接收主体可以跨 tenant，但必须由资源 owner 显式授权；正文不能提供资源 owner。无需向自己授权。

历史查询的接收主体参数为 `granteeTenant`、`granteeSubject`，分页使用 `after`（授权修订）及 `limit`（1–200）。目录最多返回 1,000 条，超过上限明确报错，不默默截断。

V12 增加 `control_plane.resource_grants` 和 `control_plane.resource_grant_audit`。授权 CAS 与审计追加在同一事务中完成，审计失败会整体回滚；数据库触发器拒绝修改或删除已追加的审计行。应用角色只有必要权限，受限 memory-worker 不获得这些表的访问权。

## 验收边界

交付基线的测试覆盖认证目录拒绝非法配置、普通 token 管理拒绝、owner 与 CAS、HTTP 不接受凭据值、真实 PostgreSQL 上共享身份分离、精确版本、撤销、竞争 CAS、审计回滚/不可变、凭据引用重绑定及归档。事务锁竞争测试通过 `pg_blocking_pids` 检查 Agent 撤销、归档及凭据撤销确实等待授权事务提交。相关测试为 `TrustedIdentityDirectoryTest`、`AgentManagementHttpTest`、`ResourceAuthorizationIT`。共享子调用、ASK 恢复与跨实例 SSE 的完整验证由对应调用链测试承担。完整 PostgreSQL 结果见文首固定提交的 CI；没有将测试中的占位 token、离线模型或本地 MCP 服务当作真实身份提供方或外部凭据验收。
