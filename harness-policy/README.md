# harness-policy

`harness-policy` 在 Plan 和 Capability 两个受控边界运行独立策略链，产生结构化
`ALLOW`、`DENY` 或 `REQUIRE_APPROVAL` 决策。Policy 只做判断，不调用 Provider，
也不自行等待人工审批。

## 公共 API

- `Policy.evaluate(PolicyContext) -> PolicyDecision`：同步扩展接口。
- `Policy.phases`：声明策略参与的阶段；为兼容阶段一策略，默认仅
  `PRE_EXECUTE`。
- `PolicyContext`：可信 InvocationContext，以及当前 Plan 或已解析
  CapabilityDescriptor/ProviderDescriptor；PRE_EXECUTE 可携带持久化 `ApprovalGrant`。
- `PolicyDecision`：effect、策略名、原因和不可变 constraints。
- `PolicyEngine`：按当前 phase 顺序执行并聚合决策。
- `PolicyPhase`：`PRE_PLAN`、`PRE_EXECUTE`。
- `PolicyEffect`：`ALLOW`、`DENY`、`REQUIRE_APPROVAL`。

## 内置策略

- `AllowAllPolicy`：同时允许 PRE_PLAN 和 PRE_EXECUTE，默认用于开发与组合测试。
- `TenantPolicy`：校验 Request tenant 与 Runtime 注入的可信 TenantContext，支持
  必填和允许列表。
- `CapabilityPermissionPolicy`：按 Capability ID 或 `*` 规则检查可信 Identity
  scopes；未配置能力默认拒绝，可用 `allow_unconfigured=True` 放宽。
- `RequireApprovalPolicy`：按 Capability ID、`SideEffectType` 或 `EgressType`
  要求人工审批；匹配的 `ApprovalGrant` 可允许恢复后的同一节点继续执行。

`CapabilityPermissionPolicy` 的规则值是可接受 scope 集合：Identity 具备其中任一
scope，或具备全局 `*`，即通过；空集合表示该 Capability 不要求 scope。

## PolicyEngine 聚合语义

```text
ALLOW constraints ─┐
REQUIRE_APPROVAL ──┼─ constraints 按顺序合并
ALLOW constraints ─┘
         ↓
DENY 优先级最高并立即短路
         ↓
否则 REQUIRE_APPROVAL 优先于 ALLOW
```

同名 constraint 以后出现的值为准。当前 phase 没有适用策略时使用
`default_effect`，默认 ALLOW，也可配置 DENY 或 REQUIRE_APPROVAL。

## Approval 行为

- PRE_PLAN DENY 会在创建执行记录和调用 Provider 前阻止 Plan。
- Plan 节点的 PRE_EXECUTE REQUIRE_APPROVAL 会转成
  `WAITING(policy_approval)`，由 ExecutionEngine 生成安全的
  `ApprovalRequest`。
- 批准后，ExecutionEngine 从持久化状态注入匹配 `ApprovalGrant`，该节点重新经过
  PRE_EXECUTE，而不是绕过 Policy。
- Direct Invocation 没有 Plan/Node 恢复位置，遇到 REQUIRE_APPROVAL 会返回
  `HARNESS.POLICY.APPROVAL_REQUIRED` 的 DENIED 结果。

## 依赖边界

本模块只依赖 `harness-contracts`。Policy 不访问 Registry/Provider、不修改 Runtime
或 StateStore；Runtime/ExecutionEngine 在受控边界构造 PolicyContext。

PRE_ROUTE、POST_EXECUTE、限流、降级、复杂规则语言和策略持久化尚未实现。

## 测试

```bash
.venv/bin/python -m pytest harness-policy/tests -v
```
