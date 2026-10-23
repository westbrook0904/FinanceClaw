# V10 表退役操作

V10 删除 `business.skill_refs`、`agent_runtime.tool_effects`、`business.outbox`、`business.projections`。这些表分别是停用的 Skill 引用、M0 实验账本、无业务读取方的本地投影队列和镜像；真实业务事件、执行检查点和飞书通知保留。V1–V9 迁移不改。

升级前停止旧版业务进程，避免旧版继续写入即将删除的表。确认外部报表或手工脚本没有引用这些表。在修改数据库前完成主库常规备份；如需单独留存退役数据，可以使用下面的命令。`FINANCECLAW_BACKUP_URL` 使用 PostgreSQL URI（例如 `postgresql://localhost:5432/financeclaw_java`），不能直接使用应用的 `jdbc:postgresql:` 地址；凭据通过已有 PostgreSQL 凭据文件传入。

```sh
umask 077
pg_dump --dbname="$FINANCECLAW_BACKUP_URL" --format=custom \
  --table=business.skill_refs --table=agent_runtime.tool_effects \
  --table=business.outbox --table=business.projections \
  --file=financeclaw-v9-retired-tables.dump
pg_restore --list financeclaw-v9-retired-tables.dump
```

归档文件可能包含历史实验参数或结果，应按数据库备份要求保存。完成备份后运行既有 migration 入口，再启动当前版本。迁移使用无 `CASCADE` 的 `DROP TABLE`：存在额外外键、视图等依赖时升级会失败并回滚，必须先核查这些外部消费者。不要为跳过错误增加 `CASCADE`。

V10 不创建归档业务表，也不把历史实验 pending 记录当成已完成的真实业务效果。保留 HTTP `outbox.pending`、`pending_outbox` 字段的兼容值 0；它们不再查询退役表。当前版本不支持迁移后直接启动旧版业务进程；回退应使用升级前备份和匹配版本，并遵循独立遗忘账本的隐私恢复流程。

验收范围：`LegacyTableRetirementIT` 从真实 V9 数据库升级到 V10，验证意外视图依赖会阻止删除并回滚、四表消失、其余 35 张应用表逐表数据不变。M0 相关恢复实验在测试初始化时单独创建实验表，生产启动不再创建该表。

V11 随后把 `business.tool_calls` 合入 `business.reports`。原报告 ID 和标题、原回执内容、pending 状态均保留；仅有报告而缺少回执的旧记录标记为 `legacy_untracked`，需要核对后恢复，不能自动宣告发布成功。升级前的常规全库备份应同时包含这两张表，四表归档文件仅是额外留存，不代替全库备份。
