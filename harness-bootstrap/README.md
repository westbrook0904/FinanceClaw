# harness-bootstrap

## 职责

创建并组装 Contracts 之外的具体实现，加载本地插件，构造 Harness Runtime，并向 API、CLI 或测试暴露应用入口。

## 依赖边界

- 可以依赖各 Harness 模块的公开 API。
- 是唯一负责组合具体实现的 Composition Root。
- 业务插件以配置或发现结果接入，不由 Runtime 直接引用。

## 阶段一非目标

不实现业务逻辑、复杂配置中心、多进程 Worker 或生产部署编排。
