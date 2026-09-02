# plugin-tests

Stage-1 已删除通用 PluginSPI、本地插件生命周期和示例插件。可执行能力统一为受治理的
LangChain `BaseTool`；远程工具通过无状态 MCP adapter 接入，并由本地 `ToolGovernance` 覆盖
远端声明。

相关测试现位于：

- `tests/stage1/test_governance.py`：本地 Tool/Catalog/Policy；
- `tests/stage1/test_mcp.py`：MCP 转换、调用与治理覆盖；
- `tests/stage1/test_agent.py`：Agent Tool allowlist 与执行时授权。
