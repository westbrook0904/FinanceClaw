# contract-tests

Stage-1 的稳定产品边界位于 `financeclaw/contracts`，覆盖受信任 `ExecutionContext`、显式
Tool/Workflow/Agent Target、BFF request/response、approval decision、stream event 与
ArtifactReference。

框架内部继续直接使用 BaseMessage、Command、Interrupt 和 LangGraph state；Capability、
Provider、Selection、Retry、ResultEnvelope 等通用执行契约已删除，不再建立镜像协议。

```bash
.conda/envs/stage0/bin/python -m pytest tests/stage1 -q
```
