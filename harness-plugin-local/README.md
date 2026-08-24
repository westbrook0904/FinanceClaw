# harness-plugin-local

## 职责

发现本地 Plugin，执行最小生命周期，并把插件声明的 Capability 注册到 Registry。

## 依赖边界

- 依赖 `harness-contracts`、`harness-spi` 和 Registry 的公开接口。
- Loader 回答“能力从哪里来”，Registry 回答“有哪些能力”。
- 不在加载器中实现业务能力。

## 阶段一非目标

不实现远程、MCP、HTTP、容器插件 Provider，不支持热更新或 Marketplace。
