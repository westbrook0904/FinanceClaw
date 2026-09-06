# Stage 7：紫微斗数领域 Agent 设计说明

状态：Implementation in progress（已有默认关闭的候选实现；规则与正式发布仍待验收）

实施更新：2026-09-06。实际交付、与本稿的收敛差异及验证边界见
[Stage 7 实施与验证](./Stage-7-实施与验证.md)，不将候选运行通过等同于产品规则已批准。

编制日期：2026-09-05

配套文档：[设计审视与待确认决议](./Stage-7-设计审视与待确认决议.md)

## 1. 设计结论

在现有顶层 Agent、受治理 Tool、Agent Delegation 和 Agent Server 上，新增一个只读的
`ziwei_doushu_agent`，专门处理紫微斗数排盘及基于盘面的传统命理解读。

职责分为三层：

1. 顶层 Agent 理解诉求、整理出生资料、委派任务、向用户澄清和汇总回复。
2. 紫微子 Agent 选择分析所需的盘面层级，调用工具，引用盘面事实解释问题。
3. 确定性计算服务负责历法、时间、排盘规则和星曜计算；LLM 不自行推算或补造这些事实。

对模型提供本命、大限、流年、流月、流日五个业务工具；内部共用一个计算服务和一套数据模型。
一次日盘请求可以原子返回所需的上层盘面，不要求 Agent 先后调用五个工具拼接结果。

本阶段不重新建设 Agent Runtime、Provider Registry、通用插件系统或命理微服务。
可以复用当前基础设施，但不能仅注册一个 Prompt 和五个 Tool 就认为闭环完成：委派输入输出、
上下文隔离、执行版本、恢复权限和大结果处理均有必须补齐的地方。

“排盘可复现”仅表示实现遵循所选计算规则，不表示命理解读具有经过科学验证的预测能力。
对用户将其定位为传统文化参考，不作为医疗、投资、法律等高风险决策依据。

## 2. 一期范围

### 2.1 包含

- 根据出生日期、出生时间或时辰、出生地点及排盘所用性别信息生成本命盘。
- 支持大限、流年、流月、流日查询，以及与问题相关的必要上层盘面。
- 支持“看盘”和“结合盘面回答问题”两种模式。
- 支持阳历／农历输入、闰月标记、时区与夏令时处理；实际支持日期范围以验证结果为准。
- 对资料缺失、同名地点、时间歧义和规则不支持返回可执行的澄清结果。
- 在 Web/API 与现有飞书 P2P 会话入口中复用同一业务链路。
- 结构化盘面、证据引用、规则版本、隐私保护、审计和故障恢复验证。

### 2.2 不包含

- 开放给用户直接指定 Agent、Tool 或 Workflow 的新业务 API。
- 合盘、自动校正出生时辰、遍历十二时辰预测、流时、择吉和多个流派同时解释。
- 长期自动跟踪运势、主动提醒或批量生成多年逐日报告。
- 默认保存可跨会话复用的个人命理档案，或把出生资料、命理结论写入长期 Memory。
- 新的星盘图片／交互式十二宫 UI；一期交付结构化数据和文本展示。
- 独立排盘 SaaS、第三方托管命理 Agent、独立部署服务。

若后续需要上述能力，分别评估，不把它们作为本阶段的隐含依赖。

## 3. 当前实现与接入缺口

本节是 2026-09-05 设计时的静态快照。2026-09-06 实施前核对：Stage 6 Fix A/B/C 已补齐
typed handoff、结构化结果、task-only 隔离、版本快照、权限上界、持久预算和恢复交付等前置能力。
Stage 7 直接复用；下表不再代表这些能力仍未实现。紫微仍选择根澄清，即使通用 child 交互已可用。

以下为代码审视结论，不是已完成修复的声明。路径以当前
[包结构与依赖规则](../../docs/architecture/package-layout.md) 为准。

| 当前实现 | 对 Stage 7 的影响 | 处理决定 |
|---|---|---|
| [`bootstrap.py`](../../financeclaw/bootstrap.py) 已装配 `market_research_agent` 和委派工具 | 可以沿用领域 Agent 模式 | 新增一个 Profile 和领域 graph，不另建注册系统 |
| 根 Profile 当前使用 `tool_catalog.latest()` 收录工具 | 新增计算工具会同时暴露给根 Agent | 根 Profile 改为明确工具白名单，仅新增紫微委派入口 |
| [`AgentProfile`](../../financeclaw/orchestration/agents/profiles.py) 声明了 `context_policy`，但装配未按它隔离上下文 | 子 Agent 可能读取父会话完整 Journal | 实现 `delegated-task-only-v1`，不是只修改配置字符串 |
| [`AgentHandoff`](../../financeclaw/modules/delegation/models.py) 只有 `task/context_refs`；[`DelegationService`](../../financeclaw/application/delegation_service.py) 只向子运行发送任务文本 | 出生参数无类型校验，引用没有真正注入 | 增加兼容旧版的 typed handoff 与授权引用解析 |
| 子运行结束时只提取最终文本 | 结构化盘面依据与澄清状态丢失 | 从声明的最终 state 字段提取并校验领域结果 |
| Agent 子任务可进入 interrupted，但现有 resume 仅支持 Workflow | 在子 Agent 内直接向用户 interrupt 会卡住 | 领域澄清以成功的结构化结果返回，由根会话发问 |
| 对账启动子任务时存在 `scopes={"*"}`；新建父／子执行上下文丢失部分属性 | 恢复可能扩权、时区和数据分类可能漂移 | 持久化可信执行快照，恢复不重新推导授权或时间 |
| Profile 版本与 `assistant_id` 的实际 graph 绑定不完整 | 记录的版本不一定是实际执行版本 | 使用明确的版本化部署绑定，旧运行保持旧绑定 |
| [`AgentFactory`](../../financeclaw/orchestration/agents/factory.py) 按版本取 Tool，但治理部分路径按名称取 latest | 执行、审批、审计可能使用不同版本 | 一次解析，过滤／执行／审批／清单／审计共享同一绑定 |
| [`ArtifactService`](../../financeclaw/modules/artifacts/service.py) 默认超过 16 KiB 后返回截断摘要，且未提供通用回读工具 | 大盘面可能只剩摘要，模型却继续解释 | 领域语义投影和完整 Artifact 分开，禁止截断关键证据 |
| [`ConversationContextBuilder`](../../financeclaw/modules/conversation/context.py) 也可能为适应 token 预算截断 ToolMessage | 小于字节阈值不等于模型看到了完整 JSON | 预算压缩必须保留结构和必需事实，不够时显式失败 |

这些修复应覆盖现有市场 Agent 和 Workflow 的回归；不以紫微特例绕过治理。

## 4. 端到端执行契约

### 4.1 正常执行

1. 用户通过原有入口发问，Conversation 创建 Turn，固定可信的请求时间和会话时区。
2. 根 Agent 提取 `ZiweiAnalysisRequest`，通过受治理的
   `delegate_agent__ziwei_doushu_agent` 发起委派。
3. 服务端校验权限、输入 Schema 和授权上下文引用；保存 handoff、版本与执行快照。
4. Agent Server 启动独立 child thread/run。子 graph 的确定性 preflight 校验、规范化出生资料
   与目标时间，生成不可变 `BirthContext` 和 `ResolvedTarget`。
5. 资料完整时进入子 Agent 的 ReAct 循环；根据诉求调用一个或少量盘面工具。
6. 工具调用同一计算服务，产出完整盘面快照、可用于分析的投影和证据索引。
7. finalization 节点生成并校验 `ZiweiAgentResult`，写入约定的最终 state 字段。
8. Delegation 将该结果交回根 Agent；根 Agent 汇总表达，保留盘面依据、假设与限制。

子 graph 内部可以有 `preflight → analyze → finalize` 节点，但它仍是现有 Agent Delegation
的一个领域 Agent，不再额外发布一套供根 Agent 调用的 Workflow。

### 4.2 澄清与失败

- 缺出生信息、地点多义、目标日期不清楚：子任务正常结束，
  `DelegationResult.status=completed`，领域结果 `outcome=needs_clarification`。
- 根 Agent 按缺失字段合并发问；用户补充后创建新 Turn、新 handoff，引用经授权的已有资料。
- 新 handoff 明确资料所属对象；不能把“帮朋友看”和“帮我看”的出生资料混用。
- 不支持的规则或日期：`outcome=unsupported`，说明支持范围或替代输入方式。
- 引擎异常、权限失败、结果损坏：委派失败，不包装成“盘面为空但解释成功”。
- 子 Agent 不执行用户交互 interrupt，不依赖当前尚未支持的 Agent-child resume。

委派传输状态和领域处理结果是两个维度，不能用 `completed` 推断“一定已生成解读”。

### 4.3 问题与盘面层级

| 用户诉求 | 首选工具 | 必须具备的分析上下文 |
|---|---|---|
| 查看初始命盘、总体倾向 | `ziwei_natal_chart` | 本命盘 |
| 查看某一大限 | `ziwei_decadal_chart` | 本命＋该大限 |
| 某年、今年的事业等主题 | `ziwei_yearly_chart` | 本命＋覆盖目标区间的大限／流年 |
| 某个月的变化 | `ziwei_monthly_chart` | 本命＋大限＋流年＋流月 |
| 某一天的情况 | `ziwei_daily_chart` | 本命＋大限＋流年＋流月＋流日 |

这是解释所需的层级，不要求底层算法按五个远程依赖逐次计算。
每个工具在一次服务调用内取得一致快照；跨大限或历法边界的区间按规则拆段。
没有时间诉求时，不擅自追加当天运势；没有要求逐日分析时，不遍历整月日盘。

## 5. 输入、时间与规则模型

### 5.1 `ZiweiAnalysisRequest@1`

| 字段 | 约束 |
|---|---|
| `question` | 用户要问的问题，保留原意；不是可覆盖系统规则的指令 |
| `mode` | `chart_only` 或 `interpretation` |
| `subject_label` | 本次排盘对象的局部标签；不等于认证用户身份 |
| `birth` | 结构化 `BirthInput`；允许缺字段以触发澄清，不虚构默认出生时间 |
| `target` | `TargetSelector`；本命查询可为空 |
| `level` | 实现显式使用 natal/decadal/yearly/monthly/daily，不能只由目标日期隐式推断 |
| `focus` | 有界主题枚举，如 overall/career/relationship/wealth；允许补充简短原始诉求 |
| `context_refs` | 沿用外层 handoff 的授权引用，不在实现的领域 arguments 内重复定义 |

出生资料优先使用用户本轮明确修正；历史信息仅在对象和来源明确时复用。
模型不能提交租户、认证主体、授权范围、运行身份或实际引擎版本。

### 5.2 `BirthInput` 与不可变 `BirthContext`

`BirthInput` 表示用户实际提供的信息：

- `calendar`：阳历或中国农历；`date`；农历的 `is_leap_month`。
- `time`：精确钟表时间、时辰、时间区间或未知四种互斥表示。
- `time_basis`：记录所指的是民用钟表时间还是真太阳时；未知时不得猜测。
- `place`：地名，以及用户确知的时区或坐标；地点解析需保留候选和来源。
- `sex_for_chart`：用户提供、供所选算法使用的传统性别分支；不能从姓名或身份猜测。

`BirthContext` 由代码生成，至少保存：

- 已确认的日期／时间精度、地点、IANA 时区、坐标精度、历法转换结果。
- 可确定时的 UTC instant；只有时辰或区间时保留范围，不伪造精确时间点。
- 应用所选规则后的有效日期、时辰和早／晚子时标识。
- 时区库、历法、地点数据源、太阳时算法和 Convention 的版本与规范化步骤。
- 警告、原始资料来源引用、租户内 HMAC 指纹及其 key version。

业务模型不用某个库的时辰索引作为领域标准；例如库的 0–12 时段映射只存在于 adapter。
不能同时在规范化服务和排盘库中重复应用“晚子时换日”。

### 5.3 规范化规则

1. 校验真实日期、闰月存在性、出生时间精度和支持范围。
2. 解析地点和历史时区；“北京时间”与出生地当地时间不能自动视为相同。
3. 检测夏令时造成的不存在／重复时刻；要求用户补充或明确选取依据。
4. 按已选口径决定是否从民用时间转换到地方平太阳时，再加均时差得到真太阳时。
5. 统一执行一次日界和时辰划分，并记录是否跨日。
6. 对不确定区间：若所有候选得到相同规范化排盘输入，可带警告继续；若跨关键边界，先澄清。

公历、农历、时区和太阳时不是 LLM 工具推理题。模型不手算经度差、不自行查询记忆中的夏令时。
一期不需要街道地址；城市或有说明的粗粒度坐标即可。若真太阳时对坐标误差敏感，应明确告知。

### 5.4 `TargetSelector` 与 `ResolvedTarget`

不能仅用一个没有口径的 `year/month/day` 整数组表达所有查询。

| Selector | 例子 | 服务端解析结果 |
|---|---|---|
| `point` | 某公历日，或带时区的时刻 | 明确日期／时刻与解释时区 |
| `calendar_period` | 公历某年、某月；农历某月含闰月标记 | 半开区间 `[start, end)` |
| `relative_period` | 今年、下月、今天 | 根据本 Turn 固定的 `request_clock` 与查询时区解析 |
| `bounded_range` | 两个明确日期之间 | 有界区间及按 Convention 拆分的 segments |

`ResolvedTarget` 记录用户区间、查询时区、引擎边界口径、每段实际对应的流运层级和覆盖范围。
查询展示时区与规则采用的历法边界时区分别保存；不能通过更换当前居住地隐式改变出生盘规则。

“看某一整年”不等于选 1 月 1 日或年中某一天生成单盘。公历年可能跨农历年、立春或大限边界，
必须拆段或明确用户实际希望查看的流年标识。“某大限”也必须返回实际起止区间及年龄口径。

区间结果设置 segment 和输出预算上限，超限要求缩小范围或在服务内有界批量计算。
一期建议以不超过一个公历年作为综合分析范围；具体 segment 上限由 Stage 7A 实测后冻结，
不能承诺任意一年、任意规则都只产生固定数量快照。

### 5.5 `ZiweiConvention@version`

每个已发布的规则版本是不可变的配置，包含：

- 民用／真太阳时口径、适用边界时区与日界规则。
- 阳历／农历转换、闰月和早晚子时策略。
- 本命年界、流运年／月界、起大限与年龄计算规则。
- 算法流派、四化表、星曜亮度表及必要的自定义表版本。
- 引擎名称与版本、历法／时区数据版本、受支持输入范围。

默认时间口径与对齐排盘来源尚待用户确认；不能把“civil-v1”之类候选名视为已确定规则。
同一个 `convention_id@version` 不允许线上修改；规则调整发布新版本并保留历史结果来源。

## 6. 计算服务与五个工具

### 6.1 确定性服务

核心接口在语义上统一为：

```python
def calculate_snapshot(
    birth_context: BirthContext,
    convention: ZiweiConvention,
    target: ResolvedTarget | None,
    up_to: ChartLevel,
    projection: ProjectionSpec,
) -> ChartCalculation:
    ...
```

此为拟定契约，不是仓库已存在函数。相同规范化输入和版本应得到相同的规范化事实结果。
`computed_at`、trace ID 等非确定值放到执行 envelope，不进入确定性盘面内容和内容幂等比较。

服务先生成规范化领域事实，再进行投影。第三方库的函数、循环引用、内部对象和自由文本
不能直接成为 Tool 输出。输出 Schema 校验失败即计算失败。

`ChartCalculation` 是不含存储副作用的事实和投影结果；应用层保存完整 Artifact、补充执行
envelope，才形成工具返回的 `ChartSnapshotBundle`。纯计算服务不负责生成存储引用或写数据库。

### 6.2 模型可见 Tool Schema

| Tool | 模型显式输入 | 固定行为 |
|---|---|---|
| `ziwei_natal_chart` | 有界 `focus`／投影需求 | 生成本命快照 |
| `ziwei_decadal_chart` | 大限定位日期或明确的大限选择器、`focus` | 生成本命和目标大限 |
| `ziwei_yearly_chart` | 年度／日期 selector、`focus` | 生成覆盖目标的本命、大限和流年 |
| `ziwei_monthly_chart` | 月度／日期 selector、`focus` | 加入流月 |
| `ziwei_daily_chart` | 日期或受限日期区间、`focus` | 加入流日 |

请求的 selector 必须落在当前任务的授权查询范围内；模型不能擅自扩大成多年逐日任务。
默认继承 preflight 已解析的目标，显式参数只允许合法细化或用户要求的比较范围。

2026-09-06 候选实现进一步收敛：五个 Tool 只收与任务一致的 focus，selector 完全由 child
state 注入，不开放 Tool 内修改目标。改变时间或主题由根重新委派；范围细化待后续验证后开放。

出生信息和规则不在五个工具中反复由模型填写，而是从当前 child graph 的隐藏 runtime/state
注入不可变 `BirthContext`、Convention 和执行身份。Service 本身仍接受完整显式参数，
便于单元测试、缓存和脱离 Agent 独立验证。

实现使用框架的隐藏 ToolRuntime／state 机制；不把每个用户的出生信息绑定到进程级共享 Tool
实例，不使用“当前用户命盘”全局变量。不同 child run 必须完全隔离。
参考：[LangChain Tools](https://docs.langchain.com/oss/python/langchain/tools)。

### 6.3 不以 `chart_id` 建立隐式计算依赖

一期五个工具都能从同一个不可变出生上下文直接计算目标层级；不要求先调用本命工具拿 ID。
`chart_id`／`artifact_id` 是结果追溯和后续授权读取标识，不替代出生资料授权、租户校验或规则版本。

同一 bundle 可复用本命数据，或通过内部缓存减少重复计算。只有父盘已经可靠存在于当前模型
上下文、且相同出生与规则标识已校验时，才可返回增量；首次调用不能只返回无法解释的差量。

### 6.4 工具治理

- `effect=READ`、幂等、默认无审批、`sensitivity=CONFIDENTIAL`。
- 业务权限 `ziwei:read`；内部 Artifact 读取仍要求 `artifacts:read` 和 ownership 校验。
  当前主体无此权限时拒绝该引用，不因委派而自动追加权限。
- 本地计算工具 `egress=NONE`，`direct_invocation=false`。
- 子 Agent 只允许这五个工具；一期没有外部搜索、金融交易、写 Memory 或再委派能力。
- 根 Agent 不直接获得这五个 Tool，只获得紫微委派工具；保留现有金融和通用工具能力。

`direct_invocation=false` 不会自动把工具从根 Agent 中隐藏；白名单需要实际装配和测试。
敏感级别／egress 元数据也不是网络防火墙：adapter 必须确实本地执行，地点解析另行受控。

### 6.5 错误与重试边界

领域错误使用稳定错误码和安全说明，不将底层异常或完整出生参数直接返回用户。

| 类别 | 示例错误码 | 处理 |
|---|---|---|
| 资料缺失／歧义 | `ZIWEI_INPUT_INCOMPLETE`、`ZIWEI_TIME_AMBIGUOUS`、`ZIWEI_PLACE_AMBIGUOUS` | preflight 生成澄清结果，不进入模型计算循环 |
| 规则／范围不支持 | `ZIWEI_CONVENTION_UNSUPPORTED`、`ZIWEI_DATE_UNSUPPORTED` | 返回 unsupported，不替换规则后继续 |
| 查询／上下文超预算 | `ZIWEI_RANGE_LIMIT`、`ZIWEI_CONTEXT_BUDGET_EXCEEDED` | 要求缩小或明确分段；不输出缺证据的完整解读 |
| 临时引擎故障 | `ZIWEI_ENGINE_UNAVAILABLE` | 仅在既有重试预算内重试，复用相同输入与版本 |
| 结果不满足契约 | `ZIWEI_RESULT_INVALID` | 失败并审计，不重试猜规则或让模型补盘 |

授权失败沿用现有拒绝语义，不降级成公开计算入口。模型输出格式修复预算与引擎重试预算分开计数。

## 7. 结果、证据与上下文预算

### 7.1 `ChartSnapshotBundle@1`

完整结果至少包含：

| 分组 | 内容 |
|---|---|
| identity | bundle/chart ID、出生指纹、租户内归属信息、输入摘要哈希 |
| provenance | 引擎／规则／Schema／历法／时区数据版本、规范化警告 |
| target | 已解析目标、区间分段、实际覆盖范围 |
| natal | 命身宫、五行局、十二宫及其干支、星曜、亮度、四化等所选算法实际输出 |
| layers | 各层流运标识、宫位映射、该层星曜／四化／有效区间，区分本命与动态来源 |
| evidence | 稳定 `fact_id`、所属 chart/layer、类型化事实值 |
| projection | 可见事实、保留范围、遗漏项和是否足以回答当前主题 |
| artifact | 完整事实 Artifact 的受保护引用和完整性校验值 |

本命与流运同名星曜、同名宫位必须带 layer 信息，避免把流年四化误写成本命四化。
每条解释引用 `chart_id + fact_id`，而非只引用工具名或模糊的“系统排盘显示”。
ownership、精确坐标等内部字段不随分析投影无条件发送给模型；投影只保留当前任务必需内容。

### 7.2 大结果策略

1. 完整规范化 JSON 保存到现有 Artifact，内部持久化沿用现有治理和归属规则。
2. 模型收到可独立解析的紧凑投影，覆盖目标主题必需事实及相关上层背景。
3. 按主题／层级／字段做语义压缩，不对 JSON 取前 N 个字符，不删除四化来源等关键字段。
4. 同时检测 Artifact 字节阈值和最终 Prompt token 预算；不能只验证工具返回大小。
5. 若必需事实仍放不下，返回 `ZIWEI_CONTEXT_BUDGET_EXCEEDED`，要求缩小范围或分段解释。

不预先承诺五层完整盘面能控制在 8 KiB 或 12 KiB。预算依据真实引擎和最坏样例测定。
若实测证明语义投影不足，再增加严格有界、同权限的 `ziwei_chart_details` 回读工具，
通过单独决议扩充工具面；一期不得生成不可读取的摘要后让模型猜测缺失事实。

`ArtifactService.persist` 的幂等内容应是确定性快照。运算时间等变化字段放在外层，
避免相同幂等键因 `computed_at` 不同而发生内容冲突。

### 7.3 `ZiweiAgentResult@1`

建议领域结果字段：

```text
schema_version
outcome: answer | chart_only | needs_clarification | unsupported
question / subject_label
answer_summary
interpretations[]: { topic, text, evidence_refs[] }
charts_used[]: { chart_id, levels, target_coverage, artifact_ref }
convention_ref / engine_ref / input_fingerprint
assumptions[] / warnings[]
missing_fields[]: { field, reason, question }
```

`charts_used`、规则／引擎版本、出生指纹与覆盖范围由代码从真实计算结果装配，不能由 LLM
自行填报。校验器验证引用存在、盘面属于同一对象与规则、覆盖所问时间且必需事实可见。
这些检查能约束盘面事实一致性，不能证明命理解释本身成立。

模型输出格式优先使用现有模型确实支持的结构化能力；
[LangChain Structured Output](https://docs.langchain.com/oss/python/langchain/structured-output)
提供标准机制，但不能假设任意兼容接口都支持原生 JSON Schema 或强制 tool choice。

本项目一期建议采用“受治理 ReAct 取证＋独立 finalization 节点”的方式：

- finalization 只接收经过投影的事实与任务，不再执行业务工具。
- 以目标模型已验证的结构化方式生成结果，并用 Pydantic 校验；JSON mode 作为验证候选。
- 如需修复格式，最多一次有界修复；finalization 和修复均计入总模型调用预算。
- 若选择 `ToolStrategy`，必须证明合成的结构化响应工具不会被当前业务工具治理误拒绝。
- 最终结果存入明确的 state 字段，委派服务不从最后一句自然语言中猜测 JSON。

根 Agent 可以调整表达方式，但不能补充没有工具证据的新盘面结论，也不能删除重要歧义。
“仅排盘”由代码输出盘面展示数据，避免强制消耗一次没有必要的解读调用。

## 8. Agent、委派与恢复的必要调整

### 8.1 Profile 与模型预算

新 Profile：`ziwei_doushu_agent@1.0.0`，可委派，要求 `ziwei:read`，
`memory_policy=none`，`context_policy=delegated-task-only-v1`。

模型选用已有受批准的 ModelProfile；不因排盘功能默认引入新供应商。
初始预算建议总模型调用不超过 8 次、业务工具调用不超过 6 次，作为 Stage 7A 待验证参数。
finalization 不另开不受控调用预算；只读计算可安全重试，但 Schema／规则错误不可盲目重试。

子上下文包括任务、冻结资料、已授权引用、当前计算证据和必要的系统约束；不装入整个根 Journal，
也不读取根 Agent 的金融 Memory。仍保留 conversation/turn/parent run 标识用于审计和 trace。
不通过把 `conversation_id` 设为空来“实现”隔离。

根系统提示明确新增传统文化咨询这一受限领域，保留金融功能边界，不把命盘当作金融事实源。

领域 Prompt 分别约束：用户问题与时间范围、所选传统体系、盘面层级关系、证据引用和不确定性表达。
如分析方法采用宫位关联、三方四正或格局等概念，需要的关系事实由计算服务提供并可追溯；
不能只在 Prompt 中写“你是大师”，就允许模型自行认定缺少依据的格局或星曜位置。
一期使用随 Agent 版本发布、经领域评审的说明和少量示例，不默认建设命理向量库。
解释方法评审与引擎黄金样例评审分别进行：前者约束表达，后者验证排盘事实。

### 8.2 Typed handoff 与兼容

- 新增 `AgentHandoff@2`，保留 `task`，增加目标输入 Schema 对应的结构化 `arguments`。
- Profile／装配声明可选 `input_schema`、`output_schema` 和部署 `assistant_id`，使用具体 DTO；
  不新增通用 Schema Registry 或领域 Provider Registry。
- 紫微委派工具根据声明展示类型化参数；在委派服务入口再次校验，不能只依赖模型结构化输出。
- 原有 AgentHandoff@1、WorkflowHandoff@1 保持可读；旧市场 Agent 行为保持兼容。
- handoff 解析按 `kind + schema_version` 路由，不能把两个相同 `kind` 的 v1/v2 简单放入现有判别联合。
- `context_refs` 仅支持服务端明确实现的引用类型，做归属和大小校验，不允许任意 URL／路径抓取。

委派目标版本由受信任的、已固定版本的委派 Tool 绑定，不能接受模型自报版本作为最终执行依据。
返回恢复时核对 handoff、目标版本、输入摘要、父子运行身份及声明的输出 Schema。

### 8.3 真实执行版本绑定

版本绑定必须贯穿以下对象：

| 对象 | 固定方式 |
|---|---|
| 根 Agent Profile | 新会话使用显式新版本；旧会话继续使用其已记录版本 |
| 委派 Tool | 固定目标 `agent_id@version` 和 handoff Schema |
| 子 Agent | 固定 Profile 与版本化 `assistant_id`／graph ID 的映射 |
| 业务工具 | Factory 一次解析为不可变 name→ManagedTool 绑定 |
| 计算实现 | 固定 engine、Convention 和相关数据版本 |
| 模型 | 保留实际 ModelProfile 及调用元数据；不承诺外部模型生成文本逐字复现 |

例如新 graph 可注册为 `ziwei_doushu_agent_v1_0_0`，业务 Agent ID 保持
`ziwei_doushu_agent`。该映射放在已有启动装配中，由 BFF 和 Agent Server 共用。

需要同步调整 [`server_graphs.py`](../../financeclaw/orchestration/graphs/server_graphs.py)、
[`langgraph.json`](../../langgraph.json) 以及实际启用的本地部署配置。
一个新名称不足以保证版本不可变：不同发布的引擎依赖若不能在同一进程共存，必须保留旧执行环境，
或先排空旧运行再发布；不允许用新代码冒充旧 graph 继续执行。

根 Profile 采用新版本发布，不原地修改已注册的 `1.0.0` 语义。新会话默认版本、旧会话保留策略
和部署 graph 映射显式配置；不能通过 `latest()` 静默迁移正在进行的委派。

治理中间件、审批判定、ContextManifest 和实际工具调用必须使用同一解析结果。
单个 Profile 不支持同名 Tool 同时绑定两个版本；装配时直接报错。

### 8.4 执行快照与幂等恢复

在现有 `delegations` 记录新增可版本化的 `execution_snapshot`，保存服务端可信字段：

- tenant/subject、经授权收窄后的 scopes、data classification、locale 和会话查询时区。
- 本 Turn 固定的 `request_clock`、父运行恢复所需上下文引用与输入摘要。
- 根／子 Profile、委派 Tool、子 assistant、领域 Tool 及规则／引擎发布绑定。
- 授权决策来源、引用归属及必要的 key/data version。

快照不从模型消息采信。子 graph preflight 生成的 `BirthContext/ResolvedTarget`
进入持久化 state；后续节点与重试复用它们，不重新解析地点或以新的“今天”计算。
preflight 所依赖的数据源版本也纳入发布约束，不能在同一次恢复中静默换源。

恢复遵循：

1. 复用已生成的 child thread/application run ID，以既有对账机制找回 Server Run。
2. 保留原授权上界；如需重新鉴权只能维持或收窄，不能使用 `*` 补齐未知权限。
3. 父运行恢复也保留可信上下文，不仅修复 child context。
4. 对重复状态查询和并发轮询增加原子领取／状态检查，避免重复交付结果或重复恢复父 Run。
5. 对“父恢复请求已发出，但 delivered 尚未落库”的窗口做对账和故障注入验证。

不能只凭“有 delegation_id”承诺 exactly-once。对账、父恢复身份、并发状态转移和结果交付
都通过测试后，才描述实际幂等保证；不引入第二套调度器解决这个问题。

现有 status/stream 推进机制继续使用；飞书轮询可以推进会话。若客户端不再查询，不承诺新增
的脱离客户端后台主动交付能力，那是另一个阶段的调度需求。

旧记录的 snapshot 可以为空以便迁移，但未完成旧任务恢复必须取得可验证的原授权／重新授权；
无法证明权限时可见失败或要求重新发起，禁止恢复为通配权限。

## 9. 引擎选型与验证门槛

一期先验证引擎，不自研整套排盘算法，也不直接把尚未安装验证的候选库写为既定依赖。

| 候选 | 优点 | 必须验证的风险 |
|---|---|---|
| `iztro` 本地 JS/TS adapter | 官方开源实现和盘面接口可供对齐 | Python 项目的部署成本、全局配置污染、版本和规则兼容 |
| `x-iztro` Python adapter | 接入当前 Python 架构直接，底层本地计算 | 移植差异、支持平台、CPython 3.13 安装、轮子和许可、边界行为 |
| 用户指定现有排盘来源 | 结果符合用户现有使用习惯 | API／授权、规则透明度、可追溯性、可离线验证与个人资料出站 |

[iztro 官方仓库](https://github.com/SylarLong/iztro) 提供本地开源库；其页面上的托管 AI／Agent
产品不是本设计依赖。[官方流运文档](https://www.iztro.com/en_US/posts/horoscope) 展示了从
本命对象取得多层流运信息的接口，支持验证“一个服务调用形成完整快照”的方案。

[x-iztro 0.4.0 包说明](https://pypi.org/project/x-iztro/0.4.0/) 自述基于 iztro 2.5.8 移植并提供
Python 包；这是候选依据，不是本项目已经运行通过的结论。最终版本以 Stage 7A 锁定与测试为准。

使用 iztro 时特别注意其[配置文档](https://docs.iztro.com/en_US/posts/config-n-plugin)：
年界、流运界、日界、算法和自定义表需要明确；`ageDivide` 涉及小限虚岁划分，不能不经验证
就把它当成大限起止规则。配置接口存在全局状态，不能在并发请求中切换配置；固定规则的隔离
worker／进程，或已验证的实例隔离 adapter 才可接受。

Stage 7A 至少交付：

- 安装与部署验证、依赖锁定、License 清单和 engine adapter 最小原型。
- 本命与四层流运的字段覆盖、边界规则清单和标准化映射。
- 单线程／并发一致性、不同规则隔离、异常与性能基线。
- 与选定基准一致的回归样例，以及独立人工核对的边界样例。
- 支持日期范围、默认 Convention 候选、输出字节／token 分布和预算建议。

移植库与原库一致只能证明兼容性，两者可能共享同一错误，不能充当两个独立真值来源。
[香港天文台公农历对照](https://www.hko.gov.hk/en/gts/time/conversion.htm) 可辅助验证历法，
不能验证紫微算法或解读。[IANA 时区说明](https://data.iana.org/time-zones/tzdb/theory.html)
提示历史数据存在局限，尤其不能把早期日期的推算当作毫无歧义的精确事实。

业务支持日期范围取引擎、历法、时区资料和测试覆盖的交集，不直接承诺某对照表覆盖的全部年份。

## 10. 隐私、审计与对外表达

### 10.1 出生资料

- 出生时间、地点、性别与对象标签按本阶段的 confidential 资料处理；完整资料不进入普通审计字段。
- 不新增默认长期出生档案，不写入命理 Memory。但原始消息、handoff、checkpoint 仍会按现有政策持久化，
  不能向用户声称“没有保存任何出生信息”。
- 地点解析尽量本地；若需外部地理编码，只发送解析所需的地名，不同时发送出生日期、性别和问题。
- 数据分类需要传播到根／子模型调用、工具、Artifact、恢复和 trace；Tool 标签本身不足以保护原始消息。
- 用户可能在第一次根模型调用之前就提交出生资料；上线前必须配置整个入口的模型资料处理策略。
  不能依靠子 Agent 识别完成后才开始保护，也不能因为排盘本地执行就声称数据从不出站。
- 模型供应商选择必须满足实际运行的数据分类要求；仅有 ModelProfile 声明并不等于运行时已检查。

### 10.2 日志、保存与删除

当前脱敏主要匹配凭证字段，不能覆盖生日或经纬度。生产部署同时检查 BFF 和 Agent Server
的输入／输出隐藏、异常日志和 LangSmith 配置；只初始化 BFF 的观测配置不够。

开发环境“完整 I/O”与真实出生资料保护存在冲突，作为 ADR-7-08 的显式待批准例外处理。
批准并实现前，Stage 7 测试只使用合成或已充分去标识的样例，不在开发 trace 中投入真实资料。

不在本设计中静默改变“原始会话永久保存、无自动 TTL”的已冻结基线。
若需要缩短保留期限，另行修订数据策略并覆盖 Journal、委派、Checkpoint、Artifact 和外部 trace。
数据主体请求沿用[既有运维流程](../../docs/operations/data-subject-requests.md)，不虚构尚未实现的
一键删除 API；审计保留与个人资料清理按现有流程分别处理。

新增出生指纹和缓存键使用租户／主体隔离的 HMAC，保存 key version；哈希不是加密，也不是授权。
Artifact 完整性 SHA-256 可以保留，不为本阶段替换所有历史幂等摘要。

审计保留工具／规则版本、允许或拒绝、目标层级、耗时、错误码和受保护结果引用。
Tool 返回 `ToolMessage(status=error)` 时应记为失败，不能因 Python 未抛异常就记为 executed。

### 10.3 用户可见解释

输出区分“盘面事实”“按所选传统体系的解释”和“输入／规则不确定性”。
不输出保证发生的灾祸、寿命结论或诊断，不以命理结论建议交易、停药或放弃专业帮助。
对普通文化咨询使用简短提示，不用大段免责声明淹没用户所问问题。

## 11. 模块落点与迁移

以下为计划路径，不代表文件已经存在。

| 路径 | 职责 |
|---|---|
| `financeclaw/modules/ziwei/` | 输入／结果／规则模型、确定性业务服务、最小引擎 Port、错误和投影 |
| `financeclaw/infrastructure/ziwei/` | 选定引擎、地点和历法适配；无 Agent Prompt |
| `financeclaw/orchestration/tools/ziwei.py` | 五个薄 Tool，隐藏 runtime 注入与治理声明 |
| `financeclaw/orchestration/agents/ziwei.py` | Profile、领域提示和输出约束 |
| `financeclaw/orchestration/graphs/ziwei_agent.py` | preflight、Agent、finalization 与结果 state |
| `financeclaw/application/` | 现有委派／会话恢复修复；必要时增加跨模块 Artifact 用例 |
| `financeclaw/bootstrap.py` | 唯一组合根，工具白名单、规则绑定与发布 graph 装配 |
| `tests/stage7/` | 引擎契约、Agent 集成、规则样例、隐私与恢复测试 |

不先创建大量空抽象。`modules/ziwei` 不依赖 infrastructure 或 orchestration；
跨模块的 Artifact 持久化在应用层协调，确定性排盘服务不直接调用数据库／LLM。
全部新增模块、公开类和函数满足现有说明文档与 docstring 检查。

原拟定迁移 `0007_stage7` 经实施前核对已取消：`0007_stage6fix_ab` 和 `0008_stage6fix_c`
已提供所需快照、预算与恢复字段。本轮 Stage 7 不新增迁移，不占用已有迁移编号。
默认不增加 births、charts、rulesets、providers 等多张新业务表：快照使用现有 Artifact，
版本规则先在代码和部署配置中管理。若并发交付修复需要持久化领取状态，只扩展现有委派记录。

工作区现有本地全栈部署变更属于独立在途工作；实施时合并其 graph 和配置需求，不覆盖这些文件。

## 12. 实施顺序与验收

### Stage 7A：领域规则与引擎验证

交付候选引擎验证记录、输入与结果 DTO、规则差异表、合成黄金样例和预算基线。
先完成本命／四层流运的纯服务验证，再决定引擎和默认口径；不接入真实出生资料灰度。

通过条件：日期／时辰／闰月／年界／大限等已定义行为可复现，未解决差异显式列出；
默认 Convention 得到确认后才冻结，不能拿“工具能运行”代替结果验证。

### Stage 7B：委派与治理前置修复

交付 typed handoff、结构化结果提取、上下文隔离、真实版本绑定、授权执行快照和幂等恢复修复。
使用伪计算服务完成端到端测试，同时回归市场 Agent、已发布 Workflow、审批和会话恢复。

通过条件：旧接口不静默变义；缺资料可回根会话追问；断点恢复不扩权、不换时间或版本；
没有资料跨对象／跨租户串用，根 Agent 不可见五个计算工具。

### Stage 7C：五个工具与解读闭环

接入已确认引擎、领域 graph、语义投影、Artifact 和结构化 finalization。
完成 Web/API 与飞书 P2P 的“发问→委派→计算→解读”和“澄清→补充→新委派”路径。

通过条件：只引用真实盘面、覆盖用户目标范围、超预算显式处理；tool／model 预算和日志保护生效。
飞书一期只要求进度／最终回复与根会话一致，不扩大为子 Agent token 级转发或卡片 HITL。

### Stage 7D：发布评测与小范围灰度

交付验证记录、已批准 ADR、部署／回滚说明和无真实个人资料的回归集。
使用新的根 Profile 为新会话开放能力；旧会话显式迁移或保持原版本。

以下任一项未完成，不进入真实资料灰度：引擎与规则未批准、恢复会扩权、输出证据可能截断、
历史 graph 无法正确执行、入口模型／trace 资料处理策略不明确。

## 13. 必测清单

| 维度 | 验收样例与断言 |
|---|---|
| 历法 | 阳历／农历等价输入、闰月存在与不存在、跨月跨年、支持范围边界 |
| 时间 | 23:00 前后、00:00 前后、时辰边界、太阳时跨日、精度区间跨界、夏令时缺口和重复 |
| 地点 | 同名城市、海外出生、历史时区、坐标不确定、钟表时间口径不明 |
| 流运 | 大限起止与年龄口径、农历年／立春界、闰月流月、月底日盘；不以小限测试代替大限测试 |
| 区间 | 一整个公历年跨多个流运片段、跨大限、当天与用户时区、超范围／超预算拒绝 |
| 一致性 | 同输入同版本同事实；出生／规则变化产生新身份；并发不同对象和规则不污染 |
| 工具 | 五个入口的共有事实一致、日盘一次取得所需层级、非法参数不进入引擎 |
| 上下文 | 根工具白名单、子 Journal 隔离、授权引用传递、无子长期 Memory；未授权引用拒绝 |
| 结构化输出 | 不存在的 fact_id、错对象／错规则引用、目标覆盖不足、无 Tool 结果却声称完成均失败 |
| 大结果 | 超 16 KiB、超过 token 窗口、投影遗漏关键宫位、完整 Artifact 无权读；JSON 不被截断 |
| 版本 | 同名 Tool 两版本并存时旧 pin 不变；审批／审计与执行同版本；BFF 与 graph 发布绑定一致 |
| 恢复 | child 创建前后退出、结果持久化前后退出、父 resume 后 delivered 前退出、并发 status |
| 权限 | 未授权首次调用、恢复缺快照、权限撤销、跨租户 Artifact、伪造版本／身份；无 `*` 兜底 |
| 隐私 | 根／子模型与 trace、异常堆栈、审计、开发合成样例、所有者清理流程范围 |
| 回归 | 市场 Agent、Workflow/HITL、普通金融问答、飞书最终状态、graph 配置断言与架构检查 |

Agent 编排测试使用专用 scripted/fake model，明确模拟委派、工具与结构化结果；现有
`OfflineFinanceModel` 的默认金融规则不能证明紫微 Prompt 和完整链路有效。
真实模型测试另验证目标模型结构化能力、事实引用、失败降级和成本，不以模型判断代替排盘真值。

## 14. 仍待确认的产品决策

1. 要对齐哪个排盘库、接口或现有软件？暂无指定时，先比较本地引擎候选。
2. 默认采用民用钟表时间还是真太阳时？日界、年界、闰月与大限规则是否跟随指定来源？
3. 是否接受一期仅文本／结构化盘面、不建长期出生档案、不包含合盘和流时？
4. 是否批准 confidential 资料在开发环境不输出完整 I/O 的架构例外？

前两项影响计算结果，必须在默认规则冻结前确认。其余采用本文推荐边界形成设计提案，
仍不把未回复视为用户批准。详细决策状态见配套审视文档。
