# 统一模型配置

所有聊天模型的服务地址、模型 ID、生成参数、容量和降级候选都在 `config/models.toml` 中声明。
默认读取这个文件，也可在 `.env` 和 `.env.memory` 中设置同一个配置路径：

```dotenv
FINANCECLAW_MODEL_CONFIG_PATH=config/models.toml
```

TOML 不保存密钥。`api_key_env` 的值是**环境变量名**，例如 `FINANCECLAW_QWEN_API_KEY`，
不是 `sk-...` 密钥本身。密钥由进程环境、本地 dotenv 或部署 Secret Manager 提供。
环境变量优先于 dotenv；代码显式指定 `_env_file=None` 时不读取 dotenv。

## 默认模型与按用途覆盖

```toml
[defaults]
model = "general"

[providers.main-provider]
base_url = "https://provider.example/v1"
api_key_env = "MAIN_PROVIDER_API_KEY"

[models.general]
provider = "main-provider"
model = "openai:your-chat-model-id"
temperature = 0
timeout_seconds = 300
max_tokens = 8192
context_window_tokens = 131072
# max_input_tokens = 98304
# token_estimator = "cl100k_base-v1"
# fallbacks = ["backup-model-alias"]
```

以上为结构示例，地址、模型 ID 和容量需填真实值。只声明 `defaults.model` 就能让根 Agent、
市场 Agent、Ziwei、摘要、记忆提取和记忆整理共用一个模型。不需要逐个填写相同别名。

需要独立配置时，再登记相应模型别名，并增加可选覆盖：

```toml
[agents]
finance_agent = "root-main"
ziwei_doushu_agent = "ziwei-main"
# market_research_agent = "market-main"

[tasks]
summary = "summary-main"
memory_extraction = "memory-main"
memory_consolidation = "memory-main"
```

`agents`、`tasks` 整个表或其中任一条目都可以省略。省略项使用 `defaults.model`。
显式填写的别名不存在、任务名拼错或默认模型缺失会直接报错，不会静默切换。
不存在“后台任务先继承摘要、再继承主模型”的隐含链。

当前本地配置保留了你填写的根 Agent 和 Ziwei 模型；默认别名以文件中的 `defaults.model` 为准。
摘要、记忆任务与未覆盖的市场 Agent 使用该默认值。要单独调整后台模型，只需添加 `[tasks]` 覆盖。

多个用途共享同一个模型别名或供应商时，修改该声明会影响所有引用方。
只改根 Agent 的绑定，不会修改 Ziwei 的绑定；若后台任务也不应随默认值变化，就为它们显式指定别名。

## 调用参数与任务预算

模型参数沿用现有 `ModelProfile` 和 LangChain 模型工厂。当前接入 OpenAI Chat Completions
兼容服务，`openai:` 是协议适配器，后面是服务商实际接受的模型 ID。
DeepSeek 保持现有非思考模式，本次未加入 `reasoning_content`。

每个模型需显式声明总窗口 `context_window_tokens`。Agent 的输入/输出还受共享上下文策略限制。
记忆提取与整理分别沿用来源许可的 2000 / 4000 输出上限：实际输出上限为模型声明与任务上限的较小值。
因此默认模型输出较大也不会扩大记忆任务授权；更小的输出上限可直接在模型别名中声明。

Qwen 聊天别名可声明 `enable_thinking = true/false`，省略时保留供应商默认值。
记忆提取和整理派生的 Qwen 档案固定使用 `enable_thinking = false`，防止思考占满
2000 / 4000 token 后尚未生成 JSON。该开关进入记忆任务冻结指纹；不会改变聊天别名，
也不会放宽来源许可或恢复已耗尽的尝试次数。非 Qwen 模型不能声明这一专用参数。

SDK 因输出截断抛出 `LengthFinishReasonError` 时，也记录实际输入、输出与思考用量。
Outbox 的 `processing_metadata.failure_history` 保留最近八次异常类型，日志打印事件编号和
异常类型；不记录模型正文。`ModelBudgetExhausted` 表示持久化调用次数已耗尽，排查时应同时看
`failure_history`、`model_usage` 和 `model_budget`，不能仅据最后一条错误认定输入超长。

`fallbacks` 可跨供应商，按声明顺序展开、去重，循环引用会报错。
是否使用降级遵守各执行路径的既有策略：根/市场 Agent 可用，Ziwei、摘要和记忆任务不新增降级行为。

## 环境文件保留什么

以下旧模型声明已从 Settings 和执行路径移除，可以从 `.env` / `.env.memory` 删除：

- `FINANCECLAW_MODEL`、`FINANCECLAW_FALLBACK_MODELS`、`FINANCECLAW_PROVIDER_BASE_URL`
- `FINANCECLAW_MODEL_TIMEOUT_SECONDS`、`FINANCECLAW_MODEL_MAX_TOKENS`
- `FINANCECLAW_MODEL_CONTEXT_WINDOW_TOKENS`、`FINANCECLAW_MODEL_MAX_INPUT_TOKENS`
- `FINANCECLAW_MODEL_TOKEN_ESTIMATOR`、`FINANCECLAW_MODEL_CAPACITIES`
- `FINANCECLAW_SUMMARY_MODEL`、`FINANCECLAW_SUMMARY_MAX_TOKENS`
- `FINANCECLAW_SUMMARY_CONTEXT_WINDOW_TOKENS`、`FINANCECLAW_SUMMARY_MAX_INPUT_TOKENS`
- `FINANCECLAW_MEMORY_EXTRACTION_MODEL`、`FINANCECLAW_MEMORY_CONSOLIDATION_MODEL`
- `FINANCECLAW_MEMORY_MODEL_TIMEOUT_SECONDS`

模型相关的环境变量保留配置路径和 TOML 引用的密钥。密钥变量可自行命名：
如果 TOML 引用了 `FINANCECLAW_PROVIDER_API_KEY`，仍需保留这个变量，它不再代表硬编码默认供应商。
旧的模型声明不再生效，不会因删除 `PROVIDER_BASE_URL` 而回退到代码里的某个供应商地址。

离线开关、出站 allowlist、Agent 重试预算、上下文策略、记忆处理授权、数据库及其他服务配置仍保留在环境文件。
Embedding 是独立的向量化配置，本次统一的是聊天模型，`EMBEDDING_*` 不受影响。

## 进程与发布

API/图 Worker 和独立记忆 Worker 必须读取同一份 TOML。API 只读取模型声明；
图执行端读取 Agent/摘要所需凭据，记忆 Worker 只读取提取/整理实际使用的凭据。
未使用的模型可以先登记，不需要提供密钥或放行端点。每个进程只需放行自己的供应商域名。
独立记忆进程通过 Compose 的 `.env.memory` 注入密钥，不应复制整份应用环境文件。

模型/端点变化进入相关发布指纹，密钥值不会进入模型档案、发布声明或模型 tracing metadata。
记忆任务还会冻结用途、输出预算和处理权限；端点变化会改变其发布身份，密钥轮换不会。

配置在进程启动时加载，不支持热更新。镜像已包含 `config/*.toml`，修改后需要重新构建并统一重启：

```bash
# 当前任务保持服务停止；准备部署时再执行。
docker compose build api
docker compose up -d --no-build
```

部署前结束旧的执行/澄清轮次。已排队的旧记忆任务仍按既有指纹核对和运维重放流程处理，
不会自动换模型继续执行。本次不自动启动已停止的服务。
