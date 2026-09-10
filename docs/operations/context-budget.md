# 上下文容量配置

当前默认模型为 DeepSeek V4 Pro，官方提供 [1M 上下文](https://api-docs.deepseek.com/quick_start/pricing/)。本项目将上下文规划上限设为 **800,000 token**；这是可用容量，不要求每次请求都填满。

| 配置 | 当前默认 | 用途 |
|---|---:|---|
| `CONTEXT_INPUT_LIMIT` | 800,000 | 上下文规划总上限，计算时包含下面的预留；沿用现有配置名 |
| `MODEL_MAX_TOKENS` | 32,768 | 实际发送给模型供应商的最大生成 token 数 |
| `MODEL_TIMEOUT_SECONDS` | 300 秒 | 给长上下文预处理与生成留出时间，原默认 60 秒 |
| `CONTEXT_RESERVED_OUTPUT` | 32,768 | 为上述生成预留空间，不能小于最大生成量 |
| `CONTEXT_SYSTEM_POLICY_RESERVE` | 8,192 | 系统提示预留 |
| `CONTEXT_TOOL_SCHEMA_RESERVE` | 32,768 | 工具 Schema 预留 |
| `CONTEXT_SAFETY_MARGIN` | 32,768 | 消息封装与 token 估算余量 |
| `CONTEXT_RECENT_MESSAGES` | 64 | 最近历史原文消息数，约 32 轮问答 |
| `CONTEXT_RELEVANT_MESSAGES` | 16 | 窗口外按相关性补充的历史原文数 |
| `CONTEXT_RELEVANT_SUMMARIES` | 8 | 按相关性召回的摘要数 |

所有环境变量加 `FINANCECLAW_` 前缀。扣除全部预留后，正文与历史可用 **693,504 token**；如果实际系统提示或工具 Schema 超过预留，超出部分继续从该额度扣除。当前轮次的完整执行上下文优先，随后选择最近原文、摘要和相关旧消息。历史原文窗口从固定 12 条改为可配置的默认 64 条。

此前本机有效上限为 122,768 token，仓库旧默认仅 32,768；本次统一扩大上限和真正影响历史选择的条数，同时将生成上限从 4,096 调到 32,768。保留摘要、相关性选择、制品外置和受保护结果不可截断的规则。扩大容量不会自动重发旧请求，也不改写永久 Journal。

根模型和紫微 Worker 共用通过 `FinanceClawSettings.context_budget` 装配的 `ContextBudget`，关闭历史持久化时也保持同一预算；上下文策略整体进入根发布指纹。紫微 Worker 的完整提示计数已包含系统/工具，使用 `model_request_limit`，即 800,000 减输出预留和安全余量的上限，避免二次扣除系统/工具内容。BFF 和 Agent Server 必须使用相同配置并一起重启；已有任务的冻结发布不随热更新改变。

本地和生产配置模板已给出完整参数：

- [local-bff.env.example](../../config/environments/local-bff.env.example)
- [local-agent-server.env.example](../../config/environments/local-agent-server.env.example)
- [production.env.example](../../config/environments/production.env.example)

如果部署环境已经显式设置对应变量，它会覆盖代码默认值。更换容量较小的模型或 fallback 时，应按所有实际可调用模型的容量调整该配置；当前计数器使用通用 tokenizer/字节估算，不是供应商精确计量接口。

验证包括：实际持久化 40 轮长历史后保留最近 64 条完整原文，输入量超过此前 122,768 上限且仍满足新预算；拒绝输出预留不足/输入余量不足；确认上下文窗口变化进入两端共享的发布指纹。测试不调用真实收费模型。

分词器只读取本地 `cl100k_base` 缓存，缓存缺失时按 UTF-8 字节数保守估算。同一段文本在这两种方式下占用的预算不同，64 条是历史条数上限，超出预算时仍会减少入选消息。设置 `TIKTOKEN_CACHE_DIR=""` 可明确禁用缓存；CI 的基础和紫微测试都使用该路径，容量回归同时覆盖本机缓存配置与显式禁用缓存的场景。
