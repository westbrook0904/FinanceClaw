# harness-contracts

## 职责

定义 Harness 各模块共享的稳定协议，包括 `Request`、`InvocationContext`、`ExecutionState`、`CapabilityDescriptor`、`ResultEnvelope` 与错误体系。

## 依赖边界

- 不依赖任何其他 Harness 模块或业务插件。
- 协议保持业务无关，不出现 Finance、SQL、RAG、LLM 等具体业务类型。
- `Request` 表示调用方输入，`InvocationContext` 表示只读执行环境，可变状态应单独建模。

## 阶段一非目标

不包含 Memory、持久化 Context、Streaming 或具体业务 DTO。
