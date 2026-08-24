# harness-registry

## 职责

维护可用 Capability，提供注册、注销、获取、列举和按查询条件解析 Provider 的能力。

## 依赖边界

- 只依赖 `harness-contracts` 与 `harness-spi`。
- Registry 回答“有哪些能力”，不负责发现能力来自哪里。
- Runtime 通过 `resolve(query)` 查询能力，不直接索取某个实现类。

## 阶段一非目标

仅规划内存实现，不处理分布式注册、健康检查、负载均衡或多版本路由。
