# harness-routing

`harness-routing` 是 FinanceClaw 的无执行权路由决策层。当前提供 Router SPI、受限
Request 投影、确定性 RuleRouter、ModelGateway 驱动的 LLMRouter 和独立
RouteDecisionValidator；它只产生并校验 `RouteDecision`，不调用 Capability、Provider、
ExecutionEngine 或 StateStore。

## 公共 API

- `Router`：异步路由 SPI，只返回结构化 `RouteDecision`。
- `SafeRequestProjector`：仅保留 allowlist metadata，并限制 JSON 深度、集合大小、字符串
  长度与总值数量。
- `RoutingContext`：包含 Invocation、RequestSummary、请求模式、capability-only Catalog
  snapshot 和类型化 Route Policy constraints。
- `InputTypeRouteRule` / `RuleRouter`：按固定优先级执行显式模式、target、input type 和
  fallback Router 规则。
- `LLMRouter`：把安全 RequestSummary、Capability-only Catalog 和收紧后的可选范围发送给
  逻辑 Model Capability，解析结构化 RouteDecision 后再次执行独立校验。
- `RouteDecisionValidator`：在 Harness 分派前重新校验 schema、固定模式、Catalog、
  Policy 约束、3B 模式可用性和显式 target 一致性。

## RuleRouter 顺序

```text
EXPLORE / HYBRID request（产生协议决策，Validator 在 3B fail-closed）
  ↓
AUTO / FAST + explicit target → FAST
  ↓
PLAN → GENERATED_PLAN（只选择模式，不选择 Planner）
  ↓
FAST + matching FAST input-type rule
  ↓
AUTO + matching FAST / PLAN input-type rule
  ↓
fallback Router，或 HARNESS.ROUTE.NO_MATCH
```

Fallback 异常会直接传播；RuleRouter 不会在 fallback 失败后反向猜测模式或 Capability。

## LLMRouter 安全边界

LLMRouter 只通过 `ModelGateway` 调用逻辑 Model Capability，不依赖厂商 SDK。Prompt 不包含
Identity、Tenant attributes、Trace baggage、Plugin pin、Provider identity 或执行状态；MODEL
类型 Capability 也不会进入 FAST 候选目录。

模型输出必须满足 `RouteDecision` JSON Schema，顶层未知字段（包括 `planner_id`、
`provider_id`、`plugin_id`）会被拒绝，`source` 必须为 `model`，并继续经过
RouteDecisionValidator。PLAN Decision 只表达“需要规划”；Planner 由 Bootstrap/Coordinator 的
受信任配置和 Policy 约束选择。模型失败只映射安全 cause code，不回传原始响应或 Provider
details。Provider retry/fallback 仍完全由 ModelGateway 负责。

RequestCoordinator 在 Router 外层创建 ROUTE span。RuleRouter 或 LLMRouter 都使用同一安全
观察边界；调用 LLMRouter 时，RoutingContext 传播 ROUTE TraceContext，因此 ModelGateway 的
MODEL span 位于 ROUTE 子树。Trace 只记录模式、来源、短 reason code，以及 RequestSummary 和
Catalog snapshot 的稳定 hash，不记录投影原文或模型响应。

## 依赖边界

本模块依赖 `harness-contracts` 的稳定协议，并允许 LLMRouter 依赖 `harness-model` 的
ModelGateway。RoutingContext 只接收
`CapabilityDescriptor` snapshot，不接收 `ProviderRegistration` 或 Provider instance；本模块
不依赖 `harness-runtime`、`harness-execution`、业务插件或厂商模型 SDK。

## 测试

```bash
.venv/bin/python -m pytest harness-routing/tests -v
.venv/bin/python -m pytest tests/stage3b/test_rule_routing.py \
  tests/stage3b/test_llm_routing.py tests/stage3b/test_policy.py -v
```
