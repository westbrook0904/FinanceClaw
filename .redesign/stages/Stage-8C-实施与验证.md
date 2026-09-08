# Stage 8C：旧根接管、唯一驱动与部署收敛

日期：2026-09-08。仓库实现和隔离验收已完成；未执行真实生产接管。
共享数据库与自建 Coordination 方向保持不变，没有引入 Temporal。
本阶段承接已推送的 8B 提交 `e0479f4`。

## 实现范围

- 生产 `build_coordination` 始终装配 Coordinator Facade。开关关闭只暂停新受理，GET／SSE
  不恢复旧派发路径；缺快照的早期 Turn 与历史 Workflow 仍可按原归属只读查询。
- `0011_stage8c` 只增加 `coordination_control`、`legacy_adoptions`，不接管历史任务。
  分页 inventory、只读 native shadow、根指纹和部署 revision CAS、完整原始证据归档分别成立。
- 支持完整首次 start，以及 root→单 child Agent／Workflow 的静止等待／终态位置；
  旧交互的 ID、revision、问题、动作和期限保留。接管不延长授权，原主体必须重新授权。
- 旧 root／child 的 operation ID、输入、发布、Stage 7 冻结资料、时钟／时区和预算不重算。
  原生索引转换为中立 attempt 索引，转换前的命令、hash、回执与观察完整保留。
- driver 3 隔离旧 Worker；兼容旧入口也检查数据库门闩。无法证明滚动兼容的旧 BFF、
  渠道及恢复进程必须受控停止并禁止自动重启，不声称数据库可以撤回已在途的 HTTP 调用。
- Worker 默认 4 个槽，PostgreSQL 同时约束 backend 全局 32 根、同租户 4 根。
  支持续租、硬退出后原回执恢复、数据库故障时停止领取和 SIGTERM 排空。
- CLI 聚合积压、年龄、unknown operation、授权／交互等待、child 结果待交付、取消、通知与
  数据库锁／连接池诊断。BFF readiness 加入兼容处理者和过度积压门禁。
- 回滚暂停新受理与新命令领取，保留查询、回执核对和兼容 Worker。已有 driver 3 根或控制／
  接管事实时禁止破坏性 downgrade；不存在 coordinator→legacy 切回操作。

## 验证与证据

仓库自动化结果及源文件摘要汇总见 [verification.json](../evidence/stage8c/verification.json)。
原生接管、兼容滚动替换与容量测量见 [native-cutover.json](../evidence/stage8c/native-cutover.json)，
正式服务故障回归见 [native-regression.json](../evidence/stage8c/native-regression.json)。

最终仓库回归 **333 passed、13 skipped、2 deselected**（未把用户原有未提交的
`test_feishu_lifecycle.py` 纳入此次提交验收）；真实 PostgreSQL Stage 8 专项 **89 passed**。
原生服务 8 个故障／恢复场景全部通过；旧根、Agent child、Workflow 审批 3 类接管全部完成。
12 根合成负载首次推进 P95 **1.253 秒**、完成用时 **4.256 秒**，观察到的单租户最大并发为 4。
Ruff、格式和差异检查通过。

| 验证 | 断言 |
|---|---|
| 只读 shadow／CAS | 不改原始数据；缺证据可见；停止证明、数据库指纹或影子证据不符时不接管 |
| 真实 PostgreSQL 两进程接管 | 同根仅一个提交；失败者无部分归档；旧驱动领取被封闭 |
| Worker 远端提交后硬退出 | 进程退出码 23；两个新 OS Worker 只找回原回执；一个远端操作、一次预算、一个最终 Journal |
| 五进程容量竞争 | 全局上限 3、同租户上限 2；两租户均获得槽位 |
| 旧交互与 child | 原 ID／revision／截止时间／上下文保留；child 结果交付原 parent，不启动替代 child |
| 旧中文幂等请求 | 兼容原 ASCII 转义摘要；相同请求保留原 Turn 和 operation，不刷新授权 |
| 正式 Alembic | 8B→8C 扩表无执行；空库往返；控制状态或 driver 3 根存在时禁止降级删表 |
| 原生 LangGraph | 独立旧生产者退出；静止旧根接管后由新 Worker 完成，原生 metadata 无重复 operation |
| 兼容 Worker 滚动替换 | 替换其中一个进程，另一个继续推进；无 GET／SSE 驱动、无租户配额超限 |

容量值是合成负载下“恢复派发到第一次正式推进”的 P95，不是模型端到端耗时或生产 SLO。
运行时为凭证隔离的 `langgraph dev / runtime-inmem`；其进程重启不提供生产持久化保证。
业务责任和多进程竞争使用真实 PostgreSQL 16，不用 SQLite 代替相关结论。

复现方式（PostgreSQL URL 指向专用测试集群，工具会建立独立测试库）：

```bash
.venv/bin/python -m pytest -q -m 'not external'
FINANCECLAW_STAGE8_TEST_POSTGRES_URL="$TEST_POSTGRES_URL" .venv/bin/python -m pytest tests/stage8 -q
.venv/bin/python -m experiments.stage8c.run --postgres-url "$TEST_POSTGRES_URL" --directory /tmp/stage8c-native --report /tmp/stage8c-native.json
.venv/bin/python -m experiments.stage8a.run --postgres-url "$TEST_POSTGRES_URL" --directory /tmp/stage8c-regression --report /tmp/stage8c-regression.json
```

8C 执行器不接受 LangSmith 许可证或模型凭据参数，使用正式发布图和合成离线模型。
测试与仓库写入仅针对隔离数据；本次没有发送真实飞书消息。

## 保持关闭的生产门禁

1. 目标部署的数据库备份、旧生产者实际停止及禁止重启证据，兼容 Agent runtime 与角色配置。
2. 持久化 self-hosted Agent Server 容器和目标部署的故障、恢复、时延及容量验收。此前真实
   LangSmith／许可证凭据注入没有获得授权，未绕过自动审批拒绝；native dev 不能替代该项。
3. 8B 的真实飞书测试单聊、原消息、固定 UUID 与回执／去重窗口验证。未获得发送目标和授权，
   新通知开关保持默认关闭。
4. 盘点中仍不可证明的旧 resume、多次操作、独立旧 Workflow 和未静止 backend；必须处理原
   证据或补充明确的迁移实现，不能换 operation ID、自动重试或迁到 latest。

本阶段交付了上述门禁的执行工具和隔离证据，未把未知项当作生产验收通过。
实际操作顺序见 [Coordinator 接管手册](../../docs/operations/coordinator-cutover.md)。
