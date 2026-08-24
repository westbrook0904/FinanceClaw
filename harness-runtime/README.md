# harness-runtime

## 职责

协调单次 Invocation：接收 Request、创建 Context、启动 Trace、执行 Policy、解析 Capability、调用 Provider，并规范化 Result。

## 依赖边界

- 依赖 Contracts、SPI 以及 Registry、Policy、Trace 的公开抽象。
- 禁止导入 `plugins.*`、财经业务、SQL、RAG 或 LLM 具体实现。
- Runtime 保持轻薄，模块间协作由它统一协调。

## 阶段一非目标

不承担 Planner、Workflow、业务路由、多 Agent DAG、Memory 或持久化任务恢复。
