# harness-routing

`harness-routing` 是 FinanceClaw 的无执行权路由决策层。当前提供 Router SPI、受限
Request 投影、确定性 RuleRouter、deterministic-first RoutingPipeline、strict LLMRouter 和独立
RouteDecisionValidator；它只产生并校验 `RouteDecision`，不调用 Capability、Provider、
ExecutionEngine 或 StateStore。

## 公共 API

- `Router`：异步路由 SPI，只返回结构化 `RouteDecision`。
- `SafeRequestProjector`：仅保留 allowlist metadata，并限制 JSON 深度、集合大小、字符串
  长度与总值数量。
- `RoutingContext`：包含 Invocation、RequestSummary、请求模式、capability-only Catalog
  snapshot、类型化 Route Policy constraints，以及配对的 ROUTE ContextProjection / ContextUseRecord。
- `InputTypeRouteRule` / `RuleRouter`：按固定优先级执行显式模式、target、input type 和
  兼容 fallback 规则；新装配优先使用独立 RoutingPipeline。
- `RoutingPipeline`：先调用确定性 Router；只有明确 `HARNESS.ROUTE.NO_MATCH` 才调用模型
  Router，静态 Policy/Schema/模式错误不会被重新解释为 no-match。
- `LLMRouter`：只把统一 PromptBuilder 从 ROUTE ContextProjection 生成的数据视图和收紧后的
  可选范围发送给逻辑 Model Capability，只解析当前仍未知字段，再由 Harness 物化最终
  RouteDecision。
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
HARNESS.ROUTE.NO_MATCH
```

当配置模型 fallback 时，推荐装配为：

```text
RoutingPipeline
  ├── RuleRouter → decision：立即返回，模型零调用
  ├── RuleRouter → HARNESS.ROUTE.NO_MATCH：调用 LLMRouter 一次
  └── RuleRouter → 其他错误：原样传播，模型零调用
```

`RuleRouter(fallback=...)` 作为 Stage 3B 兼容入口保留；Foundation F1 的规范装配是显式
`RoutingPipeline(RuleRouter(), LLMRouter(...))`。Pipeline 构造时拒绝内部已经配置 fallback 的
确定性 Router，避免隐藏的模型调用绕过唯一降级条件。

## LLMRouter 安全边界

LLMRouter 只通过公共 `StructuredGenerationAdapter` 调用逻辑 Model Capability，不依赖厂商
SDK。模型路径缺少 ROUTE ContextProjection 时会在 Gateway 调用前 fail-closed。Prompt 不包含 requested/effective mode、
Identity、Tenant attributes、Trace baggage、Plugin pin、Provider identity 或执行状态；MODEL
类型 Capability 也不会进入 FAST 候选目录。

route-v2 只使用 REQUIRED `StructuredOutputSpec`：AUTO 仍模糊时，模型可填写
`mode/capability_id/confidence/reason_code`；FAST 已由请求或 Policy 固定时，Schema 只允许
`capability_id/confidence/reason_code`。`source`、`route_type` 和所有已知字段由 Harness
物化；模型注入 `planner_id`、Provider、Plugin、metadata 或任何已知字段都会被 strict Schema
拒绝。显式 target、固定 PLAN 和单一 PLAN Policy 都是零模型调用。最终决策继续经过
RouteDecisionValidator；Planner 仍由 Bootstrap/Coordinator 的受信任配置和 Policy 选择。
模型失败只映射安全 cause code，不回传原始响应或 Provider details；Provider retry/fallback
仍完全由 ModelGateway 负责。

RequestCoordinator 在 Router 外层创建 ROUTE span。RuleRouter 或 LLMRouter 都使用同一安全
观察边界；调用 LLMRouter 时，RoutingContext 传播 ROUTE TraceContext，因此 ModelGateway 的
MODEL span 位于 ROUTE 子树。Trace 只记录模式、来源、短 reason code、RequestSummary/Catalog
hash，以及 Context snapshot/projection hash 与 included/omitted 数量，不记录投影原文或模型响应。

## 依赖边界

本模块依赖 `harness-contracts`、`harness-context` 的稳定投影，并允许 LLMRouter 依赖 `harness-model` 的
StructuredGenerationAdapter / ModelGateway。RoutingContext 只接收
`CapabilityDescriptor` snapshot，不接收 `ProviderRegistration` 或 Provider instance；本模块
不依赖 `harness-runtime`、`harness-execution`、业务插件或厂商模型 SDK。

## 测试

```bash
.venv/bin/python -m pytest harness-routing/tests -v
.venv/bin/python -m pytest tests/stage3b/test_rule_routing.py \
  tests/stage3b/test_llm_routing.py tests/stage3b/test_policy.py -v
```
