# 统一模型配置

本文用于配置根 Agent、子 Agent、摘要和后台记忆任务的聊天模型。首次部署先完成
[本地完整链路](local-full-stack.md)，再按本页配置供应商；向量 Embedding 使用独立配置。

所有聊天模型的服务地址、模型 ID、生成参数、容量和降级候选都在
[config/models.toml](../../config/models.toml) 中声明。
默认读取这个文件，也可在 `.env` 和 `.env.memory` 中设置同一个配置路径：

```dotenv
FINANCECLAW_MODEL_CONFIG_PATH=config/models.toml
```

TOML 不保存密钥。`api_key_env` 的值是**环境变量名**，例如 `FINANCECLAW_QWEN_API_KEY`，
不是 `sk-...` 密钥本身。密钥由进程环境、本地 dotenv 或部署 Secret Manager 提供。
环境变量优先于 dotenv；代码显式指定 `_env_file=None` 时不读取 dotenv。

## 默认模型与按用途覆盖

当前仓库的绑定如下；这是本地配置声明，不代表服务商已验证这些模型和容量可用。

| 用途 | 模型别名 | 当前模型声明 |
| --- | --- | --- |
| 根 Agent `finance_agent` | `root-main` | `openai:qwen3.8-flash` |
| 紫微 `ziwei_doushu_agent` | `ziwei-main` | `openai:qwen3.8-flash` |
| 市场 Agent、摘要、记忆提取/整理 | 默认 `ziwei-main` | `openai:qwen3.8-flash` |

两个别名目前均引用 `qwen` 供应商，密钥变量为 `FINANCECLAW_QWEN_API_KEY`，声明的总窗口为
131072 tokens、输出上限为 32768 tokens。`deepseek` 供应商虽已登记，目前没有模型别名使用它，
因此不需要仅因它出现在文件中就配置密钥。实际有效预算还取决于下文的任务和上下文限制。

新增供应商或模型时，按下面结构填写；请把占位值替换为实际服务配置：

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

多个用途共享同一个模型别名或供应商时，修改该声明会影响所有引用方。
只改根 Agent 的绑定，不会修改 Ziwei 的绑定；若后台任务也不应随默认值变化，就为它们显式指定别名。

## 调用参数与任务预算

模型参数沿用现有 `ModelProfile` 和 LangChain 模型工厂。当前接入 OpenAI Chat Completions
兼容服务，`openai:` 是协议适配器，后面是服务商实际接受的模型 ID。
当前工厂对 `openai:deepseek-*` 显式发送 `thinking.type=disabled`；工具往返尚未接入
`reasoning_content` 完整回传。

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
Embedding 是独立的向量化配置，`EMBEDDING_*` 不在本页的 TOML 中配置。

## 进程与发布

API/图 Worker 和独立记忆 Worker 必须读取同一份 TOML。API 只读取模型声明；
图执行端读取 Agent/摘要所需凭据，记忆 Worker 只读取提取/整理实际使用的凭据。
未使用的模型可以先登记，不需要提供密钥或放行端点。每个进程只需放行自己的供应商域名。
独立记忆进程通过 Compose 的 `.env.memory` 注入密钥，不应复制整份应用环境文件。

模型/端点变化进入相关发布指纹，密钥值不会进入模型档案、发布声明或模型 tracing metadata。
记忆任务还会冻结用途、输出预算和处理权限；端点变化会改变其发布身份，密钥轮换不会。

配置在进程启动时加载，不支持热更新。镜像已包含 `config/*.toml`，修改后需要重新构建并统一重启：

```bash
# 完成环境准备、处理旧任务后部署；会准备 MCP 定义、构建并启动服务。
.venv/bin/python scripts/deploy.py
curl --fail-with-body -sS http://127.0.0.1:8000/v1/health/ready
```

部署前结束旧的执行/澄清轮次。已排队的旧记忆任务仍按既有指纹核对和运维重放流程处理，
不会自动换模型继续执行。部署命令返回后仍需确认各进程健康，不能仅以构建成功判定模型可用。

## 本地检查与故障定位

只解析 TOML、校验别名和模型档案，不读取凭据也不调用服务商：

```bash
.venv/bin/python - <<'PYTHON'
from financeclaw.shared.llm.configuration import ModelConfiguration

configuration = ModelConfiguration.from_file("config/models.toml")
profiles = configuration.profiles()
print(f"模型配置有效：{len(profiles)} 个模型，默认别名 {configuration.defaults.model}")
PYTHON
```

| 现象 | 检查顺序 |
| --- | --- |
| 启动报未知别名、循环 fallback 或容量错误 | 先运行上面的离线检查，再检查引用和声明 |
| 执行端提示缺密钥 | 查看所选别名的 `provider → api_key_env`，确认凭据注入的是图 Worker 或记忆 Worker 对应环境 |
| 端点被出站策略拒绝 | 检查执行进程的供应商域名 allowlist，参见[完整链路](local-full-stack.md) |
| HTTP 401/403 或模型不存在 | 核对供应商地址、账户权限和真实模型 ID；离线解析成功不能验证它们 |
| `LengthFinishReasonError` | 输出被截断；记忆任务同时查看思考用量、输出上限与失败历史 |
| `ModelBudgetExhausted` | 持久化尝试预算耗尽；结合 `failure_history`、`model_usage`、`model_budget` 查找最早原因 |
| 上下文超限 | 按[上下文预算](context-budget.md)缩小输入或调整经验证的容量，不只提高 `max_tokens` |

对应回归测试是 `tests/stage1/test_model_configuration.py` 与
`tests/stage1/test_models_settings_architecture.py`。配置测试与模拟模型测试不能代替真实供应商调用、
恢复流程及飞书输出验收。

## 源码入口

| 入口 | 负责什么 |
| --- | --- |
| [configuration.py](../../financeclaw/shared/llm/configuration.py) | 读取 TOML、校验引用、按用途解析模型别名 |
| [factory.py](../../financeclaw/shared/llm/factory.py) | 按执行用途读取凭据，构建 LangChain 模型，设置供应商参数 |
| [memory_profiles.py](../../financeclaw/shared/llm/memory_profiles.py) | 派生受输出上限和数据许可约束的记忆模型档案 |
| [catalog.py](../../financeclaw/shared/releases/catalog.py) | 将模型声明绑定到固定 Agent 发布 |
