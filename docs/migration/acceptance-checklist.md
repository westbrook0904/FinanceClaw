# AgentScope Java 迁移验收清单

日期：2026-10-15。配套[迁移方案](agentscope-java-migration.md)。**M0–M3 已完成本地验证，M4 已实现记忆治理与 AgentScope 原生飞书文本接入，M5 已完成本地交付工程，M6a 已提交、M6b 历法适配已实现，真实环境验收及 M6 完整算法替换尚未完成**。M4 原计划中的卡片、表单和可靠送达核对仍待框架支持；结果与边界见 [M0 验证记录](m0-validation.md)、[M1 使用与验证](m1-validation.md)、[M2 使用与验证](m2-validation.md)、[M3 业务与验证](m3-validation.md)、[M4 记忆与飞书](m4-validation.md)、[M5 部署与恢复](m5-validation.md)、[M6 紫微基准与契约](m6-validation.md)。

## 1. 如何记录证据

每项至少记录目标 commit、依赖版本、测试输入/环境、预期、实际结果和日志/报告位置。真实凭据只通过环境或密钥管理注入；报告不保存 token、个人原文和模型推理。

| 证据类型 | 可以证明 | 不能替代 |
| --- | --- | --- |
| 静态审查 | 文件、接口和配置的声明一致 | Java 编译、运行或恢复 |
| 单元 / 契约测试 | 固定输入下的业务规则和序列化 | 真实数据库竞争、SDK 网络行为 |
| 模拟集成 | 确定性模型、假 MCP、假飞书下的完整调用链 | 供应商质量、真实租户 UI 与回调 |
| 数据库 / 故障测试 | 事务、并发、持久恢复和去重 | 不同外部供应商的副作用语义 |
| 真实环境验收 | 指定版本、环境及场景的实际连接和结果 | 未覆盖场景、生产负载和长期稳定性 |

不要求 Java 与 Python 的模型自然语言逐字一致。确定性算法、授权、接口状态、数据归属和副作用次数必须符合冻结契约；模型回答按事实完整性、来源、工具选择和限制披露评估。

### 源测试到 Java 验收的入口

以下路径相对于 Python 源仓库，只作为提取测试场景的入口；按 Java 接口和运行方式重写，不逐行翻译测试实现。

| 源测试入口 | 对应验收 |
| --- | --- |
| `tests/stage10/test_turns.py`、`test_http.py`、`test_observation.py` | M1 受理、幂等、运行核对与终态 |
| `tests/stage6fixc/test_native_interactions.py`、`tests/stage8_hotfix/test_root_clarification.py` | M2 审批与根层澄清 |
| `tests/stage11/test_context_budget.py`、`tests/stage8_hotfix/test_context_capacity.py` | M2 完整请求预算与离线估算 |
| `tests/mcp_integration/test_execution.py`、`test_hotel_result_views.py` | M2/M3 MCP 契约、归档与回读 |
| `tests/skills/test_governance.py`、`test_prompt_order.py`、`test_runtime.py` | M3 原生 Skill 加载、权限与 checkpoint 恢复 |
| `tests/stage4/test_portfolio_review_graph.py`、`tests/stage8_hotfix/test_native_subgraphs.py` | M3 Workflow 和子 Agent |
| `tests/stage7/test_calculation.py`、`tests/stage8_hotfix/test_ziwei_five_tools.py` | M3/M6 紫微算法与五工具协议 |
| `tests/stage11/test_domain_retention.py`、`test_index_projection.py`、`test_worker_role_postgres.py` | M4 遗忘、索引竞争与受限数据库身份 |
| `tests/stage8_hotfix/test_tool_progress.py`、`test_feishu_streaming.py`、`tests/skills/test_feishu_forms.py` | M4 进度卡片、流式结果与表单 |

同一单元格中的短文件名与前一个完整路径属于同一目录。涉及本地未提交改动的测试，以 M0 冻结后的文件内容为准。

## 2. M0：版本与框架适配门槛

- [x] 记录源 Python commit、5 个未提交文件的 patch/hash、配置样例和测试样本；未经授权不把源工作区改动提交或丢弃。
- [x] 锁定 JDK、Maven Wrapper、Spring Boot、AgentScope 及供应商模块，依赖解析与最小工具调用通过。
- [x] 编译验证 `ReActAgent`、`RuntimeContext`、`MiddlewareBase`、`AgentStateStore` 的实际接口；文档领先于 tag 的接口没有混入。
- [x] 列出选定状态后端的 `supportsVersioning`、CAS 冲突、创建/删除及序列化行为，两个独立实例测试旧版本写入被拒。
- [x] 执行审批挂起 → 持久化 → JVM 退出 → 新 JVM 恢复 → 正确工具执行；不能依赖旧对象或内存 future。
- [x] 对资料澄清的挂起/恢复做同等实验，验证输入 schema 与原问题绑定。
- [x] 列出 SDK 实际保存状态的时机；模型调用前后、工具前后、挂起/异常/中断均有故障结果说明。
- [x] 模拟工具已经产生效果但回执未保存：核对原逻辑调用，禁止自动重复写；无法核对则 `blocked`。
- [x] 捕获模型 HTTP 请求，核对 messages、tool schema、结构化输出与预算投影；有实际可复现的载荷样本。
- [x] 给出 M0 决策：继续使用固定发布版、升级至另一个正式版本，或先补齐适配层；不能留到上线再决定恢复方式。

## 3. M1：业务生命周期

| 编号 | 场景 | 预期 |
| --- | --- | --- |
| T01 | 相同身份、会话、幂等键和请求重复提交 | 返回同一受理结果，不新增业务任务或重复外部效果 |
| T02 | 相同幂等键，正文不同 | 明确冲突，不复用旧结果冒充新请求 |
| T03 | 同一 Conversation 并发受理两个活动 Turn | 保持单活动任务约束；不同会话可并行 |
| T04 | API 受理后、Worker 领取前重启 | Command 保留，可继续执行，不丢任务 |
| T05 | Worker 领取后异常退出 | 租约/attempt 可核对；旧 Worker 迟到写入无效 |
| T06 | SSE 断连、重连、重复游标 | 任务继续；快照 revision 单调；不重新运行 Agent |
| T07 | 模型返回文本但存在待审批/待工具 | 不标记 completed |
| T08 | 状态已保存，业务完成事务前退出 | 按原 attempt 幂等补交 Journal/Outbox，不再执行整条任务 |
| T09 | 取消与最终结果同时到达 | 由已定义的事务/CAS 规则得出唯一终态，不产生相互矛盾通知 |
| T10 | 终态后收到旧模型片段、旧工具事件 | 不覆盖终态和新 Turn 卡片 |
| T11 | API、Worker、Integrations 多副本 | 幂等领取与投影，不依赖单进程锁 |
| T12 | 失败/取消后新建 Turn | 使用符合隔离规则的干净执行状态，合法历史 Journal 仍可读 |

- [x] T01–T12 有可重复证据；并发与崩溃测试使用真实 PostgreSQL。M1 使用离线模型和本地投影，挂起识别使用持久化合成状态，详见 [M1 场景对应表](m1-validation.md)。
- [x] `/actuator/health/readiness` 检查数据库、业务表和 AgentState 表；Flyway 启动校验迁移。Artifact 尚未启用，启用时补健康检查。
- [x] 数据库、模型、SSE 的超时/连接池/线程边界已配置，模型无数据流取消与超时已测。对象存储未启用，后续添加对应边界。

## 4. M2：授权、交互、上下文与工具

- [x] 相同 subject 名称但不同 tenant、相同会话 ID 但不同 Agent/发布均不能读写对方状态。
- [x] 身份、授权范围和执行时钟由服务端注入，模型不能通过工具参数自行修改。
- [x] 发布在受理后更新，正在运行/恢复的 Turn 仍使用固定发布；版本不可用时明确阻断。
- [x] 审批校验 action hash、工具参数、问题 revision、有效期、状态版本和当前 Turn。
- [x] 重复正确回答幂等；错误类型、旧卡片、已取消任务、已撤销授权和其他用户回答均被拒绝。
- [x] 建立本地工具/MCP 共用治理入口；收窄工具集合不能扩权，子调用共用根 Turn 次数预算已测。实际子 Agent/Workflow 场景已在 M3 接入并通过本地模拟验收。
- [x] 模型/工具重试按故障分类；只读瞬时失败可有界重试，写操作有幂等/核对策略。
- [x] 超长酒店结果、中文、tool calls、response schema 和大量工具定义进入完整请求预算测试；未定价多模态块在发送前明确拒绝。
- [x] tokenizer 缺失时的保守估算单独通过，Java 不依赖 Python tiktoken 缓存。
- [x] 必需上下文超限时，在模型调用之前失败；没有通过丢弃原始用户请求或放大预算逃避失败。
- [x] 降级模型使用其实际窗口与参数；Qwen thinking、无正文响应和 usage 异常有明确结果。
- [x] Artifact 完整结果 hash 可查；引用和回读有大小限制，列表分页正确，JSON 对象不被列表切片。
- [x] 工具进度只暴露固定工具名和状态；日志/卡片无参数、原始结果、异常正文或推理内容。

M2 本地证据见 [验证记录](m2-validation.md)及[框架复用审查](framework-reuse-review.md)：新增 32 项测试（20 项真实 PostgreSQL 集成、12 项模型/预算测试）。默认开发认证、PG Artifact、固定 schema MCP 模拟不等同于生产 IAM、对象存储或真实服务验收。

## 5. M3：MCP、Skills、子 Agent 与 Workflow

| 场景 | 必测输入 / 故障 | 通过标准 |
| --- | --- | --- |
| 酒店 MCP | 原生工具发现/Schema/调用、查询/详情/标签、大数组、超时 | 只读白名单；权限/host/预算；Artifact 可授权回读 |
| MCP Schema | 新 attempt 采用服务端新增必填项 | 原生参数校验拒绝缺失字段；没有自建漂移比较器 |
| 禁用 MCP | 未提供密钥、无可用地址 | 不初始化该服务，不影响其他启用能力 |
| Taibu | 原生 `almanac` / `bazi` 与 camelCase 参数；服务错误、日期/分钟缺失 | 明确输入、结果关键字段校验；敏感数据禁止外网 |
| 调酒 Skill | SDK repository、目录、正文和附属资源按需加载 | 使用 `load_skill_through_path`；工具结果进入 AgentState |
| Skill 恢复与隔离 | 审批恢复、撤销、资源越界、同批工具、无权限任务 | checkpoint 复用；路径校验与 scope 生效；不维护另一个激活状态机 |
| 市场研究子 Agent | 正常研究、缺资料、失败、超预算 | 父子状态隔离；根层澄清；共享预算；来源与时间可解释 |
| 投资组合 Workflow | 中途审批、重复恢复、步骤失败 | 固定步骤可恢复；每步效果有账本；不跳过审批 |
| 紫微可选能力 | 五类排盘、历法、时区、日期范围、资料缺失 | 与冻结算法基准一致；不猜用户资料；不把未验收能力标为生产可用 |

- [x] 所有启用场景都有模拟集成；酒店/调酒/子 Agent 展示可由文档复现。
- [x] 子任务与根任务即使生成同名 call ID，进度和账本也不会合并错位。
- [x] Skill 采用原生 ToolResult/AgentState 保存与恢复；SSE 不转发工具正文。撤销授权阻断恢复，无权限任务不挂载仓库。M4 记忆提取只读取可信 USER 来源，排除工具文本。
- [x] 固定文本资产的许可证和 SOURCE 信息随迁移保留。

验证边界：M3 使用本地模拟 MCP、冻结行情、确定性模型与真实 PostgreSQL；市场研究另使用原生子 ReActAgent。紫微五类排盘未迁移，保持关闭，启用时明确失败。真实远端服务及真实模型的业务质量仍待 M5。见 [M3 记录](m3-validation.md)。

## 6. M4：记忆和飞书

- [x] 记忆仅来源于可信、同主体、真实用户证据；模型、工具、Skill 文本不能写成用户事实。
- [x] 精确低风险偏好指令与需确认候选分别测试；记忆确认不恢复业务 Interaction。
- [x] 提取/整合重复消费幂等，模型失败保留事务内写入的处理 job；索引故障可重试且不改写事实。旧来源迟到不能覆盖更新偏好。
- [x] owner/revision/privacy epoch 校验覆盖读取、搜索、索引重试、恢复与重建。
- [x] 遗忘后迟到索引写入、旧候选和冻结引用不能使内容复活；purge_pending 有状态与审计，清理期间新受理可重试。业务 Journal/Artifact 留存边界单独说明。
- [x] 原生 embedding 请求及响应验证 1024 维；实际 pgvector 0.8.1 查询和表达式索引通过，更换模型使用新 revision 并重建。供应商为本地 HTTP 模拟。
- [x] Memory Worker 通过独立 JVM 和受限数据库身份测试，不能读取业务/执行/渠道 schema，不装配业务工具。
- [x] 飞书原生验签、解密、消息映射与文本发送通过；重复事件不重复建任务；绑定、静音、切换、过期审批命令均校验。
- [x] 通过原生文本消息完成单字段澄清、审批、取消与最终正文通知；复杂表单仍使用现有业务 API。
- [x] 通知按 Turn/revision 唯一保存；SDK 返回记 submitted_unverified，异常或失联记 unknown，均不自动重发。
- [ ] 原计划的流式卡片、卡片工具进度、表单及旧卡片回调：SDK 2.0.3 不支持，按“飞书采用框架已有内容”的要求暂不自建；实时进度继续由 SSE 提供。
- [ ] 原计划的通知未知结果按业务事件 ID 查询核对：SDK 不返回回执且不支持查询，目前保留不确定状态供人工核查。
- [x] 模拟飞书与真实租户验收分开记录；本阶段没有连接真实飞书租户，真实验收待 M5。

详见 [M4 验证记录](m4-validation.md)。以上勾选仅覆盖明确列出的本地场景，不能视为原计划全部卡片能力已交付。

## 7. M5：数据与交付

- [x] CI 已拆分静态检查、单元/契约、PostgreSQL 集成与 Compose 交付实验；可选真实测试使用独立手动 workflow。配置已本地核验，未运行远程 CI。
- [x] 离线回归与 Compose 实验不需要真实密钥；Ziwei 继续关闭，没有引入 Python 算法依赖。
- [x] Java Compose 从空库执行一次性迁移、配置权限与索引，四角色通过 readiness；后台角色拒绝业务 HTTP。
- [ ] 真实模型工具调用、真实 MCP 查询、真实飞书审批/恢复仍待测试环境与凭据。已提供只读模型/MCP 验收脚本和手动 workflow，飞书由测试人员在租户操作。
- [ ] 真实环境的延迟、供应商调用量/token/费用和通知延迟仍待验收；本地离线阈值已预先冻结，运维 API 记录 owner 范围内队列/调用/通知状态，真实计费用量明确不可用。
- [x] 预先冻结的离线基线通过：12 请求、4 并发全部完成，p95 1.38 秒、Outbox 清空；不推导生产容量。
- [x] 正常关闭、强杀后审批恢复、数据库中断后恢复通过；模型/MCP 超时沿用 M2/M3 的本地验证。Artifact 使用 PostgreSQL，S3/MinIO 未启用，不声明对象存储故障已验收。
- [x] 运维说明包含 secret 挂载、配置模板、迁移、四角色启动、健康检查、故障核对和新库备份恢复命令。
- [x] 明确为新环境：保留 Java 事实与 Artifact，长期记忆按 epoch 遗忘；Python 历史未导入，未接管原渠道。
- [x] Java 完整备份恢复至隔离新库，33 张表行数/hash 一致；保留 Artifact、审批步骤和 privacy epoch，恢复后可继续受理新任务。
- [ ] 如导入 Python 历史：dry-run、归属、Artifact、审计来源、隐私删除和向量重建仍需专项验收；当前新环境不导入。
- [ ] 如替换现有环境：活动任务处理、单一飞书消费者、唯一写入者和线上回退仍需专项演练；当前没有接管原渠道。
- [ ] 向 Python 回退的反向导入与增量对账尚未实现。Java 备份/新库恢复已验证，运维流程明确禁止丢弃 Java 新增事实后直接切旧库。

## 8. M6：可选的全 Java 算法替换

- [x] M6a：从固定 Python 依赖与源码导出合成样本、五入口 Schema、完整盘面和 SHA-256 清单，二次生成逐字节一致。
- [x] M6a：Java 日期/出生时间契约与旧实现对照，验证五入口、时区、夏令时、子时、区间、并发和严格参数。
- [x] M6a：完整盘面比较器验证所有字段，并拒绝篡改字段、重复和缺失样本；只验证比较器，不声明 Java 算法已通过。
- [x] M6b 历法：锁定 lunar-java 1.7.7 及许可证/制品摘要，明确两处旧月界兼容处理，验证 72,684 天正向日期与合法出生年份反向转换。
- [x] M6b 部分时区：9 个区域、4,218 个偏移样本一致，仍区分 JDK TZDB 2026b 与 Python tzdata 2026.3；不扩展为全 IANA 兼容声明。
- [ ] M6b 排盘：完成固定星曜、本命与五类流运逐字段兼容，以及完整时区/规则发布策略。
- [ ] M6c：完整领域输入/澄清、固定规则、归属和 Artifact/投影预算，接入 AgentScope 原生 Toolkit 和既有治理。
- [ ] M6d：重启恢复、权限与隐私、真实工具调用、无 Python 算法运行依赖，验收后才允许启用紫微。

## 9. 完成定义

迁移交付记录逐项填写以下状态：

| 交付范围 | 状态填写要求 |
| --- | --- |
| Java 核心产品与 AgentScope 执行 | 启用能力的 M0–M5 P0 项全部通过，记录 commit 和环境 |
| 历史数据 | 写明“无需迁移”“未迁移”或附验收证据的“已迁移” |
| 飞书 / MCP / 真实模型 | 分别写明“模拟通过”与“真实环境通过”，不可合并 |
| 紫微算法 | 写明是否仍依赖 Python 计算服务；全 Java 替换需额外算法对比证据 |
| 尚未启用的能力 | 列明排除范围与原因，不能悄悄从验收清单移除 |

M0–M5 本地完整回归 **144 项通过**（M0 28、M1 23、M2 32、M3 28、M4 28、M5 5），0 失败、0 错误、0 跳过，另有 **9 项容器验收通过**，格式和静态交付检查通过，见 [M5 记录](m5-validation.md)及机器证据。真实 PostgreSQL/pgvector、独立 JVM、HTTP/SSE、原生 Agent/模型适配器与模拟 MCP/飞书有运行证据；真实模型供应商、真实 MCP/飞书租户、卡片及可靠送达、紫微算法、M5 的真实交付验收和 M6 完整算法替换不包含在通过范围内。M6a 的日期契约与基准验证单独记录于 [M6 记录](m6-validation.md)。

M6a 加入后重新运行完整 Java 验证：**294 项通过**，包含原 M0–M5 的 144 项和 M6a 的 150 项，0 失败、0 错误、0 跳过；参见 [机器证据](evidence/m6-results.json)。本次未重跑 Compose，原 9 项容器验收仍归属 M5 提交及其 jar。

M6b 历法适配加入后：**530 项完整 Java 测试通过**，新增 236 项历法验收；72,684 天公历转换与 72,635 天合法出生年份反向转换一致，9 个时区的 4,218 个偏移样本一致。完整排盘、全部 IANA 时区和真实服务仍不在通过范围内，见 [M6b 记录](m6b-calendar.md)。
