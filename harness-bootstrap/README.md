# harness-bootstrap

`harness-bootstrap` 是阶段一唯一的 Composition Root，负责组装具体基础设施实例并协调应用启动/关闭，不承担业务执行逻辑。

## 默认组装

```text
build_harness()
├── InMemoryCapabilityRegistry
├── PolicyEngine(AllowAllPolicy)
├── InMemoryTracer
├── DefaultInvocationContextFactory
├── LocalPluginProvider / LocalPluginLoader
└── HarnessRuntime
    ↓
HarnessApplication
```

`build_harness()` 只创建和连接对象，不发现或初始化插件。调用方可以注入自定义 Registry、PolicyEngine、Tracer、ContextFactory 或 LocalPluginProvider。

## 公共 API

- `build_harness(...) -> HarnessApplication`
- `HarnessApplication.start()`：发现、初始化并注册插件。
- `HarnessApplication.invoke(request)`：仅在 STARTED 状态调用 Runtime。
- `HarnessApplication.execute_plan(request, plan)`：验证并执行 Plan。
- `HarnessApplication.cancel_plan(plan_id, reason)`：取消当前进程内的活动 Plan。
- `HarnessApplication.shutdown()`：注销 Capability 并关闭全部插件。
- `HarnessComponents`：组装完成后的只读组件快照。
- `BootstrapState`：CREATED、STARTED、STOPPED。
- `BootstrapStateError`：非法生命周期操作错误。

推荐使用异步上下文管理器：

```python
async with build_harness() as app:
    result = await app.invoke(request)
```

## 生命周期

```text
CREATED
  │ start()
  ▼
STARTED
  │ shutdown()
  ▼
STOPPED
```

- 已 STARTED 时重复 `start()` 幂等。
- 重复 `shutdown()` 幂等。
- STOPPED 应用不能重新启动，应重新调用 `build_harness()`。
- 启动批次失败由 LocalPluginLoader 回滚，应用保持 CREATED。
- `invoke()` 在 CREATED/STOPPED 状态抛出 `BootstrapStateError`。

## 插件发现

默认扫描 `financeclaw.plugins` entry point。可以用 `plugins=(...)` 显式传入插件；此时通常同时设置 `entry_point_group=None`。`plugins` 与自定义 `plugin_provider` 不能同时配置。

## 依赖边界

Bootstrap 可以依赖所有阶段一 Harness 基础设施，因为它是最外层组装点；其他核心模块不得反向依赖 Bootstrap。应用通常共享一个 Registry/Policy/Trace 实例，但这是组装结果，不是基础设施类强制单例。

## 测试

项目安装后运行：

```bash
.venv/bin/python -m unittest discover -s harness-bootstrap/tests -v
```

## 阶段一非目标

不实现 Planner、Workflow、多 Agent DAG、业务路由、SQL/RAG/LLM、Remote Plugin、MCP 或数据库持久化。
