# FinanceClaw Real-use Gate

该目录保存 Foundation F5 的业务评测组合层，不属于 Harness Core，也不属于 Portfolio Risk
Plugin。它负责组装真实 ModelProvider、业务插件、Memory、Planner、Explorer、SQLite checkpoint
与脱敏报告；业务插件本身仍只依赖 `harness-contracts` / `harness-spi`。

```bash
export OPENAI_API_KEY='...'
export OPENAI_MODEL='deepseek-v4-flash'
export OPENAI_BASE_URL='https://api.deepseek.com'

.venv/bin/python -m financeclaw_real_use.gate \
  --live \
  --output-dir .real-use/f5
```

调用由官方 OpenAI Python SDK 的 `AsyncOpenAI.responses.create()` 完成，因此也可以通过
`OPENAI_BASE_URL` 接入兼容 Responses API 的 Provider。只有显式 `--live` 才会访问模型 API；
不要把 API key 写入源码。报告不包含 API key、Prompt 或原始模型响应；默认 pytest 使用 SDK +
`httpx.MockTransport` 并标记 `live=false` / `gate_passed=false`，不能作为投产 Gate 的真实调用证据。
