# Stage 5 验证记录

验证日期：2026-09-03

## 1. 已完成范围

- development/test/staging/production 配置示例分离，production 配置 fail-closed；
- BFF OIDC/JWT issuer、audience、exp/iat、固定非对称算法和可信身份 claim 投影；
- Agent Server service token、Provider/JWKS/LangSmith/OTel/内部服务出站 allowlist；
- LangSmith project、sampling 与生产输入输出隐藏策略；
- OTel HTTP/SQL/Agent Server trace、HTTP latency metric、JSON 日志和凭据脱敏；
- 永久 Audit 与有界 Outbox 同事务写入，租约、退避、dead-letter publisher；
- S3 兼容 Artifact Store、SSE/KMS、SHA-256 checksum、租户/主体 hash key；
- 数据库/Artifact/Agent Server 组合 readiness、数据库 statement timeout 和优雅关闭；
- Stage-5 版本化回归数据集、LangSmith dataset publisher 与本地 release gate；
- Docker/Compose 基线、环境模板、SBOM、依赖漏洞扫描与 Runbook；
- 删除旧 Spike、harness contracts/events/trace、旧测试和生产包声明。

## 2. 自动化证据

```text
ruff check financeclaw tests scripts
All checks passed!

ruff format --check financeclaw tests scripts
141 files already formatted

pytest -q
78 passed, 1 skipped in 16.04s

python scripts/generate_sbom.py
CycloneDX 1.6, 145 locked components

uv build
Successfully built financeclaw-0.1.0.tar.gz and financeclaw-0.1.0-py3-none-any.whl

pip-audit --strict --require-hashes --disable-pip -r <uv exported production requirements>
No known vulnerabilities found
```

跳过项是需要显式外部服务/凭据的测试，不使用 mock 冒充在线证据。Alembic 测试验证
`0001 -> 0005` upgrade、base downgrade、re-upgrade 和 `outbox_events` 索引。

## 3. 清理证据

生产依赖已将 `langgraph-cli`、PostgreSQL/Redis Agent Server adapters 移入可选
`agent-server`/`dev` 分组。默认 BFF wheel 仅包含 `financeclaw` 包，不包含
`financeclaw_spike`、`harness_contracts`、`harness_events` 或 `harness_trace`。

本地工作区已有、未跟踪的 Harness 目录和 IDE/`.DS_Store` 文件属于用户数据，未纳入提交；
远程主分支的已跟踪旧实现按 Stage-5 删除映射清理。

## 4. 尚需真实环境关闭的上线门禁

以下事项不能由单机仓库测试代替，因此 Stage-5 实现完成不等于已批准生产发布：

- Agent Server 部署形态、许可证、数据驻留、升级兼容与支持边界审批；
- 真实 OIDC、Secret Manager、网络策略、S3 versioning/retention 和服务身份联调；
- 真实 LangSmith 离线实验基线、生产在线 evaluator/人工复核队列和告警；
- 目标容量下的 P50/P95/P99、queue/checkpoint、token/cost 与单租户限额压测；
- PostgreSQL、Redis、Provider、LangSmith 和网络故障注入；
- 两套 PostgreSQL 与 Artifact Store 的隔离恢复演练及 RPO/RTO 证据；
- 安全评审/威胁模型关闭高风险项和上线批准。

这些门禁及回滚步骤记录在 `docs/operations/release-checklist.md`、
`production-runbook.md` 和 `disaster-recovery.md`，不得用本记录替代组织审批。
