# harness-registry

`harness-registry` 维护 Capability 与 Provider 的 1:N 注册关系，为 Runtime/ModelGateway
提供候选集，并向 Routing/Planning 层提供不暴露 Provider 的只读 Capability Catalog。

## 公共 API

- `CapabilityRegistry`：`register()`、`unregister()`、`get()`、
  `list()`、`resolve()` 抽象。
- Provider API：`register_provider()`、`unregister_provider()`、`get_provider()`、
  `candidates()` 和 `list_providers()`。
- `InMemoryCapabilityRegistry`：线程安全的单进程实现。
- `CapabilityQuery`：按 Capability ID、类型、标签、版本和 Plugin ID 过滤，所有条件
  采用 AND 语义。
- `ProviderRegistration`：ProviderDescriptor、CapabilityDescriptor 与受信任实例；MODEL 注册时
  深拷贝 `ModelProviderFeatures`，并冻结 feature hash、registration version 和单进程
  provider incarnation。后者用于阻断热替换实例继续消费旧 prepared reservation，不是跨进程身份。
- `ResolvedCapability`：保留给旧单 Provider API 的兼容结果。
- `CapabilityCatalog`：只读 Descriptor 目录抽象。
- `RegistryCapabilityCatalog`：Registry 到 Catalog 的实时投影视图。

## 存储与查询语义

Stage 3A 使用 Provider 身份作为主索引，并按 Capability 建立候选索引：

```text
provider_id → ProviderRegistration
capability_id → [provider_id, ...]
```

调用方仍以逻辑 Capability ID 路由；Registry 只返回候选，不决定本次选择哪个 Provider。
同一 Capability 的所有 Provider 必须具有兼容的 type、version、schema 和 execution
profile，priority、region、tags、tenant visibility 等部署属性属于 ProviderDescriptor。

- `candidates(capability_id)` 返回按 `provider_id` 排序的稳定候选快照。
- `get(id)` 仅在候选唯一时返回兼容结果，多 Provider 时拒绝歧义。
- `list(query)` 返回按 Capability ID 排序的稳定快照。
- `resolve(query)` 要求唯一匹配；无匹配或多匹配时抛出 `RegistryError`。
- 重复 `provider_id` 会被拒绝；兼容 Provider 可共享 Capability ID。
- `unregister(..., plugin_id=...)` 防止插件注销其他插件的能力。
- 所有共享 Map 读写由 `RLock` 保护。

`RegistryCapabilityCatalog` 的 `get/list` 只返回
`CapabilityDescriptor`，Router、Planner 和 PlanValidator 无法由此取得 Provider instance。

## 依赖边界与当前范围

本模块只依赖 `harness-contracts` 和 `harness-spi`。Registry 回答“有哪些能力”，
不负责插件发现、生命周期、Policy 或业务调用，也不是通用 Service Locator。

Health 与选择位于 `harness-selection`，Retry/Fallback 位于 `harness-runtime`，模型原生调用
位于 `harness-model`。Registry 不编译具体 JSON Schema，也不信任请求 metadata 或模型输出
声明的 feature；ModelGateway 使用注册时快照做大类 eligibility，再调用 Provider 做无损编译。
Registry 不负责 Policy、健康探测、选择、执行或恢复。分布式注册、
租约和远程服务发现暂不支持。

## 测试

```bash
.venv/bin/python -m pytest harness-registry/tests -v
```
