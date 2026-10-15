# M3：主业务接入与验证

日期：2026-10-15。承接本地提交 `3981eb9` 的 M1/M2。本阶段增加酒店、Taibu、调酒 Skill、市场研究子 Agent 和投资组合 Workflow，工具链仍固定为 Temurin 21.0.12+8 / Spring Boot 3.5.16 / AgentScope 2.0.3 / MyBatis 3.0.5。

验证使用真实 PostgreSQL、独立 JVM、原生 SDK 和本地模拟服务。**没有调用真实酒店/Taibu、真实模型或飞书；紫微关闭，未做算法迁移验收。** 行情仅来自部署者提供的带来源和时点的冻结文件，不会生成假实时价格。

## 已实现的调用路径

| 能力 | 入口和框架复用 | FinanceClaw 边界 |
| --- | --- | --- |
| 酒店 | 服务端原名 `searchHotels`、`getHotelDetail`、`getHotelSearchTags`；SDK Toolkit MCP 注册 | `travel:read`、host/tenant/凭据引用、根预算和结果归档 |
| Taibu | 服务端原名 `almanac`、`bazi`；原生 `McpTool` 的 Schema 和调用 | 明确日期/历法/分钟、结果关键字段、`taibu:read` 和内网出口 |
| 调酒 Skill | `ClasspathSkillRepository` + `skillRepository` + `load_skill_through_path` | 发布资产指纹、`skill:use`、根预算；正文作为原生工具结果保存 |
| 市场研究 | 根 Agent 调用 `market_research`，子任务使用原生 `ReActAgent.call` | 固定目录，仅开放 `market_snapshot`；独立状态槽、根预算、根层澄清、实际工具来源 |
| 组合复盘 | `/workflow portfolio_review {JSON}`；原生工具循环/ASK/ConfirmResult | 固定步骤驱动器和 MyBatis 步骤账本，确定性分析，发布前人工审批 |

MCP 统一经 `McpClientBuilder` 与 `Toolkit.registration().mcpClient(...).enableTools(...)` 注册。SDK 负责握手、工具发现、Schema 和协议调用；业务薄适配继续做授权、预算及 Artifact 归档。没有自行维护的 MCP registry、Schema 指纹器或 list/call 循环。

每个 attempt 从服务端发现工具，直接采用原名和参数 Schema。新增必填项时，旧参数会被 SDK `ToolValidator` 拒绝；不再保留本地冻结 Schema 的逐次漂移比较或握手名称/版本固定值检查。协议版本、连接和请求由框架处理，入口仍固定 host/tenant/只读工具白名单。

2.0.3 的原生 MCP 内容转换使用 `content`，不独立返回 `structuredContent`。酒店/Taibu 结果需在文本 content 中包含 JSON；仅返回 structuredContent 的服务会失败，不能据本地模拟认定真实服务已兼容。远端错误和超时不会归档成功结果，业务调用返回固定错误码。

## 配置与试用

先按 [M1](m1-validation.md) 启动数据库、应用并创建会话。模型配置沿用 [M2](m2-validation.md)。默认 `offline` 只做确定性回复；自然语言选择业务工具与调酒生成需要配置模型。组合复盘的显式命令使用固定驱动器，不调用 LLM。

### 酒店和 Taibu

Java 默认关闭远端集成，避免迁移时自动读取旧项目密钥。启用酒店：

```bash
export FINANCECLAW_HOTEL_ENABLED=true
export FINANCECLAW_HOTEL_CREDENTIAL_REF=ROLLINGGO_TOKEN
# ROLLINGGO_TOKEN 从本地环境/密钥管理注入，发布记录只保存变量名
export FINANCECLAW_HOTEL_TENANTS=local
```

默认 URL 为 `https://mcp.rollinggo.cn/mcp`，allowlist 为 `mcp.rollinggo.cn`。自定义端口须将 `host:port` 精确放入 allowlist。HTTP 重定向关闭；外网服务要求 HTTPS。仅允许三个已声明的酒店查询工具。

Taibu 使用独立开关和配置，不依赖酒店：

```bash
export FINANCECLAW_TAIBU_ENABLED=true
export FINANCECLAW_TAIBU_URL=http://taibu-mcp:3001/mcp
export FINANCECLAW_TAIBU_ALLOWED_HOSTS=taibu-mcp:3001
export FINANCECLAW_TAIBU_EXTERNAL=false
```

内网配置要求主机解析为内网或回环地址。Taibu 只允许内网；调用还检查 tenant allowlist 和 `taibu:read`。参数完全采用服务端命名：黄历传入明确的 `date`（YYYY-MM-DD），不再自建 `day_offset` 参数；八字使用 `calendarType`、`birthYear`、`birthMonth`、`birthDay`、`birthHour`、`birthMinute`、`gender` 等原生字段。分钟和历法必须明确，农历须明确 `isLeapMonth`。时间口径和经度含义遵循部署的 Taibu 服务约定，不再维护一套本地别名协议。

`FINANCECLAW_HOTEL_CONTRACT` / `FINANCECLAW_TAIBU_CONTRACT` 以及握手身份配置已删除。旧合同 JSON 仅保留在测试资源中，供模拟服务发布 Schema。禁用集成不解析凭据或初始化连接。`FINANCECLAW_ZIWEI_ENABLED=true` 会明确拒绝启动。

### Skill 与 Artifact

向 Turn API 发送：

```json
{"message":"/skill cocktail-from-what-i-have 我有金酒、柠檬、苏打水，没有摇壶。做两杯清爽的酒，再给一个无酒精版本。"}
```

具有 `skill:use` 的根 Agent 直接挂载内置 classpath repository，由框架在系统提示中列出技能目录。上面的 `/skill` 是向模型表达选择意图的文本，也可以直接用自然语言请求；没有单独的命令解析器或激活状态机。默认离线回复不会主动加载 Skill。

模型通过原生 `load_skill_through_path` 传入目录中的 `skillId` 和 `path`：`SKILL.md` 加载正文，`SOURCE.json` 等路径读取附属资源。正文与资源以框架 ToolResult 进入 AgentState，审批后新 attempt 从 checkpoint 恢复这些结果；不再声称正文只存在于临时请求。目录和完整请求仍纳入供应商请求预算，SDK 自己校验资源路径。没有 `load_skill` / `read_skill_resource` 或 Skill 引用持久层，也不额外限制并行工具批次。为兼容已初始化的 M3 数据库，V4 迁移及旧 `skill_refs` 表保留，应用不再读写，不执行删表。

每次原生 Skill 工具执行都由 `onActing` 复查授权并消耗根工具预算；没有权限的新任务不挂载仓库。授权撤销会阻断旧任务恢复。Skill 不提供额外工具权限；代码执行关闭，未启用在线安装、自动学习。未来 M4 的记忆抽取仍须将 ToolResult 与用户事实区分。

酒店大结果保存在 Artifact 中。`read_artifact` 和 `GET /v1/artifacts/{id}` 支持可选 RFC 6901 JSON Pointer `path`，如 `/hotels`；选中数组后按 `start/limit` 分页（最多 100 条、16 KiB）。对象不使用分页但仍受 16 KiB 限制。读取同时检查所有者、`artifact:read` 和来源 scope；撤销 `travel:read`或 `taibu:read` 后不能继续绕过原权限读取。

### 市场研究与组合复盘

设置 `FINANCECLAW_MARKET_FILE=/绝对路径/market.json`。文件作为发布资产读取，示例结构如下；**价格、来源和时间必须替换为实际取得的快照**，不能把示例时间改成“现在”来冒充新数据：

```json
{
  "AAPL": {"price":"100.00","currency":"USD","provider":"synthetic-example","as_of":"2026-09-02T00:00:00Z"},
  "MSFT": {"price":"200.00","currency":"USD","provider":"synthetic-example","as_of":"2026-09-02T00:00:00Z"}
}
```

未配置文件或缺少代码时返回 `market_snapshot_unavailable`。市场研究入参为 `task`（最多 4000 字符）、`symbols`（最多 10 个）、`analysis_period`。缺少范围时创建根 Turn 的 input Interaction，用户补齐后保留原任务重新执行子研究。子 Agent 只可查询指定证券，最多 4 次模型调用和 10 次内部工具调用，同时消耗根 Turn 预算。模型答案必须有实际工具证据；来源和时间由服务端绑定，不信任模型自报。子 Agent 不直接投递用户消息、不转发内部正文和工具事件。

组合复盘通过 Turn 的 message 字段发送以下命令：

```text
/workflow portfolio_review {"portfolio_name":"我的组合","positions":[{"symbol":"AAPL","quantity":"1","cost_basis":"90"},{"symbol":"MSFT","quantity":"3","cost_basis":"150"}],"max_snapshot_age_hours":48}
```

固定顺序为 `normalize_input → snapshot:<symbol> → validate_freshness → analyze_exposure → publication_approval → publish_report`。快照过旧、未来时点异常、混合币种、重复代码、精度不合法均失败；不会调用模型猜价格或跳过审批。集中度使用 BigDecimal，50% / 30% 阈值及 HALF_EVEN 舍入与 Python 规则一致。

到审批点后 Turn 为 `waiting`，按 [M2 交互 API](m2-validation.md) 提交 `approve/reject`；重复相同回复只产生一个 resume。发布结果含报告 Artifact 引用。步骤键、输入 hash、结果和恢复位置存入 PostgreSQL；在两个独立 JVM 间完成挂起、恢复与一次发布已有测试。当前只支持同步子研究，不宣称支持后台多 Agent 编排。

## 可复现验证

```bash
# M3 契约与业务集成：酒店 / 调酒 / 子 Agent 展示及固定 Workflow
scripts/mvn.sh -B -ntp -Dtest=BusinessContractsTest -Dit.test=BusinessIT,NativeSkillsIT verify

# 全量 M0–M3，包含故障恢复与旧功能回归
scripts/mvn.sh -B -ntp clean verify spotless:check
python3 scripts/collect-evidence.py
```

测试入口：[契约与规则](../../src/test/java/com/financeclaw/m3/BusinessContractsTest.java)、[业务集成](../../src/test/java/com/financeclaw/m3/BusinessIT.java)、[独立 JVM](../../src/test/java/com/financeclaw/m3/ForkedBusiness.java)、[原生 Skill 打包与预算](../../src/test/java/com/financeclaw/m3/NativeSkillsIT.java)。酒店展示使用 800 条模拟结果验证分页；调酒展示检查原生渐进加载、工具批次、资源路径和审批状态恢复；研究展示检查原生子 Agent、实际来源、根澄清和预算失败。额外通过独立 JVM 从可执行 Spring Boot JAR 加载原生 Skill 仓库，并验证加载后的正文计入实际供应商请求预算。测试不依赖真实凭据，不验证真实模型的调酒质量或金融推理质量。

本轮原生 Skill/MCP 改造后完整回归 **111 项通过**（M0 28、M1 23、M2 32、M3 28），0 失败、0 错误、0 跳过。M3 包括 8 项单元规则测试、18 项 PostgreSQL/SDK 业务集成测试，以及 2 项原生 Skill 打包和模型预算集成测试。最终执行 `spotless:apply clean verify spotless:check`，2026-10-15 09:50（Asia/Shanghai）成功。

机器证据位于 `target/m3/results.json`，归档见 [M3 测试结果](evidence/m3-results.json) 和 [独立进程退出记录](evidence/m3-process-exits.json)。源码、配置、模拟 Schema、Skill 原文和测试样本一并计算指纹。

## 交付边界

- M3 的本地能力与模拟集成已实现；远端服务真实性、实时行情、真实模型质量和飞书验收仍属 M5。
- 紫微算法与五工具协议未迁移，本阶段关闭；M4 记忆与飞书尚未实现。
- Release 仍为进程内不可变目录；配置变更后旧版本未装载时明确阻断旧任务。M3 不提供跨版本热升级恢复。
- Artifact 保存在 PostgreSQL，单项最多 2 MiB；大型对象须按 `path` 选取有界子对象/数组，未实现无限下载。
- Skill 的 [SOURCE](../../src/main/resources/skills/cocktail-from-what-i-have/SOURCE.json)、[MIT LICENSE](../../src/main/resources/skills/cocktail-from-what-i-have/LICENSE) 和原文保持一致。资产来源与哈希见 [M3 资产基线](baseline/m3-assets.json)。
