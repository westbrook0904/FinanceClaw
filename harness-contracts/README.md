# harness-contracts

`harness-contracts` 是所有 Harness 模块共享的稳定、业务无关协议层，不依赖其他 Harness 模块或插件。

## 公共 API

| 分类 | 类型 |
|---|---|
| 请求 | `Request`、`RequestInput`、`RequestTarget`、`RequestOptions` |
| 计划 | `ExecutionPlan`、`PlanNode`、`PlanEdge`、Binding、Condition、Budget、Retry / Failure Policy |
| 上下文 | `InvocationContext`、`IdentityContext`、`TenantContext`、`TraceContext`、`CancellationContext` |
| 执行状态 | Direct Invocation、Plan 与 Node 的状态模型及状态枚举 |
| 能力 | `CapabilityDescriptor`、`CapabilityType`、`CapabilityExecutionProfile` |
| 审批 | `ApprovalRequest`、`ApprovalDecision` |
| 结果 | `ResultEnvelope`、`ResultOutput`、`ResultIssue`、`Continuation`、`ResultStatus` |
| 错误 | `ErrorDetail`、`ErrorCode`、`HarnessError` 及模块异常 |
| 基础类型 | `ContractModel`、`MutableContractModel`、`JsonPrimitive`、`JsonValue` |

所有公共类型均从 `harness_contracts` 顶层导入：

```python
from harness_contracts import Request, RequestInput, RequestTarget

request = Request(
    input=RequestInput(type="text", content="hello"),
    target=RequestTarget(capability="echo.reply/v1"),
)
```

## 协议约束

- Pydantic 模型默认 `extra="forbid"`，拒绝未协商字段。
- Request、Context、Descriptor 和 Result 深度冻结，嵌套 `dict/list` 也不能原地修改。
- Context、Plan 与 Result 深度冻结；Invocation、Plan 与 Node 的 State 模型明确可变。
- 时间字段必须包含时区。
- `Request.target` 对 Plan 请求可为空；Direct Invocation 仍必须由 Runtime 校验其存在。
- Request 中的 `user_id`、`tenant_id` 只是外部声明，不能直接视为可信 Identity/Tenant。
- `ResultEnvelope` 分别校验 SUCCESS、PARTIAL、FAILED、DENIED、CANCELLED、ACCEPTED 的载荷。
- `HarnessError.to_detail()` 把内部异常转换为可安全跨模块传播的结构化错误。

## 依赖边界

本模块不依赖任何其他 Harness 模块或业务插件，也不包含 Finance、SQL、RAG、LLM 等业务类型。

## 测试

项目安装后运行：

```bash
.venv/bin/python -m unittest discover -s harness-contracts/tests -v
```

Contracts 只冻结跨模块数据边界；DAG 拓扑校验、调度、持久化与恢复由后续模块负责。
