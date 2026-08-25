# harness-policy

`harness-policy` 在 Capability 执行边界运行独立策略链，并产生结构化允许或拒绝决策。业务插件不感知公共权限校验过程。

## 公共 API

- `Policy`：同步 `evaluate(PolicyContext) -> PolicyDecision` 扩展接口。
- `PolicyContext`：可信 InvocationContext、已解析 CapabilityDescriptor 和执行阶段。
- `PolicyDecision`：`ALLOW` / `DENY`、策略名、原因和不可变约束。
- `PolicyEngine`：顺序执行策略、合并约束并在首个 DENY 处短路。
- `PolicyPhase`：阶段一仅支持 `PRE_EXECUTE`。

## 内置策略

- `AllowAllPolicy`：显式允许调用，默认用于开发和组合测试。
- `TenantPolicy`：校验 Request tenant 与 Runtime 注入的可信 TenantContext，支持必填和允许列表。
- `CapabilityPermissionPolicy`：按 Capability ID 或 `*` 规则检查可信 Identity scopes；未配置能力默认拒绝，可通过 `allow_unconfigured=True` 放宽。

`CapabilityPermissionPolicy` 的规则值是可接受 scope 集合：Identity 具有其中至少一个 scope，或具有全局 `*` scope，即通过检查。空集合表示该 Capability 不要求 scope。

## PolicyEngine 语义

```text
Policy 1: ALLOW + constraints
  ↓ merge
Policy 2: ALLOW + constraints
  ↓ merge
Policy 3: DENY
  ↓ short-circuit
Final DENY + merged constraints
```

无策略时默认 ALLOW，也可以通过 `default_effect=DENY` 改为默认拒绝。后出现的同名约束覆盖之前的值。

## 依赖边界

- 只依赖 `harness-contracts` 和 `harness-spi`。
- Policy 不调用 Provider、不修改 Registry，也不写入 Runtime。
- Runtime 在 Registry 解析之后构造 PRE_EXECUTE PolicyContext。

## 测试

项目安装后运行：

```bash
.venv/bin/python -m unittest discover -s harness-policy/tests -v
```

## 阶段一非目标

不实现 PRE_ROUTE、POST_EXECUTE、审批、限流、降级、复杂规则语言或租户策略持久化。
