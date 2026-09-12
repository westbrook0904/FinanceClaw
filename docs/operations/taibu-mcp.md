# Taibu MCP 接入与运行

更新日期：2026-09-12。已实现首期 `taibu_almanac`、`taibu_bazi`，默认关闭；配置、工具实例、发布指纹、权限、重试预算、原文归档及有界结果均已接入 FinanceClaw。现有紫微五工具继续使用原有实现。

本次完成代码与隔离联调，没有切换现有 API/Worker，也没有发送飞书消息。真实模型、飞书和持久原生运行时的联合验收仍是正式开放前的发布门槛。

## 1. 版本与运行边界

| 项目 | 固定值 / 行为 |
|---|---|
| 上游源码 | `e8f636972a6fdb14f2a532ee223101f889ab4820` |
| MCP HTTP 服务 | `taibu-mcp-online`，`3.1.1` |
| 计算包 | `taibu-core 3.5.0`，依赖沿用上游 lockfile |
| Python SDK | 沿用项目锁定依赖；实测 LangChain `1.3.18`、adapter `0.3.2`、MCP SDK `1.29.1` |
| 传输 | Streamable HTTP，每次尝试新建并关闭 session |
| 开放工具 | 仅黄历、八字；模型不能指定远端 URL 或任意远端工具名 |
| 真实出生参数 | 仅允许部署配置明确声明的内部端点 |
| 公共服务 | 仅支持允许出域的黄历；confidential 上下文不允许发往公共服务 |
| 认证 | 上游不验证 API Key，访问控制依赖内部网络；本地工具另需 `taibu:read` |

镜像使用固定 Node 22 Alpine digest 和 pnpm `10.11.0`。构建时仅去掉上游 lockfile 的根 Web 应用 importer，保留两个 MCP 包及其依赖版本；两次安装都使用 `--frozen-lockfile`，运行依赖安装还使用 `--offline`。最终镜像不包含根 Web 应用依赖，并保留两个包的 LICENSE。

本机验收平台为 `linux/amd64`，镜像标签为 `financeclaw-taibu:e8f6369`。具体本地 image ID 记录在[镜像证据](../../.redesign/evidence/taibu-mcp/image-validation.json)；image ID 不是可供远端拉取的仓库 manifest digest。正式推送镜像后，应将真实 `repository@sha256:...` 配置到 `TAIBU_IMAGE`。

## 2. 配置与权限

所有环境示例已经补充以下配置；实际 `.env` 未被修改。

```dotenv
FINANCECLAW_TAIBU_ENABLED=false
FINANCECLAW_TAIBU_MCP_URL=http://taibu-mcp:3001/mcp
FINANCECLAW_TAIBU_EGRESS=internal
FINANCECLAW_TAIBU_ALLOWED_HOSTS='["taibu-mcp"]'
FINANCECLAW_TAIBU_ALLOWED_TOOLS='["almanac","bazi"]'
FINANCECLAW_TAIBU_TIMEOUT_SECONDS=10
FINANCECLAW_TAIBU_PROJECTION_BYTES=8192
FINANCECLAW_TAIBU_RESULT_MAX_BYTES=262144
FINANCECLAW_TAIBU_CONTRACT_CACHE_SECONDS=300
```

启用八字还必须设置以下三项；`compose.taibu.yml` 会同时注入 API 与 Worker：

```dotenv
FINANCECLAW_DEBUG_FULL_IO=false
FINANCECLAW_LANGSMITH_HIDE_INPUTS=true
FINANCECLAW_LANGSMITH_HIDE_OUTPUTS=true
```

单独给获授权身份增加 `taibu:read`，保留 `artifacts:read` 以便回读历史结果。开发 API 使用 `FINANCECLAW_API_SCOPES`，飞书身份使用 `FINANCECLAW_FEISHU_SCOPES`，生产 OIDC 使用身份提供方的 scope。修改 scope 清单时保留已有业务授权，不能用 `*` 替代。示例最小只读清单为：

```dotenv
FINANCECLAW_API_SCOPES='["market:read","tools:read","artifacts:read","taibu:read"]'
FINANCECLAW_FEISHU_SCOPES='["market:read","tools:read","artifacts:read","taibu:read"]'
```

可用 `FINANCECLAW_TAIBU_TENANT_ALLOWLIST='["your-tenant"]'` 限定租户。八字还要求可信执行上下文为 `confidential`；启用八字时根 Agent 的发布分级会同步提高。缺 scope、租户不匹配或数据分级不符都会在远端调用前被拒绝。

`allowed_hosts` 是部署管理员维护的明确允许清单，并不替代网络隔离。URL 必须是 HTTP(S) 的 `/mcp`，禁止 userinfo、query、fragment 和重定向；外部路由要求 HTTPS，公开 taibu 域名不能声明成内部。内部 HTTP 客户端不继承系统代理，避免 localhost 或 Compose 请求误走代理。

## 3. 构建、启用与验收

先准备现有统一部署需要的 `.env`、数据库和原生 AgentServer 凭据。停止接收新任务并处理在途 Turn 后，再同步切换 API/Worker 的能力配置。

```bash
docker compose -f compose.yml -f compose.taibu.yml config --quiet
docker compose -f compose.yml -f compose.taibu.yml build taibu-mcp
docker compose -f compose.yml -f compose.taibu.yml up -d taibu-mcp
docker compose -f compose.yml -f compose.taibu.yml ps taibu-mcp
```

预期 taibu 容器显示 healthy。服务只 `expose: 3001`，不发布宿主机端口；Worker 等待其健康。上游 `MCP_ALLOWED_HOSTS` 精确匹配完整 HTTP Host，因此必须是 **`taibu-mcp:3001`**；本地 `FINANCECLAW_TAIBU_ALLOWED_HOSTS` 则只填写主机名 `taibu-mcp`，两者格式不同。

```bash
docker compose -f compose.yml -f compose.taibu.yml up -d --build api worker integrations
docker compose -f compose.yml -f compose.taibu.yml ps
```

预期 API、Worker、integrations 健康。真实模型验证需使用已经配置的 provider，并关闭 `FINANCECLAW_OFFLINE_MODEL`。通过现有产品入口用合成资料核对：

1. “查询 2026-09-12 的黄历”：只调用 `taibu_almanac`，日期与请求一致。
2. “按公历 1990-01-15 09:00、中国标准时间、标准时间排八字，性别男”：调用 `taibu_bazi`，返回四柱及来源。
3. 故意缺少分钟、历法或时间口径：提出澄清；回答后沿原 Turn 恢复，不创建第二个有效业务任务。
4. 追问上次结果：按原 `artifact_ref` 回读；不重新计算后冒充历史结果。
5. taibu 停机：最多三次实际尝试，每次计入原 Turn 预算，最终明确工具失败；其他工作可以继续。

改动影响 `deployment_revision` 与配置指纹，API/Worker 必须同步。工具清单、端点、时间/大小预算、缓存周期以及本地和远端 Schema 都参与冻结。API 只读本地声明，不在启动时连接 MCP。每次 MCP 尝试核验服务名称/版本，工具 Schema 成功验证最多缓存 300 秒；变化会拒绝调用并要求重新验收。

## 4. 可复现的独立联调

仓库提供只接受服务地址的探针，不提供用户出生资料输入参数。它调用真实 SDK、真实工具包装和临时 ArtifactService，报告中保存全部合成快照，完成后删除临时数据库和存储。

若希望单独验证 MCP，而不启动产品栈：

```bash
docker build \
  --build-context taibu-src=https://github.com/hhszzzz/taibu.git#e8f636972a6fdb14f2a532ee223101f889ab4820 \
  -t financeclaw-taibu:e8f6369 deploy/taibu

docker run --detach --rm --name financeclaw-taibu-probe \
  --publish 127.0.0.1:13001:3001 \
  --env MCP_ALLOWED_HOSTS=127.0.0.1:13001 \
  --env MCP_REQUEST_LOG=false --env MCP_TRUST_PROXY=false \
  --memory 768m --cpus 1 --init financeclaw-taibu:e8f6369

.venv/bin/python -m experiments.taibu.probe \
  --url http://127.0.0.1:13001/mcp \
  --output /tmp/taibu-self-hosted-probe.json

docker stop financeclaw-taibu-probe
```

预期 `passed: true`、`cases: 6`：黄历、标准时间八字、真太阳时八字、普通农历、有效闰月均成功；不存在的农历闰月必须明确失败并保留原始错误工件。网络联通初次验证失败时，先检查服务健康和 Host 端口清单。

公共黄历探针：

```bash
.venv/bin/python -m experiments.taibu.probe \
  --url https://mcp.mingai.fun/mcp --public \
  --output /tmp/taibu-public-almanac.json
```

预期 `passed: true`、`cases: 1`。公共模式不开放八字，也不发送出生资料。

## 5. 输入、结果和失败语义

黄历的 `date` 和 `day_offset` 必须且只能提供一个。相对日期始终取本轮可信 `request_clock` 和 `timezone`，恢复、重试不会改成执行机器的新日期。`day_master` 仅接受十天干。

八字必须明确性别、公历/农历、年月日、小时、分钟、`time_basis="china_standard"` 和 `solar_time`。农历还必须明确是否闰月；实际农历月份及天数由固定计算引擎复验。海外当地钟表、夏令时和已校正太阳时不在首期输入范围内。

`solar_time="true_solar"` 必须提供明确数值经度，客户端只传原始时刻，由上游校正一次；`standard` 禁止附带经度。真太阳时结果必须报告 `placeResolutionInfo.resolved=true`、`source=manual_input` 和相同经度，否则不能作为成功结果。

原始 `content`、`structuredContent`、调用参数、口径、版本、契约哈希、调用时间及耗时均保存在工件中。工具消息返回版本化 `TaibuToolResult`，只带有界数据、警告和 `artifact_ref`。权限审计记录关联 `tool_call_id`；工件以该 ID 为 `source_id`，可结合 Turn、所有者和结果状态定位证据。

实际投影不超过 `min(taibu_projection_bytes, artifact_inline_bytes - 1024)`，并复验外层消息再次进行 JSON 编码、附上引用后的实际大小。原始结果上限独立计算；超过预算时返回明确错误和可回读引用，不增大全局上下文。256 KiB 是解析后的应用层序列化上限，不是 HTTP 响应流的硬上限。

| 情况 | 行为 |
|---|---|
| 缺项、冲突、不明确时间口径 | 安全输入错误，要求根 Agent 澄清，不回显完整出生记录 |
| 连接失败、超时、5xx | 交给现有中间件最多重试两次；每次通过 Turn 累计预算 |
| 重试耗尽 | taibu 专用中间件返回 error ToolMessage，根图可继续；其他工具原有失败行为保持不变 |
| 429 | 返回限流及安全的 Retry-After 秒数；不立即重试 |
| 403 / 重定向 | 不追随重定向，不重试，检查端点及 Host 配置 |
| `isError`、缺 JSON、不完整四柱、日期不符 | 明确失败；已收到且在预算内的原文先归档 |
| 服务版本或 Schema 漂移 | 在工具执行前拒绝，不能自动接受新清单 |
| 用户取消 | 透传取消并清理 session，不转成可重试错误 |

SDK 适配的一处实现细化：使用 `session.send_request(CallToolRequest, CallToolResult)`，而非 SDK 的高层 `call_tool()`。后者会按即时发现的输出 Schema 提前验证；当前实现通过 SDK 的公开请求接口保留原始结果，随后按本地冻结 Schema 和业务不变式校验。

## 6. 验证状态

本次证据汇总见[验收记录](../../.redesign/evidence/taibu-mcp/implementation-validation.json)。自动化覆盖真实 AgentFactory、原生内存 checkpoint 的澄清恢复、工具预算、SDK HTTP 模拟传输、固定发布、权限及 Artifact 归属；外部计算在自动化中使用固定 fixture。

真实 MCP 验证单列保存：[自建服务](../../.redesign/evidence/taibu-mcp/self-hosted-probe.json)、[MCP 容器重启后](../../.redesign/evidence/taibu-mcp/after-restart-probe.json)、[公共黄历](../../.redesign/evidence/taibu-mcp/public-almanac-probe.json)、[Compose 配置](../../.redesign/evidence/taibu-mcp/compose-validation.json)。所有出生样例均为明确的合成资料。报告里的临时 Artifact 引用不指向产品存储，其完整快照直接保存在报告中。

稳定 FinanceClaw 基线 `7dab6d4` 加本次 taibu 改动的隔离完整回归：**486 通过、4 跳过**，使用空 `TIKTOKEN_CACHE_DIR`。其中 taibu 专项 **66 项通过**。工作区同时进行 Stage 11 重构，全量回归状态应以验收记录为准；不能将隔离结果当作所有并行改动已验收。

真实模型、飞书答案去重、持久原生运行时的进程重启恢复及实际生产切换尚未联合验证。当前开关默认关闭，不把单次 MCP 联通或内存图测试当作这些发布门槛已通过。

## 7. 回退

先完成或取消本次发布下的在途 Turn，再恢复不含 taibu 的统一配置和应用版本。关闭开关会改变发布指纹，不能用它热切换已有 pending 交互。

`compose.taibu.yml` 会覆盖 `.env` 中的 `TAIBU_ENABLED=false`，所以回退时必须移除 overlay：

```bash
docker compose -f compose.yml up -d --force-recreate api worker
docker compose -f compose.yml -f compose.taibu.yml stop taibu-mcp
```

若 `.env` 曾手动启用，也需将 `FINANCECLAW_TAIBU_ENABLED=false` 恢复。旧工件按既有归属、权限和保留策略继续回读；回退不删除历史数据。本次 taibu 接入没有新增业务表，也不要求业务数据库迁移。
