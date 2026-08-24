# harness-contracts

## 职责

定义 Harness 各模块共享的稳定协议，包括 `Request`、`InvocationContext`、`ExecutionState`、`CapabilityDescriptor`、`ResultEnvelope` 与错误体系。

## 依赖边界

- 不依赖任何其他 Harness 模块或业务插件。
- 协议保持业务无关，不出现 Finance、SQL、RAG、LLM 等具体业务类型。
- `Request` 表示调用方输入，`InvocationContext` 表示只读执行环境，可变状态应单独建模。

## 阶段一非目标

不包含 Memory、持久化 Context、Streaming 或具体业务 DTO。

## 已冻结的公共 API

| 分类 | 类型 |
|---|---|
| 请求 | `Request`、`RequestInput`、`RequestTarget`、`RequestOptions` |
| 上下文 | `InvocationContext`、`IdentityContext`、`TenantContext`、`TraceContext`、`CancellationContext` |
| 执行状态 | `ExecutionState`、`ExecutionStatus` |
| 能力描述 | `CapabilityDescriptor`、`CapabilityType` |
| 结果 | `ResultEnvelope`、`ResultOutput`、`ResultStatus` |
| 错误 | `ErrorDetail`、`ErrorCode`、`HarnessError` 及六类模块异常 |

所有稳定类型都从 `harness_contracts` 顶层导出：

```python
from harness_contracts import Request, RequestInput, RequestTarget

request = Request(
    input=RequestInput(type="text", content="hello"),
    target=RequestTarget(capability="echo.reply/v1"),
)
```

## 设计约束

- 模型基于 Pydantic v2，默认 `extra="forbid"`，拼错或未协商字段会立即报错。
- Request、Context、Descriptor、Result 均为深度冻结模型，嵌套 `dict/list` 也不能原地修改；`ExecutionState` 是唯一明确可变的模型。
- 时间字段必须包含时区，序列化统一使用 `model_dump(mode="json")`。
- `ResultEnvelope` 保证成功时只有 Output，失败或拒绝时只有 Error。
- `target.capability` 在阶段一必须提供；空目标路由留到后续 Planner/Router 阶段。
- 包含 `py.typed` 标记，后续模块可直接获得公共类型的静态检查信息。

## 运行契约测试

直接运行测试文件：

```bash
.venv/bin/python harness-contracts/tests/test_contracts.py
```

通过 unittest discovery 运行：

```bash
PYTHONPATH=harness-contracts/src \
  .venv/bin/python -m unittest discover -s harness-contracts/tests -v
```

项目采用 `src` layout。未安装项目时，生产包不在 Python 默认搜索路径中；测试文件已添加仅用于本地直接执行的路径引导。正式运行应用或供其他模块使用时，仍建议在虚拟环境中安装项目。
