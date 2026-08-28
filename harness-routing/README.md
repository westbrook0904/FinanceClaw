# harness-routing

`harness-routing` 是 FinanceClaw 的无执行权路由决策层。当前 Stage 3B Step 2 提供
Router SPI、受限 Request 投影、确定性 RuleRouter 和独立 RouteDecisionValidator；它只
产生并校验 `RouteDecision`，不调用 Capability、Provider、ExecutionEngine 或 StateStore。

## 公共 API

- `Router`：异步路由 SPI，只返回结构化 `RouteDecision`。
- `SafeRequestProjector`：仅保留 allowlist metadata，并限制 JSON 深度、集合大小、字符串
  长度与总值数量。
- `RoutingContext`：包含 Invocation、RequestSummary、请求模式、capability-only Catalog
  snapshot 和类型化 Route Policy constraints。
- `InputTypeRouteRule` / `RuleRouter`：按固定优先级执行显式模式、target、input type 和
  fallback Router 规则。
- `RouteDecisionValidator`：在 Harness 分派前重新校验 schema、固定模式、Catalog、Planner、
  Policy 约束、3B 模式可用性和显式 target 一致性。

## RuleRouter 顺序

```text
EXPLORE / HYBRID request（产生协议决策，Validator 在 3B fail-closed）
  ↓
AUTO / FAST + explicit target → FAST
  ↓
PLAN → configured default planner
  ↓
FAST + matching FAST input-type rule
  ↓
AUTO + matching FAST / PLAN input-type rule
  ↓
fallback Router，或 HARNESS.ROUTE.NO_MATCH
```

Fallback 异常会直接传播；RuleRouter 不会在 fallback 失败后反向猜测模式或 Capability。

## 依赖边界

本模块依赖 `harness-contracts` 的稳定协议。RoutingContext 只接收
`CapabilityDescriptor` snapshot，不接收 `ProviderRegistration` 或 Provider instance；本模块
不依赖 `harness-runtime`、`harness-execution`、业务插件或厂商模型 SDK。

## 测试

```bash
.venv/bin/python -m pytest harness-routing/tests -v
```
