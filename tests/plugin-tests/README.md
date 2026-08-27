# plugin-tests

插件测试位于 `plugins/tests` 和 `harness-plugin-local/tests`，覆盖：

- Echo Agent 原样回显；
- Calculator Tool 四则运算及结构化错误；
- Mock Finance Agent 的显式 mock 输出；
- Plugin 生命周期幂等；
- Manifest 与 Provider Descriptor 一致；
- `financeclaw.plugins` entry point 打包配置；
- 自动/显式发现、注册、注销和失败回滚；
- 三个插件通过 Bootstrap 与 Direct Runtime 的完整调用；
- 示例 Capability 参与第二阶段 finance-review-plan 的并行、Join、Approval 和 Resume。
- 旧 Plugin 在 Stage 3A 下自动获得稳定 Provider ID，加载/卸载按 Provider 精确执行。

插件测试同时守住依赖边界：业务插件不实现 Scheduler、Policy、Trace、StateStore 或
恢复逻辑。

```bash
.venv/bin/python -m pytest \
  plugins/tests harness-plugin-local/tests tests/stage2 tests/stage3a -v
```
