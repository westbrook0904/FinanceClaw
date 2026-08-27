# harness-registry

`harness-registry` 维护当前可调用 Capability，为 Runtime 提供与插件实现解耦的查询
和唯一解析，并向 Planning 层提供不暴露 Provider 的只读 Catalog。

## 公共 API

- `CapabilityRegistry`：`register()`、`unregister()`、`get()`、
  `list()`、`resolve()` 抽象。
- `InMemoryCapabilityRegistry`：线程安全的单进程实现。
- `CapabilityQuery`：按 Capability ID、类型、标签、版本和 Plugin ID 过滤，所有条件
  采用 AND 语义。
- `ResolvedCapability`：Descriptor、所属 `plugin_id` 和实际 Provider。
- `CapabilityCatalog`：只读 Descriptor 目录抽象。
- `RegistryCapabilityCatalog`：Registry 到 Catalog 的实时投影视图。

## 存储与查询语义

第二阶段继续使用单 Provider 主索引：

```text
capability_id → ResolvedCapability(descriptor, plugin_id, provider)
```

Runtime 的调用目标是 Capability，因此 Capability ID 而不是 Plugin ID 是主键。同一
Plugin 可以注册多个不同 Capability；`plugin_id` 用于来源过滤和注销所有权校验。

- `get(id)` 不存在时返回 `None`。
- `list(query)` 返回按 Capability ID 排序的稳定快照。
- `resolve(query)` 要求唯一匹配；无匹配或多匹配时抛出 `RegistryError`。
- 重复 Capability ID 会被拒绝。
- `unregister(..., plugin_id=...)` 防止插件注销其他插件的能力。
- 所有共享 Map 读写由 `RLock` 保护。

`RegistryCapabilityCatalog` 的 `get/list` 只返回
`CapabilityDescriptor`，PlanValidator 或未来 Planner 无法由此取得 Provider instance。

## 依赖边界与当前范围

本模块只依赖 `harness-contracts` 和 `harness-spi`。Registry 回答“有哪些能力”，
不负责插件发现、生命周期、Policy 或业务调用，也不是通用 Service Locator。

第二阶段不处理分布式注册、健康检查、负载均衡或同一 Capability 的多 Provider 选择。
后续应通过 Provider Selector 演进，而不是让 Runtime 依赖具体插件。

## 测试

```bash
.venv/bin/python -m pytest harness-registry/tests -v
```
