# 紫微领域模块：从输入到盘面证据

本目录负责出生资料校验、时间规范化、确定性排盘和结果投影；模型解读在上层子图完成。
功能默认关闭，目前只允许 development/test 环境使用，规则口径仍是待独立核验的候选版本。
启用步骤、输入示例和限制见[紫微 Agent 手册](../../../../docs/operations/ziwei-agent.md)。

## 先理解调用链

```text
紫微子图选择五个固定排盘工具之一
  → tools/ziwei.py：接收类型化参数和框架注入的可信上下文
  → tool_inputs.py：转换为不可变 ZiweiAnalysisRequest
  → application.py：鉴权、聚合校验、计算、持久化 Artifact
  → normalization.py + service.py + adapters/x_iztro.py：时间处理与确定性事实
  → 工具返回有界盘面、证据引用或需要澄清的字段
  → 紫微子图根据事实解读，结果交回根 Agent
```

每次请求显式携带出生快照、目标区间和上下文；服务实例不保存共享的“当前用户命盘”。
确定性计算层不调用模型，`application.py` 是本目录中连接鉴权和 Artifact 持久化的应用层。

## 按问题定位源码

| 想理解或修改什么 | 入口 |
| --- | --- |
| 出生资料、规则、事实、领域结果的数据结构 | [kernel/ziwei.py](../../../kernel/ziwei.py) |
| 五个固定工具各自允许哪些参数 | [kernel/ziwei_tools.py](../../../kernel/ziwei_tools.py) |
| 模型参数如何变成统一内部请求 | [tool_inputs.py](tool_inputs.py) |
| 时辰精度、时区歧义、相对日期、主体 HMAC 指纹 | [normalization.py](normalization.py) |
| 权限、问题聚合、投影大小与 Artifact 归属 | [application.py](application.py) |
| 目标层级及上层事实、区间分段、主题投影 | [service.py](service.py) |
| 引擎接口与 x-iztro 适配 | [ports.py](ports.py)、[adapters/x_iztro.py](adapters/x_iztro.py) |
| 稳定错误码与澄清问题合并 | [errors.py](errors.py)、[clarification.py](clarification.py) |
| 工具执行与子模型循环 | [tools/ziwei.py](../../tools/ziwei.py)、[graphs/ziwei_agent.py](../../graphs/ziwei_agent.py) |

## 修改时保留的边界

- 资料缺失、时辰歧义或夏令时冲突必须返回澄清，不填入猜测的出生时间。
- `request_clock`、时区、权限和所有者来自可信运行上下文；模型不能覆盖。
- 计算请求不可变；并发工具调用分别绑定请求、证据和 Tool call ID。
- 完整盘面保存为受保护 Artifact；模型只接收有界投影，超限明确失败。
- 错误不拼入出生原文；Artifact ID 不等于读取授权。

## 本地验证

在仓库根目录执行：

```bash
uv sync --frozen --extra dev --extra ziwei
.venv/bin/python -m pytest -q tests/stage7 tests/stage8_hotfix/test_ziwei_five_tools.py
```

未安装 `ziwei` extra 时部分引擎测试会跳过。基础单测通过、引擎集成通过和真实模型解读质量
是不同验证结论，记录结果时应分别说明。
