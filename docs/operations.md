# 运行维护

当前仅支持新库初始化，不提供历史应用迁移或原拆分部署切换路径。业务运行结构见[统一进程](architecture/combined-runtime.md)。

## 配置与启动

先执行 `scripts/init-local.py` 创建本地凭据，配置非密钥参数后启动 Compose。`init-db` 只持有结构所有者身份，业务进程使用 `financeclaw_app`，Memory Worker 使用 `financeclaw_memory`。结构所有者凭据不挂载到运行进程。

```bash
docker compose up -d --build --wait --wait-timeout 180
docker compose ps
curl --fail http://localhost:8080/actuator/health/readiness
```

就绪检查只证明必要存储可读和连接可用，不证明模型、供应商或业务任务已经完成。数据库中断时先恢复数据库并观察就绪状态，不用新幂等身份重发未知写入。

## 受理与排空

`PUT /v1/operations/admission` 接受 `paused`、预期 `revision` 与 `Idempotency-Key`。暂停只阻止当前主体的新任务，已有审批、回答、取消和恢复仍然可用。

`GET /v1/operations/drain` 返回当前主体积压计数；`/drain/items` 按任务、通知、未生成通知的绑定和记忆作业分页。不存在旧 Outbox 或不完整报告字段。独立外部通知的未知/未验证结果仍然是阻塞项。

```bash
python3 scripts/inspect-drain.py --help
```

预检脚本只读 HTTP，核对前后观测与明细数量，分别报告阻塞、检查不完整或可供复核；它不改变受理状态，不自动停止服务。

## 画像与记忆

`GET /v1/memory/profile.md` 返回当前认证主体的 Markdown 和 ETag。编辑后用 `PUT`、`Content-Type: text/markdown; charset=utf-8` 和原始 `If-Match` 保存。不能跨主体、跨遗忘 epoch 或覆盖别人的并发编辑。

自动证据提取默认关闭；手工画像默认启用，两者是不同配置。画像只能由认证 API 或隔离隐私恢复入口处理，受限 Worker 不具备画像表读取权限。

## 备份和隐私恢复

当前应用的正常备份恢复继续保留。恢复旧主库备份可能遗漏其后发生的遗忘，必须在隔离库中与独立遗忘事实或最新权威快照核对，不能只比较 epoch，也不能先启动业务进程再补清理。

```bash
# 停止本应用的写入者；不会自动停止其他项目或手工进程
docker compose stop api memory-worker

python3 scripts/check-restore-privacy.py --help
python3 scripts/user-profile.py --help
```

隐私快照由 [SQL](../ops/restore-privacy-snapshot.sql) 导出，不包含记忆正文或 Markdown。比较器拒绝不支持的快照结构和缺少权威事实的恢复。独立遗忘账本由[账本 SQL](../src/main/resources/privacy-ledger/V1__forget_ledger.sql)单独创建，启用前使用 `privacy-recovery` 的显式隔离 `bootstrap`；恢复库使用 `replay` 重放，再重新检查快照。

保留的恢复边界与完整参数示例见历史操作记录中的[独立遗忘账本](history/m5f-forget-ledger.md)和[恢复隐私核对](history/m5e-restore-privacy.md)。它们记录过去验收批次，当前表结构和入口以本文件、新库初始化及代码为准。新的初始化程序不会修改或清空任何已有实验库。
