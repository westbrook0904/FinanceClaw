# 通用 MCP 接入与 RollingGo 查询

通用 HTTP MCP 已接入现有工具目录。新增同类服务通常只需填写 TOML、导入工具定义、绑定 Agent，
无需为每个工具编写参数类或客户端。模型使用普通工具调用，工具结果返回后仍由根 Agent 决定下一步。

目前提供 RollingGo 酒店三个、机票两个查询工具的配置。仓库默认关闭这两个服务，
尚未包含真实账户导出的工具定义。启用前需要有效 API Key；未认证访问两个官方端点均返回 HTTP 401。
已通过真实 MCP SDK 与本地 HTTP 协议替身验证通用链路，真实查询、模型选择和飞书端到端效果仍待账户联调。

## 1. 启用 RollingGo

所有命令在仓库根执行，使用已安装项目依赖的 `.venv`。也可以把 `.venv/bin/python` 换成 `uv run python`。

### 配置密钥

在本地 `.env` 或部署环境中填写 `FINANCECLAW_ROLLINGGO_API_KEY`。
API Key 不写入 TOML、导入文件或命令行参数。执行环境变量优先于 dotenv，
代码显式使用 `_env_file=None` 时不读取 dotenv。

服务配置在 [config/mcp.toml](../../config/mcp.toml)：

| 服务别名 | 端点 | 开放的远端工具 |
|---|---|---|
| `rollinggo_hotel` | `https://mcp.rollinggo.cn/mcp` | `searchHotels`、`getHotelDetail`、`getHotelSearchTags` |
| `rollinggo_flight` | `https://mcp.rollinggo.cn/mcp/flight` | `searchAirports`、`searchFlights` |

端点及工具名单依据 [RollingGo 官方连接说明](https://github.com/RollingGo-AI/rollinggo-hotel-mcp)。
实际参数以当前账户返回的 `tools/list` 为准。两个服务默认引用同一个 Key；若账户分别分配凭据，
修改各自 `auth.token_env` 为对应环境变量名即可。

### 导入并启用

先保持 `enabled = false`，查看账户可见清单，再导入允许的工具：

```bash
.venv/bin/python scripts/mcp_catalog.py discover --server rollinggo_hotel
.venv/bin/python scripts/mcp_catalog.py import --server rollinggo_hotel
.venv/bin/python scripts/mcp_catalog.py discover --server rollinggo_flight
.venv/bin/python scripts/mcp_catalog.py import --server rollinggo_flight
```

`discover` 只显示远端定义；`import` 只保存 TOML 的 `allowed_tools`，不会执行酒店或机票查询。
完整分页成功后才原子替换文件，任何一页失败都会保留旧文件。
生成的 `config/mcp/rollinggo_hotel.json` 和 `config/mcp/rollinggo_flight.json` 应随代码发布并审阅差异。

审阅完成后，把对应服务的 `enabled` 改为 `true`，再检查：

```bash
.venv/bin/python scripts/mcp_catalog.py check
```

两个服务均启用时应显示 `5 个已启用工具`。全部禁用时显示 `0`，这是正常结果。
`check` 离线验证已启用契约、端点、Schema、命名和 MCP 绑定，不测试凭据有效性。
关闭服务时保留其 Agent 绑定即可，装配会忽略这些绑定；不需要删除配置。
未开通机票时可只启用酒店。

如使用其他配置路径或环境文件，所有维护命令均支持：

```bash
.venv/bin/python scripts/mcp_catalog.py check --config config/mcp.toml --env-file .env
```

应用进程通过 `FINANCECLAW_MCP_CONFIG_PATH` 指定 TOML，默认就是 `config/mcp.toml`。
`contracts` **相对于 TOML 所在目录**，因此默认填写 `mcp/rollinggo_hotel.json`，不要再加一层 `config/`。

### 配置调用身份与任务数据级别

工具要求 `travel:read`。在现有 `FINANCECLAW_FEISHU_SCOPES` 和开发环境的
`FINANCECLAW_API_SCOPES` 中追加该权限，保留原来的权限和用于回读大结果的 `artifacts:read`。
生产 OIDC 身份应由身份提供方授予 `travel:read`；环境变量不会覆盖 JWT 的授权。

默认 MCP 策略只允许 `public/internal`。当前根 Agent 在启用紫微、或启用 Taibu 八字时，
整轮任务会使用 `confidential`，此时旅行工具会被现有权限规则过滤，即使用户只问酒店也一样。
这是现有的整轮数据分级机制，本次没有新增按参数降级或自动放行。

若部署策略允许该级别的任务调用 RollingGo，在两个服务的 `policy` 中明确配置：

```toml
allowed_data_classes = ["public", "internal", "confidential"]
```

这表示允许 `confidential` 任务向该服务发送查询参数；提示词要求仅传本次出行条件，
它不构成字段级的数据隔离。若不允许该出域范围，保持默认策略，在没有启用上述领域能力的部署中测试旅行查询。

### 发布到 Docker

配置在进程启动时固定，修改 TOML 或导入文件后需要统一重新装配。
Dockerfile 已包含 `config/*.toml` 和 `config/mcp/`。导入命令在本地执行后再构建镜像：

```bash
docker compose build api
docker compose up -d --no-build
docker compose ps -a
```

若原部署叠加了 Taibu 等 Compose 文件，继续使用原来的 `-f` 参数组合。
API 与图 Worker 必须使用相同配置和契约。API 装载目录不连接 MCP，也不需要 MCP Key；
实际执行时由图 Worker 获取凭据。现有 Compose 共享 `.env` 的注入方式可直接使用，无需新增 RollingGo 容器。
独立 `memory_worker` 不使用这些工具，无需给 `.env.memory` 添加 RollingGo Key。

发布前结束使用旧配置的运行/澄清轮次，新测试从新的 Turn 开始。
修改契约、端点、凭据引用或治理策略会改变相关 Agent 的发布指纹；旧任务不会静默切换定义继续执行。
这次改造不需要新增表、迁移或清空数据库。

## 2. 新增另一个 MCP

先在 TOML 中添加服务，例如下面的结构。地址、工具名和描述文件应换成真实值：

```toml
[servers.catalog]
enabled = false
transport = "streamable_http"
url = "https://mcp.example.com/mcp"
allowed_hosts = ["mcp.example.com"]
allowed_tools = ["lookup"]
contracts = "mcp/catalog.json"

[servers.catalog.auth]
type = "headers"
headers_env = { "X-API-Key" = "CATALOG_API_KEY" }

[servers.catalog.policy]
side_effect = "read"
required_scopes = ["catalog:read"]
egress = "external"
allowed_data_classes = ["public", "internal"]
```

然后执行 `discover --server catalog` 和 `import --server catalog`，
在**已有**的 `[agents.finance_agent].mcp_tools` 数组中追加 `"catalog.lookup"`，启用并运行 `check`。
只给子 Agent 使用时，把引用放进 `[agents.market_research_agent]` 或 `[agents.ziwei_doushu_agent]`，
不要加到根的绑定。它仍须满足该子 Agent 原有的任务、授权和输出协议。
未知 Agent、未知工具或重复绑定会报错，不会回退到“所有 Agent 都可见”。

其他可选项：

| 配置 | 用法 |
|---|---|
| `url_env` | 用环境变量提供地址，与 `url` 二选一；URL 不承载密钥或查询参数 |
| `auth.type = "none"` | 无认证服务，省略 `token_env/headers_env` |
| `auth.type = "bearer"` | 配合 `token_env = "某个环境变量名"` |
| `aliases = { lookup = "mcp__catalog__lookup" }` | 自定义模型侧名称，适用于远端名字过长或不符合工具命名规则 |
| `policy.tenant_allowlist = ["tenant-a"]` | 进一步限制可调用租户 |
| `[servers.catalog.tool_policies.lookup]` | 完整覆盖该工具的读取策略，仍需明确 `side_effect/required_scopes/allowed_data_classes` |
| `pin_server_version = true` | 额外固定远端软件版本，默认只固定服务名和所选工具完整定义 |

`[defaults]` 提供连接参数默认值，每个服务可同名覆盖：

| 参数 | 默认值 | 作用 |
|---|---|---|
| `timeout_seconds` | 30 | 单次尝试的初始化、核对和调用总超时 |
| `catalog_max_pages` | 20 | 列举目录最大页数 |
| `catalog_max_bytes` | 2 MiB | 目录和本地导入文件的字节上限 |
| `result_max_bytes` | 2 MiB | 单次 MCP 结果上限，与模型上下文和工件内联阈值分别计算 |

当前支持 Streamable HTTP、匿名/Bearer/固定 Header 认证和只读查询。
输入/输出定义使用 JSON Schema 2020-12 校验，只支持文档内部的 Schema 引用，不在线加载外部定义。
OAuth、stdio、订单写入、长期会话、MCP sampling/elicitation 尚未接入。
远端的 `readOnlyHint` 等注解不会自动授予执行权限，开放范围由本地读取策略决定。

## 3. 运行行为与排错

每次工具调用使用同一个 SDK session 完成初始化、完整 `tools/list`、固定定义核对和 `tools/call`。
当前没有核验缓存，因此每次查询都会付出初始化与目录请求的耗时；不缓存业务答案。
服务身份或所选工具定义变化时返回 `MCP_CONTRACT_CHANGED`，需重新导入、审阅并发布。
后来新增的远端工具不会自动进入 Agent。

模型侧名称默认为 `mcp__服务别名__远端工具名`，首轮即可使用已绑定且有权限的完整 Schema。
未引入 `ProviderToolSearch`、`search_tools` 或工具选择中间件。工具参数占用现有上下文预算。
支持既有 `/tool` 指令，例如导入酒店工具后使用 `/tool mcp__rollinggo_hotel__searchHotels`，
参数不足时由根使用既有澄清工具询问，不跨轮缓存或自动补值。

返回的 `content`、`structuredContent`、扩展数据和实际查询参数会一并保存在工具 artifact 中。
模型先看文本；只有结构化结果时直接看 JSON。达到既有归档阈值的大结果保存为工件引用，
由 `read_artifact` 回读，不为读取旧结果重跑远端查询。没有文本的图片/资源信息仍被保留，链接不会自动抓取。
超过 `result_max_bytes` 的响应明确失败；它不属于“已成功归档”的结果。

| 现象/错误 | 含义与处理 |
|---|---|
| 模型看不到旅行工具 | 检查 `enabled`、Agent 绑定、身份 `travel:read` 和整轮数据级别 |
| `MCP_CREDENTIAL_MISSING` | 执行环境没有对应凭据；离线目录检查通过不代表凭据已配置 |
| `MCP_HTTP_REJECTED` / HTTP 401、403 | 核对 Key 及对应酒店/机票权限，不自动重试 |
| `MCP_RATE_LIMITED` / HTTP 429 | 本次查询失败，核对服务限流，不追加盲目重试 |
| `MCP_INPUT_INVALID` | 参数不符合导入的 JSON Schema，返回相关字段名让根修正或澄清 |
| `MCP_OUTPUT_INVALID` / 远端 `isError` | 失败回执保留原始响应，不能当作成功查询 |
| `MCP_UNAVAILABLE` | 网络、超时或 5xx；沿用现有最多两次重试，耗尽后仍返回准确的错误调用回执 |
| `MCP_CATALOG_TOO_LARGE` / `MCP_RESULT_TOO_LARGE` | 调整合理上限或缩小查询，不静默截断成成功 |

重试的每次实际尝试仍计入已有 Turn 预算；没有第二层 MCP 重试调度。
停止会取消当前 SDK session，取消不会变成查询成功或触发重试。
工具开始/完成/失败走原生 `tool.progress`；单个工具完成不代表根任务完成。
飞书仍由已有整轮状态与根输出驱动，真实卡片验收需在部署后进行。

## 4. 验证范围

自动化测试位于 [tests/mcp_integration](../../tests/mcp_integration)，覆盖配置与冻结发布、
API 无 SDK 启动、真实 SDK 分页/认证/错误、根和子 Agent 的工具循环、`/tool`、调用配对、
单层重试、原生进度、取消清理及大结果归档。HTTP 为本地协议替身，模型为确定性测试模型。

```bash
.venv/bin/python -m pytest -q tests/mcp_integration
```

2026-09-15：上述新增用例 **47 passed**。包含既有架构、Taibu、Agent 与飞书修复用例的
回归 **393 passed**；全仓 Ruff、编译和密钥扫描通过。回归命令为：

```bash
TIKTOKEN_CACHE_DIR= .venv/bin/python -m pytest -q tests/mcp_integration tests/architecture tests/taibu tests/stage1 tests/stage6fix tests/stage8_hotfix
```

真实账户验收顺序：酒店标签 → 酒店搜索 → 依据返回标识查详情；机场查询 → 依据返回代码查航班；
最后在飞书测试酒店与机票组合、缺参澄清、停止和流式回答。核对实际币种、价格口径、税费及退改规则，
不把查询结果或预订链接表述为已锁房、下单或出票。

总体边界与后续 OAuth/订单工作见[实施方案](../../.redesign/stages/MCP-工具主动发现与配置化接入实施方案.md)。
