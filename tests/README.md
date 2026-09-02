# Tests

当前测试只覆盖仍由 FinanceClaw 拥有的核心：Contracts、SPI、Registry、Selection、Policy、
Context、Memory、Runtime、Trace、Events、Bootstrap、Plugins 和 Provider Fabric。已删除的自研
Router/Planner/Model/DAG/State/ReAct 测试不再作为回归基线。

```bash
.venv/bin/python -m pytest \
  harness-contracts/tests harness-spi/tests harness-registry/tests \
  harness-context/tests harness-memory/tests harness-plugin-local/tests \
  harness-selection/tests harness-policy/tests harness-trace/tests \
  harness-runtime/tests harness-events/tests harness-bootstrap/tests \
  plugins/tests tests/stage3a -q
```

未来 LangChain/LangGraph 适配测试应以契约和端到端行为为中心，避免复制框架自身的单元测试。
