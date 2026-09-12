# Stage 11 实现与验证

日期：2026-09-12。基于 Stage 10 统一 API/原生 Worker 架构实施；代码交付与真实模型质量、生产容量验收分别记录。设计见[实施方案](stage-11-异步记忆与上下文治理实施方案.md)，场景编号见[验收矩阵](stage-11-场景链路与验收矩阵.md)。

## 最终实现

| 边界 | 实现与职责 |
|---|---|
| 应用 SQL | 新增 memory_owners、memory_sources、memory_extractions、memory_records；唯一初始迁移总计 18 表。事实、候选、源版本、永久回执和 Outbox 同事务提交 |
| shared/memory | 唯一领域写入口；输入证据回读、scope、低风险规则、独立候选决定、source_seq、隐私 epoch、防重放墓碑、删除及保留规则 |
| API | 原始 user/accepted-answer 与来源登记同事务；最终 Journal 与闭合提取意图同事务；记忆设置、列表、纠正、遗忘和候选决定接口 |
| memory_worker | 独立进程；闭合输入拆批、受限结构化提取、owner 串行整合、不可重置模型预算、租约续期、快照 CAS、死信与后继唤醒 |
| integrations | 历史索引、记忆索引、删除分别消费；Store HTTP 前后回读 SQL；精确版本 key 删除，未知在途写保留 purge_pending |
| Agent Worker | 每 Turn 冻结有限 SQL 画像与目录；历史需求才查询语义索引，SQL 回读实际事实，空索引/故障有界回退 |
| WorkingContext | 原生 checkpoint 中唯一结构化正文；较早完整工具批次先归档后压缩，用户原文和未完成调用配对受保护 |
| 容量 | 共享 LLM 工厂/计数器；主模型与 fallback 共同窗口；实际回答/摘要请求的 Manifest 与 observed token 用量 |
| 飞书 | 复用验证过的通知地址，独立候选卡；当前用户决定直接走记忆用例，原任务结束后仍可确认，不创建 resume |
| 部署 | 四角色同镜像；memory_worker 使用单独环境文件、数据库身份和模型凭据；不导入图、ASGI 应用或渠道连接 |

旧的 Store 权威画像/事件、`approved=True` 记忆批准路径、记忆原生 HITL 和无人消费的默认审计 Outbox 已从实际装配删除。业务工具审批、Turn/Command/Interaction 和原生运行调度保持其职责。

## 关键场景与证据

| 场景 | 实际测试覆盖与证据 | 验证边界 |
|---|---|---|
| S01–S11 | test_api_memory、test_domain_intake、test_worker_pipeline、test_recall_snapshot、test_quality_policy；正式 AgentFactory 记忆工具回归 | 入库、候选与召回机制已测；真实模型的提取 precision/recall 尚未测 |
| S12–S25、S53 | test_domain_mutations、test_domain_postgres、test_worker_postgres、test_worker_outbox、test_worker_pipeline、test_worker_process | 事务回滚、并发、预算、丢租约、无丢唤醒、跨进程 kill/restart 有独立证据；不等于逐个生产容器故障点全部演练 |
| S26–S29、S51–S52 | test_api_memory、test_memory_cards、test_domain_mutations | SQL/ASGI/真实领域与模拟 Feishu gateway；未向真实用户发送测试卡 |
| S30–S40 | test_index_projection、test_domain_mutations、test_recall_snapshot、test_context_evidence | 删除即时不可读、迟到写、派生工件隐私、旧来源/回执不能复活；未声称所有备份/供应商 trace 都已删除 |
| S41–S49 | test_context_budget、test_context_native、test_context_evidence | 原生 create_agent、reducer、interrupt/resume、checkpoint 落盘后新实例恢复；真实模型多次压缩语义质量尚未测 |
| S50 | test_domain_retention、test_worker_operations、Stage 9 保留回归 | 未完成记忆来源/候选保护、清理预览/执行、当前 SQL 索引重建；旧记忆版本采取保守保留 |

原生运行证据：[context-native-probes.md](../evidence/stage11/context-native-probes.md)、[native-http-final.json](../evidence/stage11/native-http-final.json)。HTTP 探针使用隔离临时目录及随机回环端口：两个 Turn 均完成，四条 user/assistant 来源与两个闭合提取意图入库；两轮间 API 提交语言画像后，下一轮 Manifest 引用了正确 SQL revision。该探针未启动 Memory Worker，提取事件的 pending 状态没有被当成已提取。后台、PostgreSQL 迁移/并发、进程恢复和专用权限的独立证据见 [memory-worker-probes.md](../evidence/stage11/memory-worker-probes.md)。

已建立[中英混合规则与质量语料](../../tests/stage11/fixtures/memory-quality.json)，包含明确、临时、否定、引用、假设、更正、金额/日期、实时金融数据、部分失败和任务范围。21 条自动写入策略断言通过；候选/任务标签供后续真实模型评估，不能用规则测试成绩替代模型指标。

## 验证中发现并修复的问题

1. 原生 async 图工厂同步冷启动触发运行时 BlockingError；初始化移到线程，未关闭框架阻塞检查。
2. ModelProfile 集合字段跨进程序列化顺序不同，导致提取档案指纹漂移；序列化按稳定顺序输出，真实 kill/restart 测试保留并通过。
3. 旧 Manifest 只接受 preference/goal 等旧类型；统一为带 revision 的 profile/task 引用。
4. 重复登记同一 source 时自动偏好重算 expected_revision，错误触发同 mutation 冲突；只在首次登记的同事务执行规则写入。
5. 有效 task 尚未索引或索引没有有效命中时无结果；增加有限 SQL 兜底，始终忽略 Store 提供的正文。
6. 旧工件清理只做物理保护，但过期后仍拒绝必要恢复读取；现在未完成责任同时保护内容保留和授权读取。
7. 可选记忆固定配额可能挤占仍能容纳的当前用户请求；现在按完整输入预算剔除，checkpoint 和实际 Manifest 记录省略，诊断字段不冒充 Provider 输入。
8. 澄清不创建新用户锚点，原缓存未刷新检索；现在按已接受回答 hash 更新 L1，并仅刷新该回答支持的画像字段。正常问题模板中的问号不再阻止明确偏好答案写入。

## 可复制验证

```bash
.venv/bin/pytest -q
.venv/bin/ruff check financeclaw tests scripts deploy experiments/stage10
.venv/bin/ruff format --check financeclaw tests scripts deploy experiments/stage10
.venv/bin/python scripts/check_secret_leaks.py
```

PostgreSQL 用例需要显式 `FINANCECLAW_TEST_POSTGRES_URL`，每次创建随机私有 schema，并只删除该测试创建的 schema。未将 SQLite 替代 PostgreSQL 的锁、唯一约束或 SKIP LOCKED 证据；应用启动没有清空现有数据库。

最终全套回归：**599 passed、11 skipped、2 个第三方弃用警告，128.26s**。需要外部服务或显式 PostgreSQL DSN 的测试按条件跳过；独立启用真实 PostgreSQL 的 **8 项通过** 已另行执行。随后新增的上下文/检索边界定向集合 **71 项通过**，包含最终 S46 诊断计数和 S40 降级日志。Ruff 检查、364 个 Python 文件格式检查、敏感材料扫描及 `git diff --check` 全部通过。

部署入口、运维与进程恢复最终定向复测 **12 项通过**。容器内 `operations` 子命令通过专用入口重新构造 DSN；记忆进程和运维 CLI 仅读取显式环境注入，不自动读取项目业务 `.env`。入口分派和密码编码另以无数据库 I/O 检查核实。

## 明确保留的验收与运行边界

- 本机标准测试配置未提供真实 Provider key；没有执行真实模型提取/整合/摘要质量集，也没有测中文真实 embedding 检索质量。
- 真实 AgentServer HTTP 使用 dev 原生运行时和离线模型；已执行独立 PostgreSQL 并发/迁移测试，但没有将两者合称完整 PostgreSQL + Redis 四容器压力与故障验收。
- 未测设计中固定规格 P95 50ms/60s/10s 或 Stage 10 的 32/128 突发尾延迟。零星离线样本不能当生产性能承诺。
- 最终准备到实际模型请求之间发生隐私变化时，门禁拒绝旧请求；常规下一准备边界清理派生内容，不在 request wrapper 中伪改 checkpoint 或重放业务工具。
- 发布固定 `memory-v1` 索引契约；新 embedding/schema 需明确新版本发布和重建。当前旧事实版本保守保留，永久回执和墓碑不能由普通保留任务删除。
- 重放不会延长过期许可，不提供启动自动回填旧聊天。扩大历史来源范围需要新的明确授权；既有回放不能绕过遗忘屏障。

工作区同时有独立 Taibu/Skills 工作，本次没有回退其改动；本次未执行 Git 提交或推送。
