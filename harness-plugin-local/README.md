# harness-plugin-local

## 职责

发现本地 Plugin，执行最小生命周期，并把插件声明的 Capability 注册到 Registry。

## 依赖边界

- 依赖 `harness-contracts`、`harness-spi` 和 Registry 的公开接口。
- Loader 回答“能力从哪里来”，Registry 回答“有哪些能力”。
- 不在加载器中实现业务能力。

## 阶段一非目标

不实现远程、MCP、HTTP、容器插件 Provider，不支持热更新或 Marketplace。

## 公共接口

- `LocalPluginProvider`：从显式插件集合及 `financeclaw.plugins` Python entry point 发现本地插件。
- `LocalPluginLoader`：负责校验、初始化、注册、注销和关闭，并提供 `load()`、`load_all()`、`unload()` 与 `shutdown()`。
- `LoadedPlugin`、`PluginState`：描述 Loader 中插件的活动或停止状态。

单个插件加载具有事务语义：初始化或任一 Capability 注册失败时，已经注册的能力会被注销，插件也会关闭。`load_all()` 对本次发现批次提供同样的回滚保证。

## 运行测试

```bash
PYTHONPATH=harness-contracts/src:harness-spi/src:harness-registry/src:harness-plugin-local/src \
  .venv/bin/python -m unittest discover -s harness-plugin-local/tests -v
```
