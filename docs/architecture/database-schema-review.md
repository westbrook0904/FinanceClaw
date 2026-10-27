# 当前数据结构

当前应用由 V1–V15 共 15 个迁移建立 **52 张主库应用表**，独立遗忘账本另有两张表，不计入主库。结构与升级操作见[数据库初始化与升级](fresh-schema.md)。已交付基线 `ad0b3ca` 的 [Actions 40361051811](https://github.com/westbrook0904/FinanceClaw/actions/runs/40361051811) 已通过完整数据库和 Compose 验收；该结果不覆盖此后的文档或代码改动。

| schema | 当前表数 | 主要事实 |
| --- | ---: | --- |
| `business` | 23 | 会话、Turn/Command/Attempt、交互、结果、子调用、派发策略/元数据与 worker/工件目录 |
| `control_plane` | 10 | Agent 与草稿、发布与指针/历史、资源授权与审计、触发规则/修订/回执 |
| `memory` | 13 | 画像、证据、隐私、提取与索引作业及恢复事实 |
| `channel` | 4 | 飞书入站去重、绑定与持久通知 |
| `agent_runtime` | 2 | SDK 检查点、会话工作投影与 Turn 轨迹 |

历史结构曾在 V5 为 40 表，V6 增加固定研究子调用后为 41 表、business 17 表，V7–V10 不增加表。当前 V11/V15 扩充叶子 profile、交互桥接与执行隐私绑定；V12 增加两张授权表和 Agent 归档；V13 增加三张触发表及四张派发表；V14 增加两张运行资源表及持久事件通知触发器。V1–V10 保持原字节与校验值，采用前向迁移，不导入 Python 历史数据或兼容实验库；下方原设计分析仍适用的边界已按当前结构补齐。

已经消除的重复设计包括无人读取的本地 Outbox/投影、停用的 Skill 引用、实验工具账本，以及报告实体之外的第二份工具回执。报告现在一次事务写入完整结果，避免双写与中间状态。

仍然保留以下边界，因为它们表示不同业务事实：

- Agent 身份与草稿修订：前者表达稳定 owner 与资源 ID，后者保存不可变编辑历史；草稿不能直接执行。
- 不可变发布、可变默认或 Agent 指针与发布历史：分别表示执行内容、新会话默认选择和 CAS 切换回执；会话绑定不随指针变化。
- 任务、命令、执行尝试与原生检查点：分别表示用户请求、待执行工作、当前租约和 SDK 可恢复状态。
- 子调用与步骤结果：前者保存稳定身份、冻结目标、叶子 profile 及累计额度；原研究/空工具/单行情契约保持，新增只读/受控叶子继续受根预算和当前授权约束。后者保存可回放的本地结果与精确交互桥接记录；子调用不另建结果副本，也不替代根 Turn 的运行/取消权威。
- 资源授权与不可变审计：当前行决定精确 Agent 发布或凭据引用是否可访问；审计追加记录授权修订，撤销不删除历史，caller 与资源 owner 分开。
- 触发规则/修订/回执与派发元数据：分别表示配置、历史定义、确定的受理/跳过结果及执行排序/配额；触发仍创建原有 Turn/Command，不建立第二套执行状态机。
- worker 注册与工件目录：前者表达当前协议、能力、运行摘要与有效期，后者记录经验证的受控本地 JAR；登记工件不动态加载或执行代码。
- 当前授权/受理状态与幂等回执：重放旧请求必须得到原结果，不能返回新的当前状态。
- 用户画像与证据记忆：画像由用户编辑；证据来自已受理会话，有来源、确认状态与隐私版本。
- 事实与向量投影：事实是权威数据，投影可重建，远程晚到结果仍须核验版本。
- 主库隐私状态与独立遗忘账本：恢复旧备份时，不能让遗忘事实也随旧主库回退。

`business.child_invocations` 的物理列仅为 `root_turn_id`、`call_id`、`target`、`target_agent_id`、`target_release_hash`、`leaf_profile`、`input_hash`、`scopes`、`status`、`model_calls`、`tool_calls`。主键为根 Turn 和调用 ID，目标档位与权限由数据库闭集约束，私有目标身份不可原地修改，状态只允许 pending/completed。owner、conversation、根发布、epoch 和 grant revision 从根 Turn/来源/受理授权 JOIN；最终结果直接关联现有 `capability_steps` 的 `child:<call_id>/result`，不新增结果副本、子任务表或轮询。`leaf_profile` 取 `legacy/read_only/controlled`；子 ASK 复用根交互、SDK 检查点和已有步骤表，后续精确恢复不重置调用身份或累计预算。原阶段分析见[固定研究](child-invocations.md)、[空工具叶子](private-agent-delegation.md)及[固定行情叶子](private-agent-market-leaf.md)，当前扩展见[非 RAG 集中实施](non-rag-completion-plan.md#p3a-受控叶子与结果读取语义)。

Mapper 命名参考[项目 Java 规范](java-coding-guidelines.md)。数据库锁、CAS、去重和受影响行数是方法契约的一部分，不为统一外观而隐藏。

历史盘点见[历史审查](../history/architecture/database-schema-review.md)，其中的候选表数和升级方案不代表当前实现。
