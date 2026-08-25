# plugin-tests

插件测试位于 `plugins/tests` 和 `harness-plugin-local/tests`，覆盖：

- Echo Agent 原样回显；
- Calculator Tool 四则运算及结构化错误；
- Mock Finance Agent 的显式 mock 输出；
- Plugin 生命周期幂等；
- Manifest 与 Provider Descriptor 一致；
- `financeclaw.plugins` entry point 打包配置；
- 自动/显式发现、注册、注销和失败回滚；
- 三个插件通过 Bootstrap 和 Runtime 的完整调用。
