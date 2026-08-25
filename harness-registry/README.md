# harness-registry

`harness-registry` 维护当前可调用 Capability，并为 Runtime 提供与插件实现解耦的查询和解析接口。

## 公共 API

- `CapabilityRegistry`：`register()`、`unregister()`、`get()`、`list()`、`resolve()` 抽象。
- `InMemoryCapabilityRegistry`：线程安全的阶段一内存实现。
- `CapabilityQuery`：按 Capability ID、类型、标签、版本和 Plugin ID 过滤，条件采用 AND 语义。
- `ResolvedCapability`：包含 Descriptor、所属 `plugin_id` 和实际 Provider。

## 存储与查询语义

阶段一主索引是：

```text
capability_id → ResolvedCapability(descriptor, plugin_id, provider)
```

Runtime 的调用目标是 Capability，因此 Capability ID 而不是 Plugin ID 是主键。同一个 Plugin 可以注册多个不同 Capability；`plugin_id` 用于来源标记、过滤和注销所有权校验。

- `get(id)` 不存在时返回 `None`。
- `list(query)` 返回按 Capability ID 排序的稳定快照。
- `resolve(query)` 要求结果唯一；无匹配或多匹配时抛出 `RegistryError`。
- 同一 Capability ID 重复注册会被拒绝。
- `unregister(..., plugin_id=...)` 防止一个插件注销另一个插件的能力。

所有共享 Map 读写都由 `RLock` 保护。应用通常只组装一个 Registry 实例，但实例数量和生命周期由 Bootstrap 依赖注入控制，本类不强制全局单例。

## 依赖边界

- 只依赖 `harness-contracts` 和 `harness-spi`。
- Registry 回答“有哪些能力”，不负责插件发现或生命周期。
- Registry 不是面向业务代码的通用 Service Locator。

## 测试

项目安装后运行：

```bash
.venv/bin/python -m unittest discover -s harness-registry/tests -v
```

## 阶段一非目标

不处理分布式注册、健康检查、负载均衡或同一 Capability 的多 Provider 选择。后续支持多 Provider 时应引入 Provider Selector，而不是让 Runtime 依赖具体插件。
