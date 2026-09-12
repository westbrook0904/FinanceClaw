# Taibu 真实联调

运行 `python -m experiments.taibu.probe --url <endpoint>/mcp --output <report.json>`。仅使用代码内固定合成样例，验证真实 HTTP、SDK、两项工具及原始结果归档回读；不会调用模型或发送渠道消息。公共服务须加 `--public`，只验证黄历。

构建、Host 端口配置、启动命令和验证边界见 [运行说明](../../docs/operations/taibu-mcp.md)。自动化回归为 `.venv/bin/pytest -q tests/taibu`，无需外部服务。真实联调不在普通 CI 中运行。
