# FinanceClaw

FinanceClaw 第一阶段是一个最小、边界清晰的 Harness Runtime。它负责接收请求、创建执行上下文、执行策略、解析本地能力并生成追踪信息；所有财经业务能力都位于插件中。

## 环境

- Python 3.14.3（`.python-version` 已固定）
- FastAPI 作为当前应用入口
- 源码包遵循 `src` layout
- 根 `pyproject.toml` 显式映射所有分离的源码目录，安装项目后可直接按下划线包名导入

文档模块名使用连字符，例如 `harness-contracts`；Python 导入名使用下划线，例如 `harness_contracts`。

## 模块

```text
FinanceClaw/
├── harness-contracts/       # 通用请求、上下文、结果和错误协议
├── harness-spi/             # Agent、Tool 和 Plugin 扩展接口
├── harness-registry/        # 能力注册、查询和解析
├── harness-policy/          # 调用策略与允许/拒绝决策
├── harness-trace/           # Trace、Span 和 Event 抽象
├── harness-runtime/         # 单次 Invocation 生命周期
├── harness-plugin-local/    # 本地插件发现、加载和注册
├── harness-bootstrap/       # 依赖组装与应用启动
├── plugins/                 # 业务 Agent/Tool 插件
└── tests/                   # 契约、集成和插件测试
```

每个模块目录均包含 `README.md`，说明职责、允许依赖和阶段一非目标；每个 Python 包的 `__init__.py` 也包含包级说明。

## 依赖红线

- Harness Core 不得导入 `plugins.*` 或任何财经业务实现。
- 业务插件只依赖 `harness-contracts` 和 `harness-spi`。
- Registry、Policy、Trace 彼此不直接协作，由 Runtime 统一协调。
- 第一阶段不实现 Planner、LLM Router、Memory、RAG、Workflow、远程插件或数据库持久化。

## 当前状态

当前提交只初始化结构和模块边界，不提前冻结具体协议实现。下一步应按照 `.design/第一阶段.md` 从 M0 Contracts Freeze 开始开发。
