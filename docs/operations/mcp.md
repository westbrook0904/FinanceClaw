# 通用 MCP 接入与 RollingGo 查询

通用 HTTP MCP 已接入现有工具目录。新增同类服务通常只需填写 TOML、配置凭据、绑定 Agent 并部署，
无需为每个工具编写参数类或客户端。模型使用普通工具调用，工具结果返回后仍由根 Agent 决定下一步。

本文用于接入酒店/机票查询、发布固定工具定义和排查运行错误。先完成
[本地完整链路](local-full-stack.md)；Taibu 的领域校验与专用协议见 [Taibu MCP](taibu-mcp.md)。

当前仓库配置以 [config/mcp.toml](../../config/mcp.toml) 为准：

| 服务 | 仓库状态 | 固定定义 | 根 Agent 的开放能力 |
| --- | --- | --- | --- |
| `rollinggo_hotel` | **已启用** | 已提交 `config/mcp/rollinggo_hotel.json` | 酒店搜索、详情、标签，共 3 个工具 |
| `rollinggo_flight` | **关闭** | 尚未提交定义；启用后导入 | 机场与机票查询，共 2 个工具 |

启用状态不等于账户可用：酒店实际执行仍需有效 Key、`travel:read` 和匹配的数据策略。
不使用旅行查询的部署可把对应服务设为 `enabled = false`，保留绑定即可。关闭的服务不读契约、不连接，
也不要求凭据。本文描述仓库行为，不声明当前容器或远端服务已经完成验收。

## 1. 配好酒店查询并部署

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

表中端点和名单来自仓库配置；实际参数在导入时取自当前账户返回的 `tools/list`，发布后使用冻结定义。
两个服务默认引用同一个 Key；若账户分别分配凭据，
修改各自 `auth.token_env` 为对应环境变量名即可。

### 启用并部署

在 TOML 中确认 `allowed_tools` 和 Agent 绑定；酒店已启用，机票仅在账户开通后改为 `enabled = true`。
配置好下文的调用权限和数据级别后，直接执行：

```bash
uv run --frozen python scripts/deploy.py
```

命令先补齐已启用服务缺失的定义，检查完整目录，再构建和启动 Docker。
已有定义默认离线复用，关闭的服务不连接、不要求契约或 Key。未开通机票时只启用酒店即可。
两个服务均启用时检查应显示 `5 个已启用工具`，只启用酒店为 `3`，全部禁用为 `0`。

工具定义需要更新时执行 `scripts/deploy.py --refresh-mcp`。
若改动了端点或增加 `allowed_tools`，已有文件检查可能失败，此时也需要显式刷新。
刷新只更新定义，不执行酒店或机票业务查询；生成差异仍需审阅。
希望先审阅生成文件时，执行 `scripts/deploy.py --prepare-only --refresh-mcp`，审阅后正常部署。

完成权限配置并部署后，可在飞书单聊或普通 Turn 的 `message` 中提交：

```text
请推荐北京的酒店，明天入住、住一晚，2 位成人，每晚预算 500 至 800 元，优先交通方便。请比较价格、位置和退改条件，并注明信息缺失的地方。
```

预期根 Agent 查询酒店、按实际返回标识取得详情，必要时读取结果工件，再组织比较。
缺少必需条件时先澄清；只返回一般旅行建议，不能作为实时酒店查询已执行的证据。

### 可选：查看清单或单独维护

不确定远端工具名称时，可以先保持服务关闭，用以下诊断命令查看；它们不再是部署前的必做步骤：

```bash
.venv/bin/python scripts/mcp_catalog.py discover --server rollinggo_hotel
.venv/bin/python scripts/mcp_catalog.py import --server rollinggo_hotel
.venv/bin/python scripts/mcp_catalog.py discover --server rollinggo_flight
.venv/bin/python scripts/mcp_catalog.py import --server rollinggo_flight
```

`discover` 只显示远端定义；`import` 只保存 TOML 的 `allowed_tools`，不会执行酒店或机票查询。
完整分页成功后才原子替换文件，任何一页失败都会保留旧文件。
生成的 `config/mcp/rollinggo_hotel.json` 和 `config/mcp/rollinggo_flight.json` 应随代码发布并审阅差异。

只检查当前本地定义：

```bash
.venv/bin/python scripts/mcp_catalog.py check
```

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

当前根 Agent 在启用紫微、或启用 Taibu 八字时，整轮任务会使用 `confidential`。
如果 MCP 策略只允许 `public/internal`，工具虽已装配，也会在模型请求前被过滤。
当前酒店配置已明确允许 `public/internal/confidential`，让此部署的根任务可以使用酒店查询；
机票仍关闭并保留 `public/internal`。新服务需要按其部署策略单独配置，不自动继承酒店的范围。

若部署策略允许该级别的任务调用对应服务，在该服务的 `policy` 中明确配置：

```toml
allowed_data_classes = ["public", "internal", "confidential"]
```

这表示允许 `confidential` 任务向该服务发送查询参数；提示词要求仅传本次出行条件，
它不构成字段级的数据隔离。若不允许该出域范围，使用更窄的策略，在没有启用上述领域能力的部署中测试旅行查询。

### 发布到 Docker

配置在进程启动时固定，修改 TOML 或导入文件后需要统一重新装配。
部署入口为 [scripts/deploy.py](../../scripts/deploy.py)，需要本机安装 uv 和 Docker Compose。
已有项目环境时也可以使用 `.venv/bin/python scripts/deploy.py`。

```bash
# 自动补齐缺失定义，检查、构建、启动，并显示容器状态
uv run --frozen python scripts/deploy.py

# 更新所有已启用 MCP 的定义后部署
uv run --frozen python scripts/deploy.py --refresh-mcp

# 只准备文件，供审阅；不构建、不启动容器
uv run --frozen python scripts/deploy.py --prepare-only --refresh-mcp

# 指定环境文件，并沿用 Taibu 的 Compose 组合
uv run --frozen python scripts/deploy.py --env-file .env \
  -f compose.yml -f compose.taibu.yml
```

默认读取 `.env`，也支持 `FINANCECLAW_ENV_FILE`，命令行 `--env-file` 优先。
同一文件同时用于 Compose 变量替换和应用容器的 `env_file`；`.env.memory` 仍按原 Compose 单独加载。
入口在内存中读取 Compose 解析后的配置，用实际 Worker 环境执行导入，不输出展开后的密钥。
`FINANCECLAW_MCP_CONFIG_PATH` 必须指向镜像包含的 `config/*.toml`，
已启用服务的 `contracts` 必须落在 `config/mcp/` 下。API 与 Worker 使用同一配置和端点。

准备阶段只执行 `initialize` 和 `tools/list`，不调用业务工具。
待导入服务全部获取成功并通过完整目录检查后才逐文件原子保存；远端或契约检查失败不会覆盖旧定义。
准备失败不构建镜像，构建失败不执行启动。已生成的文件会留在本地，便于审阅和提交。
服务启动沿用 `docker compose up -d --no-build`，最后显示 `ps -a`；返回不代表所有健康检查已经通过。
普通 `docker compose restart` 或 `up -d --no-build` 不触发发现，继续使用镜像中的固定定义。

若构建时报 `Failed to download` / `operation timed out`，这是构建环境下载 Python 包失败，
需查看该错误前的包名和下载地址。Dockerfile 使用 120 秒读取超时、30 秒连接超时、4 路下载，
并通过 BuildKit 缓存复用已下载的依赖；锁定版本与文件哈希仍会校验。
超时后可以重新执行部署命令；缓存不会进入运行镜像。
这些设置使用 [uv 的原生网络配置](https://docs.astral.sh/uv/reference/environment/#uv_http_timeout)
和 [Docker 构建缓存](https://docs.astral.sh/uv/guides/integration/docker/#caching)。
如果持续无法访问 `pypi.org` 或 `files.pythonhosted.org`，需要修复 Docker 的网络或代理，
仅在宿主机设置代理不代表 Docker 构建已经使用该代理。

Docker Desktop 可通过 `host.docker.internal` 访问宿主机代理，例如本机 HTTP 代理端口为 7890 时：

```bash
.venv/bin/python scripts/deploy.py --build-proxy http://host.docker.internal:7890
```

也可在终端设置 `FINANCECLAW_BUILD_PROXY`，之后执行普通部署命令。
该配置只向镜像构建传递 Docker 的预定义代理参数，不修改 MCP 导入或应用容器的代理配置；
Dockerfile 不声明这些代理 ARG，避免将代理值记录到镜像历史。
地址必须能从 Docker 内访问，容器中的 `127.0.0.1` 指向容器自身。
参见 [Docker 构建代理说明](https://docs.docker.com/build/building/variables/#proxy-arguments)。

API 与图 Worker 必须使用相同配置和契约。API 装载目录不连接 MCP，也不需要 MCP Key；
实际执行时由图 Worker 获取凭据。现有 Compose 共享 `.env` 的注入方式可直接使用，无需新增 RollingGo 容器。
独立 `memory_worker` 不使用这些工具，无需给 `.env.memory` 添加 RollingGo Key。

发布前结束使用旧配置的运行/澄清轮次，新测试从新的 Turn 开始。
修改契约、端点、凭据引用或治理策略会改变相关 Agent 的发布指纹；旧任务不会静默切换定义继续执行。
这次改造不需要新增表、迁移或清空数据库。

## 2. 新增另一个只读 MCP

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

在**已有**的 `[agents.finance_agent].mcp_tools` 数组中追加 `"catalog.lookup"`，
配置密钥、启用服务，然后执行 `uv run --frozen python scripts/deploy.py` 即可自动导入并部署。
不确定工具名时先用 `discover --server catalog` 查看清单。
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
| `pin_server_version = true` | 额外固定远端软件版本，默认核对服务名、所选工具存在及输入参数结构 |

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

## 3. 运行时会发生什么

每次工具调用使用同一个 SDK session 完成初始化、完整 `tools/list`、固定定义核对和 `tools/call`。
当前没有核验缓存，因此每次查询都会付出初始化与目录请求的耗时；不缓存业务答案。
服务身份、所选工具存在性或输入参数结构变化时返回 `MCP_CONTRACT_CHANGED`，需重新导入、审阅并发布。
运行时结构比较保留字段、类型、必填项、枚举、嵌套结构和取值约束；忽略 Schema 中的描述、标题、示例及默认值等注释。
工具展示元数据或远端输出 Schema 的变化不会单独阻断调用；实际回包仍按已发布的输出契约处理。
完整定义仍保存在导入文件和发布指纹中，运行期间不自动替换模型看到的工具说明。
这类发布差异不能通过修改业务参数修复，错误回执会要求本轮停止重试该工具。
可用 `scripts/deploy.py --prepare-only --refresh-mcp` 更新文件，审阅后执行正常部署。
后来新增的远端工具不会自动进入 Agent。

模型侧名称默认为 `mcp__服务别名__远端工具名`，首轮即可使用已绑定且有权限的完整 Schema。
未引入 `ProviderToolSearch`、`search_tools` 或工具选择中间件。工具参数占用现有上下文预算。
支持既有 `/tool` 指令，例如导入酒店工具后使用 `/tool mcp__rollinggo_hotel__searchHotels`，
参数不足时由根使用既有澄清工具询问，不跨轮缓存或自动补值。

根 Agent 的对外回答只描述业务能力、结果和必要限制，不列举内部工具标识、参数 Schema、
MCP 配置或诊断细节。用户问“有哪些工具”时也按业务能力回答。
工具协议仍携带完整名称与 Schema，原生执行进度继续独立展示。
历史消息中“没有实时查询能力”的旧结论不能覆盖当前模型请求实际可见的工具。
这些是根系统提示的行为约束，不是对任意模型输出的确定性文本过滤器。

返回的 `content`、`structuredContent`、扩展数据和实际查询参数会一并保存在工具 artifact 中。
模型先看文本；只有结构化结果时直接看 JSON。达到既有归档阈值的大结果保存为工件引用，
由 `read_artifact` 回读，不为读取旧结果重跑远端查询。没有文本的图片/资源信息仍被保留，链接不会自动抓取。
超过 `result_max_bytes` 的响应明确失败；它不属于“已成功归档”的结果。

## 4. 故障定位

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

## 5. 大结果文件与按需读取

酒店搜索和详情通过 `config/mcp.toml` 的 `result_views` 配置使用文件引用。完整回包
保存在现有 Artifact 中；模型收到业务格式、字段/数组目录、记录数量及最多三条预览。
预览和目录的回执上限为 4 KiB，预览不代表全部结果或价格排序。普通 MCP 可省略该配置，
继续按业务正文阈值自动决定是否内联。

```toml
[servers.rollinggo_hotel.result_views.searchHotels]
delivery = "reference"
collection_path = "/hotelInformationList"
preview_fields = ["/hotelId", "/name", "/price"]
preview_records = 3
```

结果规则只影响目录和预览，原始字段仍可读取。配置路径与回包不符时返回通用目录，
不把展示问题变成参数澄清。规则变化进入发布指纹，需要 API/Worker 一起重新部署。

`read_artifact` 3.0.0 要求从已有结果复制配对的 ID、SHA-256，并明确指定 `mode`：

| 模式 | 用法 |
|---|---|
| `inspect` | 查看业务根或 `path` 处的结构和数组长度 |
| `json` | 用 JSON Pointer 定位 `path`；`fields` 的每一项也是 JSON Pointer，相对于选中的对象或每条数组记录；数组用 `start/limit` 分页 |
| `text` | 对文本业务视图用 `offset/max_chars` 分页 |

`path` 相对于实际业务数据，不需要定位 MCP 或归档包装。空字符串 `""` 表示业务根；
非空路径必须以 `/` 开头，逐层用 `/` 分隔，不支持 `$`、点号路径或通配符。
`"/"` 表示名称为空的字段，不代表根。字段名本身包含 `/` 或 `~` 时分别写为 `~1` 或 `~0`。

下面两例展示读取参数；调用时还需从已有结果原样复制配对的 `artifact_id`、`content_hash`。

例一：业务根本身就是酒店详情对象，读取该对象的名称和价格：

```json
{"mode": "json", "path": "", "fields": ["/name", "/price"]}
```

例二：业务根包含 `hotelInformationList` 数组，读取前 10 家酒店的名称和价格：

```json
{"mode": "json", "path": "/hotelInformationList", "fields": ["/name", "/price"], "start": 0, "limit": 10}
```

第二例的 `fields` 相对于每条酒店记录，不重复 `/hotelInformationList`。
`["name", "price"]` 会触发 `invalid JSON pointer`；合法路径选中的字段不存在时则记录为
`missing_fields`，不属于语法错误。不确定结构时先用 `mode="inspect", path=""` 查看目录。

记录默认请求 20 条，最多 200 条，
实际回执仍受 16 KiB 上限约束；跟随 `next_start` 或 `next_offset`，不能按请求数量自行跳页。
单个 JSON 对象按 `path/fields` 读取，忽略 `start/limit`，携带 `limit=1` 不会再触发数组分页错误。
单条数据装不下时返回 `needs_narrower_selection`，应减少字段或定位更深路径。
读取页面被上下文清理时复用原文件引用，不再产生一层页面文件。

根默认保留一次模型调用用于预算收尾。8 次根模型、12 次根工具限制保持不变；共享
SQL 模型预算中的最后一次也留给根最终回答，普通子调用和重试不可占用。超额工具批次
补齐未执行回执后交回根；取消、撤销授权和服务故障仍服从原终止规则。

完整设计和当前验收状态见[大结果实施方案](../../.redesign/stages/MCP-大结果归档与结构化读取实施方案.md)。

## 6. 验证范围与源码入口

在仓库根目录运行离线契约检查和自动化回归：

```bash
.venv/bin/python scripts/mcp_catalog.py check
.venv/bin/python -m pytest -q tests/mcp_integration tests/stage11/test_deployment.py tests/architecture
```

`check` 成功说明已启用的本地定义和 Agent 绑定可装配。自动化覆盖固定发布、真实 SDK 的分页/认证/错误、
原生工具循环、取消、进度、重试和 Artifact；HTTP 使用本地协议替身，模型使用确定性替身，
不代表真实账户查询、模型选择或飞书展示已通过。

真实账户验收从酒店标签 → 酒店搜索 → 使用返回标识查详情开始；启用机票后再验机场 → 航班。
最后验证飞书缺参澄清、停止、工具进度和最终回复。价格、币种、税费、退改规则应按本次结果核对；
查询或预订链接不代表锁房、下单或出票。

历史[契约与 Artifact 修复记录](../../.redesign/evidence/mcp-views/contract-read-hotfix.json)
保存了 2026-09-16 的真实只读 MCP 与文件回读探测，明确未执行真实模型决策和飞书消息发送。
该记录及旧部署状态不是当前环境健康证明。设计演进另见
[配置化接入方案](../../.redesign/stages/MCP-工具主动发现与配置化接入实施方案.md)和
[大结果方案](../../.redesign/stages/MCP-大结果归档与结构化读取实施方案.md)。

| 入口 | 负责什么 |
| --- | --- |
| [config/mcp.toml](../../config/mcp.toml) | 服务开关、认证变量名、工具/Agent 绑定、数据策略、结果视图 |
| [mcp_catalog.py](../../scripts/mcp_catalog.py) | 发现、导入、离线检查与部署前准备 |
| [deploy.py](../../scripts/deploy.py) | 读取 Compose 的实际 Worker 配置后准备、构建、启动 |
| [configuration.py](../../financeclaw/shared/mcp/configuration.py) | 声明校验和固定 MCP 发布 |
| [contracts.py](../../financeclaw/shared/mcp/contracts.py) | 忽略说明文字，只比较调用相关的输入结构 |
| [mcp_transport.py](../../financeclaw/agent_server/tools/mcp_transport.py) | SDK session、服务/契约核对与真实调用 |
| [mcp_generic.py](../../financeclaw/agent_server/tools/mcp_generic.py) | 受治理工具包装、结果投影及失败回执 |
