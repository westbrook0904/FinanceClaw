# harness-registry

## 职责

维护可用 Capability，提供注册、注销、获取、列举和按查询条件解析 Provider 的能力。

## 依赖边界

- 只依赖 `harness-contracts` 与 `harness-spi`。
- Registry 回答“有哪些能力”，不负责发现能力来自哪里。
- Runtime 通过 `resolve(query)` 查询能力，不直接索取某个实现类。

## 阶段一非目标

仅规划内存实现，不处理分布式注册、健康检查、负载均衡或多版本路由。

## 公共接口

- `CapabilityRegistry`：Registry 抽象，提供 `register()`、`unregister()`、`get()`、`list()` 和 `resolve()`。
- `InMemoryCapabilityRegistry`：线程安全的阶段一内存实现，每个 Capability ID 只允许一个 Provider。
- `CapabilityQuery`：支持按 ID、类型、标签、版本及插件过滤，多个条件采用 AND 语义。
- `ResolvedCapability`：包含 Descriptor、Provider 和所属插件 ID。

`get()` 在目标不存在时返回 `None`；`resolve()` 要求查询结果唯一，无结果或结果不唯一时抛出 `RegistryError`。

## 运行测试

```bash
PYTHONPATH=harness-contracts/src:harness-spi/src:harness-registry/src \
  .venv/bin/python -m unittest discover -s harness-registry/tests -v
```
