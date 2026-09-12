# Stage 11 后台记忆执行验证

日期：2026-09-12。模型使用显式离线结构化响应夹具；测试来源均为虚构内容。

## 本地自动化结果

```sh
.venv/bin/pytest -q tests/stage11/test_worker_outbox.py tests/stage11/test_worker_pipeline.py tests/stage11/test_worker_operations.py tests/stage11/test_worker_process.py tests/stage11/test_worker_model_policy.py tests/stage11/test_index_projection.py
```

结果：25 passed in 7.83s。

- 完成 Turn 的最终 Journal 校验后，prepare 分组、part 提取、完整分组 ready、consolidate 写 SQL 事实及索引 Outbox；空提取不会再调用整合模型。
- 来源撤权正常跳过；模型不能引用未授权来源；整合期间 owner revision 变化拒绝旧快照整体提交；新 ready 输入不会丢失后继唤醒；死信输入隔离后，新输入仍可执行。
- Outbox 与结果同事务回滚，过期 claim 无法提交；模型调用前持久预扣尝试与输入/输出预算；重领和显式重放保留已消耗预算；完整处理期间续租。
- 真实启动 Python 子进程，在模型调用已预扣预算后 kill；租约到期后新子进程重领并完成。最终 claim_epoch=2、模型尝试数=2，旧未知尝试没有退还。该探针使用文件 SQLite 持久队列，并非生产容器/Redis 故障演练。
- 上述子进程探针发现并修复了集合序列化导致的跨进程模型指纹漂移；ModelProfile 的分级和区域集合现在排序序列化。
- 模型使用来源表冻结的数据分类、处理区域及派生授权预算；不合规模型、超授权输入、错误阶段输出额度被拒绝。Provider 地址或允许分类变化使旧模型指纹失效；显式选择主模型时使用主模型容量，不能借用摘要模型更大的窗口。

## 真实 PostgreSQL 探针

```sh
# FINANCECLAW_TEST_POSTGRES_URL 由测试运行环境提供；不要把密码写入命令或证据。
.venv/bin/pytest -q tests/stage11/test_worker_postgres.py tests/stage11/test_worker_role_postgres.py tests/stage11/test_domain_postgres.py
```

并发及迁移探针使用每个测试新建的随机 schema；权限探针单独创建随机数据库及随机登录角色，因为撤销 PUBLIC TEMP 必须只作用于新数据库。结束后只清理本次创建的资源。没有修改既有业务数据或权限。

最终运行结果：8 passed in 7.66s。

- 两个真实连接并发 claim，SKIP LOCKED 返回不重叠任务；过期 epoch 被替换后无法提交。
- 两个真实连接并发提交最后的 part，整个分组仅产生一次 ready revision 和一个 owner 整合指针。迟到旧 source 获得更晚的 ready revision，但保留较早的 source_seq。
- initial Alembic upgrade、metadata parity check 和 downgrade 在 PostgreSQL 成功；迁移后的业务表集合为 18 张，与 ORM 相同。
- 两个连接并发纠正同一 expected revision，仅一个成功；并发首次来源登记保持 owner 内唯一序号；跨 session 重放旧 mutation 不覆盖新画像。
- 独立角色重复 provision 仅验证；真实凭据拒绝业务表修改、审计改写、通知删除、TRUNCATE、业务范围外表读取、DDL 和临时表。故意添加过宽权限后 verify 拒绝启动；使用独立角色实际完成 prepare → part → consolidate → SQL task/audit/index Outbox，证明所授权限支持正常后台链路。

## 索引、重建和清理

- Store 测试夹具控制远程写入顺序：只能投影 SQL 当前 task 正文和确切 revision；外部事件不能指定正文或跨 owner 投影。
- 旧 put 与 forget 交错时删除确切旧键；无法确认远端结束的写入保留 unknown，物理清理保持 purge_pending，不宣称已完成。
- 其他记忆引起 privacy epoch 增长时，仍有效的待索引 task 重新核对当前 SQL/来源后可写入，避免索引永久遗漏。
- `memory_worker.operations reindex` 按 owner 的当前 active task 分页，重新产生可重放索引意图；旧已 published 事件不会阻挡重建，同一 request_id 去重，已 forget 记录不复活。
- `status` 展示队列、用量、版本冲突、租约接管和 purge_pending；`replay` 要求操作人及原因，保留授权与预算，额外模型预算须显式参数并审计。
- `retain` 默认 dry-run；只清理达到保留期限且无依赖的已 consumed 提取和已 published Outbox。当前/候选记忆引用、未结束子任务、活跃 owner 指针、未知远端写入均阻止清理。本轮不自动删除历史 memory_record 事实版本。

## 验证边界

未调用真实模型 Provider、embedding 服务或飞书；没有将离线结构化夹具当作提取质量验证。没有用此探针验证生产 Redis + AgentServer queue-worker 容器拓扑、长期负载、跨机网络分区或真实 Store 永久未知写入的运维恢复。原生 HTTP Turn/下一轮注入证据由 `context-native-probes.md` 单独记录。
