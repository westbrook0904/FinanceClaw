# portfolio-risk-agent

Foundation F5 的真实财经业务插件，实现 `finance.portfolio-risk/v1`。它根据调用方提供的
时点持仓快照计算净资产、日损益、持仓权重、集中度和日亏损限额，并返回可复算的结构化结果。

该 Agent 不访问外部行情、不修改资产、不提供投资建议；Descriptor 明确声明
`side_effect=NONE`、`egress=NONE`、`completion_mode=SYNC`，因此既可用于 FAST / PLAN，也符合
standalone EXPLORE 的最小安全范围。真实行情采集仍应由业务侧受治理的 READ Capability 完成。

金额和百分比采用 Decimal 计算并以定点字符串输出，避免二进制浮点误差。输入不合法时返回
`HARNESS.REQUEST.INVALID`，不会用部分持仓继续估值。

## F5 live Gate

Gate 会真实执行 FAST、LLM PLAN、standalone EXPLORE，并先写入一条风险偏好 Memory，再验证
新请求的 ContextUse 命中和 Action 是否实际应用该偏好。输出目录包含脱敏
`real-use-report.json`、Plan checkpoint SQLite 和 Memory SQLite；不会保存 API key、Prompt 或
原始模型响应。

```bash
export OPENAI_API_KEY='...'
export OPENAI_MODEL='your-structured-output-capable-model'

.venv/bin/python -m financeclaw_real_use.gate \
  --live \
  --output-dir .real-use/f5
```

`--live` 是必填开关，用来防止普通测试或误操作产生真实模型费用。默认 pytest 使用官方 SDK 与
`httpx.MockTransport`，生成的报告标记为 `live=false` / `gate_passed=false`，只能证明 Gate wiring，不能作为
一期投产证据。
