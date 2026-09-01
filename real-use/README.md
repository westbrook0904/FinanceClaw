# FinanceClaw Real-use Gate

该目录保存 Foundation F5 的业务评测组合层，不属于 Harness Core，也不属于 Portfolio Risk
Plugin。它负责组装真实 ModelProvider、业务插件、Memory、Planner、Explorer、SQLite checkpoint
与脱敏报告；业务插件本身仍只依赖 `harness-contracts` / `harness-spi`。

```bash
export OPENAI_API_KEY='...'
export OPENAI_MODEL='your-structured-output-capable-model'

.venv/bin/python -m financeclaw_real_use.gate \
  --live \
  --output-dir .real-use/f5
```

只有显式 `--live` 才会访问模型 API。报告不包含 API key、Prompt 或原始模型响应；默认 pytest
使用记录型 transport 并标记 `live=false` / `gate_passed=false`，不能作为投产 Gate 的真实调用
证据。
