# harness-bootstrap

FinanceClaw 领域核心的唯一 Composition Root。`build_harness()` 只组装 Registry、Selection、
Policy、Context、Memory、Trace、Events、Plugin Loader、CapabilityInvoker 与 Direct Runtime。

它刻意不创建模型、Router、Planner、Agent loop、Scheduler、图执行器或 checkpoint store。
LangChain/LangGraph 接入将在上层通过显式 Adapter 组合，不重新塞回 Bootstrap 隐式分支。

`HarnessApplication` 只暴露插件生命周期和 `invoke(request)`；`start`、`shutdown` 与异步上下文
管理器均保持幂等和 fail-closed。
