# tests

阶段一测试覆盖公共协议、各基础设施模块、示例插件以及完整 Invocation 主链。实际测试文件与所属模块放在一起，顶层 `tests/*` 目录用于记录跨模块测试关注点。

## 测试位置

| 范围 | 目录 | 重点 |
|---|---|---|
| Contracts | `harness-contracts/tests` | 构造、冻结、序列化和错误协议 |
| SPI | `harness-spi/tests` | Agent/Tool 语义分离和 Manifest 一致性 |
| Registry | `harness-registry/tests` | 注册、过滤、唯一解析和所有权 |
| Local Plugin | `harness-plugin-local/tests` | 发现、生命周期和事务回滚 |
| Policy | `harness-policy/tests` | 策略顺序、短路、约束和内置策略 |
| Trace | `harness-trace/tests` | Span 生命周期、层级、续接和 Console 输出 |
| Runtime | `harness-runtime/tests` | 完整 Invocation、超时、取消和错误归一化 |
| Planning | `harness-planning/tests` | DAG、引用、条件、Binding 与可执行性校验 |
| Execution | `harness-execution/tests` | 串行、并行、Join、分支、失败与并发限制 |
| Bootstrap | `harness-bootstrap/tests` | 依赖组装、状态机和启动失败回滚 |
| Plugins | `plugins/tests` | 三个插件行为、打包和 Bootstrap 集成 |

## 运行全部测试

安装项目后：

```bash
for suite in harness-contracts harness-spi harness-registry harness-plugin-local harness-policy harness-trace harness-runtime harness-planning harness-execution harness-bootstrap plugins; do
  .venv/bin/python -m unittest discover -s "$suite/tests" -v || exit 1
done
```

测试不依赖真实网络、数据库、行情或 LLM。
