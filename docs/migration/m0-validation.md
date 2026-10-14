# M0：固定工具链与 AgentScope 兼容性验证

本记录对应 [M0 验收清单](acceptance-checklist.md)；验证范围是 Java 工程和框架适配实验，产品 Turn API、完整审批授权及后台任务属于后续阶段。

## 验证结果

2026-10-14，MyBatis 版工程完成干净构建、打包及格式检查：**28 项测试通过，0 失败、0 错误、0 跳过**。使用真实 PostgreSQL 16.10 容器、独立 Java 子进程和本地 HTTP 服务；模型输出是确定性的合成数据。

| 实验 | 数量 | 实际确认 |
| --- | ---: | --- |
| 身份、SDK 和模型 HTTP 契约 | 6 | 身份哈希无拼接歧义；拒绝绕过权限的工具/策略；五个 hook 和真实工具循环；发送载荷与预算 |
| 独立 JVM 与故障窗口 | 13 | 审批前零效果、批准后一次、拒绝后零效果；澄清恢复；旧动作/版本拒绝；未知效果阻断与核对复用 |
| 状态与并发隔离 | 8 | MyBatis 原子 CAS；损坏状态拒绝；删除后拒绝迟到写入/重新执行；同作用域互斥与跨租户并行；合作中断 |
| Spring Boot 集成 | 1 | 启动与数据库就绪；MyBatis Mapper 自动注入；Hikari 连接池下的执行锁独占和释放 |

归档证据：[结果及源码 SHA-256](evidence/results.json)、[实际发送载荷](evidence/openai-request.json)、[依赖树](evidence/dependencies.txt)。目标仓库当前尚未创建 Git commit，因此以源码指纹绑定这次结果；修改代码后必须重新生成证据。

另做工具链负例：使用系统 Oracle JDK `21.0.2+13-58` 执行 `validate`，构建被 Enforcer 的版本、vendor 和 runtime build 三项规则拒绝。该预期失败不计入上面的 28 项测试。

本次未执行真实模型供应商、MCP、飞书或远程 GitHub Actions；不能将这些实验结果标记为产品全量迁移或生产恢复验收。

## 固定版本

| 组件 | 固定版本 | 固定方式 |
| --- | --- | --- |
| JDK | Eclipse Temurin `21.0.12+8` | `toolchains.lock.json` 固定分发包 URL/SHA-256；Maven Enforcer 验证 vendor、补丁及完整 runtime build |
| Spring Boot | `3.5.16` | `pom.xml` 固定 parent，使用 Boot 管理的 Spring/Jackson/Reactor 版本 |
| AgentScope Java | `2.0.3` | core 和 OpenAI model extension 使用相同 property；未引入滚动在线文档的新接口 |
| MyBatis Spring Boot Starter | `3.0.5` | POM 固定版本，使用 Mapper 与自动配置；无手写 JDBC 映射代码 |
| Maven | `3.9.11` | 官方 Wrapper `3.3.4`、固定分发 URL/SHA-256、Enforcer 精确版本 |
| PostgreSQL 测试镜像 | `16.10-alpine` | Testcontainers 隔离数据库，测试记录服务器实际版本 |
| Testcontainers | `1.21.3` | POM 显式版本 |

Spring Boot 选用与现有 Jackson 2 / Spring 6 依赖组合相符的 3.5 分支，本次不是升级到 Boot 4 的实验。3.5.16 官方系统要求支持 JDK 21；选择的 JDK 归属于官方 Temurin 21 发布，分发包已按官方发布摘要核验。[Spring Boot 要求](https://docs.spring.io/spring-boot/3.5/system-requirements.html)、[Temurin 发布](https://github.com/adoptium/temurin21-binaries/releases/tag/jdk-21.0.12%2B8)

编译和最小工具循环确认了三个容易误用的发布版行为：带 `RuntimeContext` 时使用 `call(List<Msg>, context)`；`maxRetries(1)` 映射为一次总尝试（`0` 无效）；本实验的 hook 顺序为 `onAgent → onSystemPrompt → onReasoning → onModelCall → onActing → onReasoning → onModelCall`，系统提示 hook 不会在每轮推理前重复触发。动态预算必须检查每次真实发送，不依赖系统提示 hook。

数据访问统一采用 MyBatis，SQL 集中在 `RuntimeMapper`，结果映射为简单 record；连接管理使用 Spring 自动配置的 `SqlSessionTemplate`。这里的状态 CAS、JSONB 与工具幂等是明确的条件更新，适合直接表达为 Mapper SQL；无需引入实体关系、脏检查或通用 Repository 框架。Starter 3.0.5 的官方发布基线为 Spring Boot 3.5，并依赖 MyBatis 3.5.19 / MyBatis-Spring 3.0.5。[官方发布说明](https://github.com/mybatis/spring-boot-starter/releases/tag/mybatis-spring-boot-3.0.5)

审批实验还定位到一个必须在接入层阻断的行为：`2.0.3` 的普通 `AgentTool` 不参与 `PermissionEngine` 规则评估，只有 `ToolBase` 及其派生工具进入该路径；空的默认权限上下文也会进入兼容逻辑。`IsolatedAgentFactory` 因此拒绝普通 `AgentTool`、空策略及 `BYPASS` 模式，实验工具统一继承 `ToolBase`。工具 schema 和规则注册必须一起审查，不能只看到 ASK 配置就认为审批已经生效。依据为固定版 `ReActAgent.CallExecution.evaluateOne` 和实际副作用计数。

## 复现命令

首次安装工具链需要 Python 3.12+、curl 和网络，测试需要已运行的 Docker。JDK 只安装到项目 `.tools/jdk`；依赖下载在 `.cache/`，均不提交 Git。已安装时无需再次运行 bootstrap。

```bash
python3 scripts/bootstrap-jdk.py
scripts/mvn.sh -B -ntp verify spotless:check
python3 scripts/collect-m0-evidence.py
```

若显式设置 `JAVA_HOME`，它必须指向相同 Temurin build；其他 JDK 或 Maven 会在构建前被拒。仅运行不需要 PostgreSQL 的测试可用 `scripts/mvn.sh test`；该命令不能代替完整 M0 验证。

证据输出：

- `target/surefire-reports/`：身份、SDK hook 和实际 HTTP 载荷测试。
- `target/failsafe-reports/`：真实 PostgreSQL、独立 JVM 恢复、故障与并发测试。
- `target/m0/processes/`：每个子 JVM 的退出记录；临时数据库连接配置在测试结束删除。
- `target/m0/openai-request.json`：实际模型适配器发送的合成输入，无真实用户信息或密钥。
- `target/m0/results.json`：版本、结果和源码 SHA-256 清单；收集脚本拒绝缺失、失败或跳过的测试。
- `target/m0/dependencies.txt`：用 `scripts/mvn.sh dependency:tree -DoutputFile=target/m0/dependencies.txt` 保存的实际依赖树。

GitHub Actions 已配置同一工具链及 `verify`，远程 CI 是否通过必须以实际 run 为准。

## 实现边界

| 入口 | 职责 |
| --- | --- |
| `FinanceClawApplication` | Spring Boot 启动、数据库迁移、存活/就绪检查 |
| `ExecutionScope` | 服务端租户/主体/会话/Agent/发布身份；长度编码哈希避免拼接歧义和 SDK 路径分隔问题 |
| `MyBatisAgentStateStore` | 完整 AgentState JSONB 存储、数据库原子 CAS、删除墓碑、绑定身份校验 |
| `RuntimeMapper` | MyBatis SQL 与 record 映射；状态、账本、执行锁集中管理 |
| `IsolatedAgentFactory` | 每次执行独立 engine/toolkit，拒绝绕过权限评估的工具/策略，显式 `ConflictPolicy.FAIL`，禁用自动修补悬空工具 |
| `GuardedAgentRunner` / `ExecutionGate` | M0 跨进程互斥入口；恢复输入在持有执行权后生成；未知副作用前后检查 |
| `ResumeInputs` | 从持久状态构造审批/澄清输入；检查问题 ID、状态版本和 action hash |
| `EffectLedger` | 副作用意图、已核对结果和相同操作键复用；未知结果阻断自动重试 |
| `BudgetedTransport` | 在适配器序列化之后、HTTP 发送之前检查完整载荷与输出预留 |

`MyBatisAgentStateStore` 是项目自有 PostgreSQL 适配器。M0 没有宣称验证所有 AgentScope 官方存储后端。只接受完整 `AgentState` 的版本化写入；旧版列表状态、无版本覆盖和隐式格式迁移明确拒绝。删除保留墓碑，旧会话身份不能重新创建，以免迟到状态写回；读取墓碑也明确报错，防止 SDK 将空值当作新任务先执行工具。新任务身份及活动执行中的删除/fencing 策略在 M1 完成。

`ExecutionGate` 使用 PostgreSQL session advisory lock，通过一个专用 MyBatis `SqlSession` 在活动执行期间持有连接，并在退出时解锁、关闭 session。它证明跨进程单写者准入，但不能作为生产租约/fencing 的完整替代：连接断开、僵尸 Worker、长任务调度和取消确认仍须在 M1 按 Turn/Command/attempt 实现。

审批实验验证的是版本与动作绑定及 SDK 恢复。主体授权、有效期、grant revision、业务 Interaction 状态机及渠道鉴权仍在 M1/M2 实现；不能直接将 `ResumeInputs` 暴露成公共 HTTP API。SDK 原生确认支持修改 ToolUseBlock，因此本适配器始终从服务端保存的动作构造 ConfirmResult。

副作用实验的“外部系统”是独立测试表，故意与本地工具账本分开提交，并且不为逻辑操作键设置唯一约束：重复派发会真实增加计数，不能被测试表的去重约束掩盖。它能证明未知回执被阻断及核对后不重复调用；真实供应商的幂等键和查询接口仍需逐工具验证。M0 未提供任意历史步骤回滚，也不把重新调用模型视为透明续跑。

预算实验采用整个发送 JSON 的 UTF-8 字节数作保守估算，包含少量传输字段；本地未发送的 Artifact/audit metadata 不进入载荷。尚未实现完整 ContextPlanner、tokenizer、压缩和多模态定价/容量规则，这些属于 M2。

## 状态保存边界与决策

以固定版本源码和故障实验共同核对，不能仅以 `AgentStateStore` 接口存在推断可恢复性：

| 中断位置 | 需要观察的结果 | 恢复约束 |
| --- | --- | --- |
| 首次模型调用前 / 工具执行前硬退出 | AgentState 尚未提交，外部效果为零 | 业务命令是否能重试由 M1 决定，框架不提供逐步骤日志 |
| 审批/外部工具挂起正常返回 | 保存等待点 | 新 JVM 根据原问题和状态版本恢复 |
| 模型异常 | 保存已接受的安全上下文后抛出错误 | 不能把抛错视为完全未写状态 |
| 普通工具异常 | SDK 将工具错误变成结果并可能继续推理 | M1/M2 必须独立判断任务是否成功 |
| 合作中断 | SDK 处理挂起工具/安全上下文并保存 | 中断请求不等于外部效果撤销 |
| 已记工具意图、外部结果未知 | pending 工具账本保留 | `blocked`，不能自动再次派发 |
| 外部效果成功、账本回执丢失 | 外部效果存在，AgentState 可能不存在 | 查询外部事实后核对，不依赖租约推断失败 |
| 工具回执保存、AgentState 未保存 | 账本结果存在 | 同一稳定逻辑操作键可复用已确认结果 |
| 完整调用保存之后硬退出 | 最终快照存在 | M1 需要幂等提交业务完成事务 |

**M0 决策：继续使用 AgentScope Java 2.0.3，数据访问统一采用 MyBatis。** 固定版本具备本阶段所需的快照、审批和澄清能力；FinanceClaw 保留原子 CAS、作用域隔离、ToolBase 接入限制、动作绑定及工具账本适配。任何 AgentScope 升级都必须重新运行这些实验。

M1 必须把目前的同步实验入口接入持久 Turn/Command/attempt，增加租约及 fencing、取消确认、Interaction 授权/有效期，以及状态保存后的业务完成事务。M0 的 PostgreSQL 锁和确定性模型回放不构成生产环境的 exactly-once 承诺。

## Python 源基线

[基线清单](baseline/python-manifest.json)记录源 commit 和 17 个依赖/配置/契约/测试文件 SHA-256；[工作区补丁](baseline/python-working-tree.patch)冻结迁移方案中发现的 5 个未提交文件；[API 样本](baseline/python-api-contracts.json)静态提取 Turn/Interaction 字段、拒绝规则、状态码和分页约束。补丁未应用到源仓库，也未读取真实 `.env`。历史配置与测试样本由源 commit 加补丁定位；没有将静态提取记为 Python 测试通过。
