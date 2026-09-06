# Ziwei 领域模块

本模块只保存紫微输入／规则／事实契约和确定性计算，不调用模型、不写数据库，
也不维护共享的“当前命盘”。属于默认关闭的 Stage 7 验证候选。

- `models.py`：冻结输入、Convention、出生上下文、盘面事实和有引用约束的领域结果。
- `normalization.py`：真实时间精度、时区歧义、租户／主体 HMAC 指纹、绝对目标区间。
- `ports.py`：领域需要的最小引擎接口，实际 x-iztro 适配在 infrastructure。
- `service.py`：一次生成目标层级及上层事实、按实际变化拆段、可解析的语义投影。
- `errors.py`：稳定错误码，不把出生原文拼进异常。

跨模块持久化由 `application/ziwei_service.py` 协调，Agent 与 Tool 在 orchestration。
测试与限制见 [Stage 7 实施与验证](../../../.redesign/stages/Stage-7-实施与验证.md)。
