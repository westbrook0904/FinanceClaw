# Provider Fabric Acceptance

仓库级验证 FinanceClaw 仍拥有的 Provider 语义：多 Provider priority/health selection、READ
retry/fallback、WRITE idempotency 与 equivalence-group fail-closed、Provider events，以及旧插件
Direct Invocation 兼容性。

```bash
.venv/bin/python -m pytest tests/stage3a -q
```
