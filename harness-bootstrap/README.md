# harness-bootstrap

`harness-bootstrap` 是 FinanceClaw 阶段一的 **Composition Root**。它负责把 Registry、Policy、Trace、Local Plugin Loader、ContextFactory 和 Runtime 组装成一个可启动的 Harness 应用，但不承担任何业务执行逻辑。

## 职责

```text
build_harness()
    │
    ├── CapabilityRegistry
    ├── PolicyEngine
    ├── Tracer
    ├── InvocationContextFactory
    ├── LocalPluginProvider / LocalPluginLoader
    └── HarnessRuntime
            │
            ▼
      HarnessApplication
```

`build_harness()` 只做对象组装，不执行插件发现或初始化。真正的生命周期由：

```text
HarnessApplication.start()
HarnessApplication.invoke()
HarnessApplication.shutdown()
```

负责，也可以使用：

```python
async with build_harness(...) as app:
    result = await app.invoke(request)
```

## 默认阶段一实现

- Registry：`InMemoryCapabilityRegistry`
- Policy：`PolicyEngine((AllowAllPolicy(),))`
- Trace：`InMemoryTracer`
- Context：`DefaultInvocationContextFactory`
- Plugin 来源：`LocalPluginProvider`
- Runtime：`HarnessRuntime`

这些实现都可以从 Bootstrap 注入替换；底层模块不需要知道具体组装方式。

## 插件生命周期

```text
CREATED
   │ start()
   ▼
STARTED
   │ shutdown()
   ▼
STOPPED
```

- `start()` 负责发现、初始化并注册本地插件。
- 重复 `start()` 在已启动状态下是幂等的。
- `shutdown()` 负责注销 Capability 并关闭插件，重复调用是幂等的。
- `STOPPED` 应用不允许重新启动；需要重新组装一个新的 Application。
- 启动失败时依赖 `LocalPluginLoader` 的批次回滚语义，应用保持 `CREATED`。

## 依赖边界

Bootstrap 可以依赖所有阶段一 Harness 基础设施，因为它就是最外层组装点；其他核心模块不得反向依赖 `harness-bootstrap`。

阶段一仍然不在 Bootstrap 中实现：

- Planner / Supervisor
- Workflow / 多 Agent DAG
- 业务路由规则
- SQL / RAG / LLM 逻辑
- Remote Plugin / MCP
- 数据库持久化
