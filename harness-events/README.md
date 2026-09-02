# harness-events

Provider Fabric 的最小进程内事件边界，包含 candidates、selected、retrying、fallback、failed。
事件供 Audit、Metrics、UI 和集成订阅使用，是 best-effort 观察面，不承担 LangGraph checkpoint
或执行恢复职责。
