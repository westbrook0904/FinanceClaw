# harness-policy

## 职责

通过独立 Policy 链在调用边界执行权限和约束判断，并返回结构化的允许或拒绝决策。

## 依赖边界

- 只依赖 `harness-contracts` 与 `harness-spi`。
- Policy 不写入 Runtime，也不由业务插件自行实现公共安全规则。
- 第一阶段重点保留 `PRE_EXECUTE` 决策扩展点。

## 阶段一实现

- `PolicyContext` / `PolicyDecision`
- `Policy` SPI
- `PolicyEngine` 顺序执行、ALLOW 约束合并、DENY 短路
- `AllowAllPolicy`
- `TenantPolicy`
- `CapabilityPermissionPolicy`

## 阶段一非目标

不实现审批、限流、降级、复杂规则语言或租户策略持久化。
