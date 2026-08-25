# harness-plugin-local

`harness-plugin-local` 回答“Capability 从哪里来”：发现本地 Plugin，执行最小生命周期，并把其 Provider 注册到 Registry。

## 公共 API

- `LocalPluginProvider`：从显式插件集合和 Python entry point 发现插件。
- `LocalPluginLoader`：提供 `discover()`、`load()`、`load_all()`、`unload()`、`shutdown()` 和 `loaded_plugins()`。
- `LoadedPlugin`：活动插件、Manifest 和 Capability ID 的只读快照。
- `PluginState`：`DISCOVERED`、`INITIALIZED`、`REGISTERED`、`ACTIVE`、`STOPPED` 生命周期状态。

默认 entry point 组为：

```text
financeclaw.plugins
```

Entry point 目标可以是 `PluginSPI` 实例、Plugin 类或返回 Plugin 的无参工厂。测试和嵌入场景可以使用 `LocalPluginProvider(plugins, entry_point_group=None)` 只启用显式发现。

## 加载生命周期

```text
discover
  ↓
validate manifest / descriptors / provider types
  ↓
initialize
  ↓
register every capability
  ↓
active
  ↓
unregister every capability
  ↓
shutdown / stopped
```

Loader 会验证：

- Manifest Capability 集合与 Provider Descriptor 完全一致；
- 每个 Provider 恰好实现 AgentSPI 或 ToolSPI 之一；
- Descriptor 的 `CapabilityType` 与 Provider SPI 类型一致；
- 同一发现批次没有重复 Plugin ID。

单插件加载具有事务语义：初始化或任一注册失败时，已注册能力按逆序注销，随后关闭插件。`load_all()` 对本次发现批次提供同样的整体回滚保证。

## 依赖边界

- 依赖 `harness-contracts`、`harness-spi` 和 Registry 公共接口。
- Loader 不实现能力查询、业务逻辑或 Runtime 调用。

## 测试

项目安装后运行：

```bash
.venv/bin/python -m unittest discover -s harness-plugin-local/tests -v
```

## 阶段一非目标

不实现远程、MCP、HTTP、容器插件 Provider，不支持热更新或 Marketplace。
