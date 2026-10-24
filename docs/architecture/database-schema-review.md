# 当前数据结构

当前应用包含 41 张主库应用表，结构和职责划分见[数据库初始化与升级](fresh-schema.md)。V1 基线保持不变，V2 增加持久发布目录，V3 固定会话发布，V4 增加 Agent 身份和不可变草稿修订并固定会话/Turn 的 Agent 目标；V5 增加可空 composition 与 v3 清单标记，保持当时的 40 张表；V6 仅增加固定研究子调用表，当前 `business` 为 17 张表。V7–V9 扩充执行隐私定位、私有目标绑定和固定行情调用约束，均未增加应用表。只支持当前 Java 基线的前向演进，没有 Python 历史导入、历史实验库兼容、半成品报告或 Outbox 兼容返回值。

已经消除的重复设计包括无人读取的本地 Outbox/投影、停用的 Skill 引用、实验工具账本，以及报告实体之外的第二份工具回执。报告现在一次事务写入完整结果，避免双写与中间状态。

仍然保留以下边界，因为它们表示不同业务事实：

- Agent 身份与草稿修订：前者表达稳定 owner 与资源 ID，后者保存不可变编辑历史；草稿不能直接执行。
- 不可变发布、可变默认或 Agent 指针与发布历史：分别表示执行内容、新会话默认选择和 CAS 切换回执；会话绑定不随指针变化。
- 任务、命令、执行尝试与原生检查点：分别表示用户请求、待执行工作、当前租约和 SDK 可恢复状态。
- 子调用与步骤结果：前者保存稳定身份、冻结私有目标及累计额度，研究为 4/10、空工具叶子为最多 4/0、行情叶子为最多 4/1；后者保存可回放的本地结果。子调用不复制结果，也不替代根 Turn 的运行/取消权威。
- 当前授权/受理状态与幂等回执：重放旧请求必须得到原结果，不能返回新的当前状态。
- 用户画像与证据记忆：画像由用户编辑；证据来自已受理会话，有来源、确认状态与隐私版本。
- 事实与向量投影：事实是权威数据，投影可重建，远程晚到结果仍须核验版本。
- 主库隐私状态与独立遗忘账本：恢复旧备份时，不能让遗忘事实也随旧主库回退。

`business.child_invocations` 的物理列仅为 `root_turn_id`、`call_id`、`target`、`target_agent_id`、`target_release_hash`、`input_hash`、`scopes`、`status`、`model_calls`、`tool_calls`。主键为根 Turn 和调用 ID，目标档位与权限由数据库闭集约束，私有目标身份不可原地修改，状态只允许 pending/completed。owner、conversation、根发布、epoch 和 grant revision 从根 Turn/来源/受理授权 JOIN；最终结果直接关联现有 `capability_steps` 的 `child:<call_id>/result`，不新增结果副本、子任务表或轮询。其必要性与恢复边界见[固定研究](child-invocations.md)、[空工具叶子](private-agent-delegation.md)及[固定行情叶子](private-agent-market-leaf.md)。

Mapper 命名参考[项目 Java 规范](java-coding-guidelines.md)。数据库锁、CAS、去重和受影响行数是方法契约的一部分，不为统一外观而隐藏。

历史盘点见[历史审查](../history/architecture/database-schema-review.md)，其中的候选表数和升级方案不代表当前实现。
