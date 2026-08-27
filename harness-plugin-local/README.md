# harness-plugin-local

`harness-plugin-local` 回答“Capability 从哪里来”：发现本地 Plugin，执行最小生命周期，
并把 Provider 注册到 Registry。第二阶段的 ExecutionEngine 复用相同 Registry，不改变
插件发现和生命周期边界。

## 公共 API

- `LocalPluginProvider`：从显式插件集合和 Python entry point 发现插件。
- `LocalPluginLoader`：`discover()`、`load()`、`load_all()`、
  `unload()`、`shutdown()` 和 `loaded_plugins()`。
- `LoadedPlugin`：活动插件、Manifest 和 Capability ID 的只读快照。
- `PluginState`：`DISCOVERED`、`INITIALIZED`、`REGISTERED`、
  `ACTIVE`、`STOPPED`。

默认 entry point 组为：

```text
financeclaw.plugins
```

Entry point 目标可以是 `PluginSPI` 实例、Plugin 类或返回 Plugin 的无参工厂。测试和
嵌入场景可使用 `LocalPluginProvider(plugins, entry_point_group=None)` 只启用显式发现。

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
- Provider 恰好实现 AgentSPI 或 ToolSPI 之一；
- Descriptor 的 CapabilityType 与 Provider SPI 类型一致；
- 同一发现批次没有重复 Plugin ID。

单插件加载具有事务语义：初始化或任一注册失败时，已注册能力按逆序注销，随后关闭插件。
`load_all()` 对整个发现批次提供相同的整体回滚保证。

## 依赖边界与当前范围

本模块依赖 `harness-contracts`、`harness-spi` 和 Registry 公共接口。Loader 不执行
Capability、不查询业务结果，也不参与 Plan、Policy、Trace、State 或 Runtime 调用。

远程/MCP/HTTP/容器 Plugin Provider、热更新和 Marketplace 不在第二阶段范围内。

## 测试

```bash
.venv/bin/python -m pytest harness-plugin-local/tests -v
```
