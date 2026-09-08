# Stage 8 Hotfix HF-0

真实 LangChain `create_agent`、ToolNode、LangGraph 子图、checkpoint 和 interrupt/resume
探针。模型只做确定性合成决策，叶子工具只记录合成标记，不调用真实 LLM、金融服务或渠道。
不导入 FinanceClaw 业务 bootstrap，不装配 Coordinator，不修改生产发布目录。

## 运行

在仓库根目录使用 Python 3.13 和已安装的开发依赖；本次验证的版本在
[`requirements.txt`](requirements.txt)。需要重建隔离环境时再使用这些实验固定版本，
不要在运行中的生产环境覆盖依赖。

```bash
.venv/bin/python -m pytest tests/stage8_hotfix -q

# 仅本地原生图，不监听端口；报告会明确 hf0_complete=false。
.venv/bin/python -m experiments.stage8_hotfix.run \
  --local-only --report /tmp/financeclaw-hf0-local.json

# 完整 HF-0：本地图和独占本机 Agent Server HTTP 验证。
.venv/bin/python -m experiments.stage8_hotfix.run \
  --report /tmp/financeclaw-hf0-native.json
```

完整验证需要允许本机 loopback 监听。默认创建唯一临时目录，并输出日志位置；
可用 `--directory /tmp/my-hf0-run` 指定目录，该目录下的 `native/` 必须不存在，
以防读到上一轮日志或原生数据。
每次运行都停止自己创建的服务；临时日志保留供排查。正式证据只记录版本、源码摘要、
合成 ID、计数和断言，不保存环境变量值或完整应用日志。

Agent Server 配置使用空 `env`，子进程环境采用白名单；SDK 使用无认证头、
`trust_env=False` 的本机 HTTP client。本地图使用 `tracing_context(enabled=False)`，
服务关闭 tracing／遥测。不使用真实 license／API key，不发送飞书消息。

## 验证范围

| 场景 | 验证重点 |
|---|---|
| serial | 顶层依次调用 research Agent 和 review Workflow，一次 start 完成 |
| question / two_questions | 子 Agent 内提问，向顶层传播 interrupt；每次只恢复原 thread |
| workflow_approve / workflow_reject | Workflow 审批前节点不重跑，批准执行一次、拒绝不执行 |
| hitl_approve / hitl_reject | 子 Agent 的原生 HumanInTheLoopMiddleware 审批向顶层传播 |
| repeat_agent / repeat_workflow | 同一个子图连续调用两次，第二次中断；状态和工具 ID 不串用 |

本地模式在每次 resume 前重新构建 Graph 实例，保留同一个 InMemorySaver，证明恢复不依赖
旧 Graph 对象的调用栈；这不等于进程重启的持久化保证。
HTTP 模式只注册 `hf0_orchestrator_v1`，每个场景一个顶层 thread，
全量核对原生 thread/run 清单，确保没有额外 child HTTP 创建。
两个模式都检查实际节点计数和顶层 ToolMessage／最终回答，而不只检查流结束或 HTTP 200。

## HF-1 接续

[`release-plan.json`](release-plan.json)保留根图、三个 Worker 与工具名称的新发布版本，
状态为 `reserved_for_hf1_not_registered`；它不是当前生产 Catalog。
正式工具治理、根预算、发布校验、紫微输出、BFF Journal、Webhook 及持久运行时重启
继续在 HF-1／HF-2／HF-3 实施。

具体原生契约、父 checkpoint 复用行为及退出清单见
[HF-0 实施与验证](../../.redesign/stages/stage-8-hotfix-HF-0-实施与验证.md)。
