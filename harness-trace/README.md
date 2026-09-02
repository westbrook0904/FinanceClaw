# harness-trace

FinanceClaw 的轻量 Trace/Span 边界。当前 Direct Runtime 记录 request、runtime、policy、
capability、provider selection 与 provider invocation。历史 Span 枚举可为后续 LangChain
callback 和 LangGraph stream bridge 提供稳定映射，但 Trace 不是授权来源或 checkpoint。

适配层不得记录 Secret、完整 Prompt、隐藏推理或未脱敏 Provider 原始响应。
