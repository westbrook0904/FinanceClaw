# M6c：紫微领域治理与原生工具接入

日期：2026-10-15。基线为本地提交 `33d59b8`（M6b 固定 Java 排盘，未推送）。本阶段完成出生资料校验与澄清、固定发布、归属 HMAC、区间事实、结果预算及 AgentScope 原生 Toolkit 接入。本阶段已本地提交 `ef4057b`。默认关闭，只允许显式开发模式；后续独立进程恢复和部署验收见 [M6d](m6d-recovery.md)，真实模型验收仍待完成。

## 如何调用

工具仍为五个固定入口：`ziwei_natal_chart`、`ziwei_decadal_chart`、`ziwei_yearly_chart`、`ziwei_monthly_chart`、`ziwei_daily_chart`。调用者必须拥有 `ziwei:read`，读取完整结果另需 `artifact:read`。工具直接注册到现有 `GovernedTools`，不增加 HTTP 排盘旁路、MCP 服务或新 Agent 协议。

流年示例（合成资料）：

```json
{
  "birth": {
    "calendar": "solar",
    "date": {"year": 1990, "month": 6, "day": 15},
    "time": {"kind": "shichen", "shichen": "yin"},
    "time_basis": "civil",
    "place": {"name": "上海"},
    "sex_for_chart": "male"
  },
  "year": 2026,
  "mode": "chart_only",
  "focus": "overall"
}
```

成功结果包含 `status`、`mode`、`projection`、`artifact`、`time_context`。投影提供带 `fact_id` 的事实、覆盖区间、规则版本和省略项；完整事实保存在已有 Artifact 表，使用 `read_artifact` 的 `/facts` 路径分页读取。模型只能按指定层级、主题和事实作解读；`chart_only` 要求只展示盘面。真实模型是否遵守这些要求属于 M6d 验收范围，当前离线默认模型仍只返回收件文本，不会自行选择工具。

结构校验复用 AgentScope `ToolValidator`。生产 Schema 从冻结的旧 Schema 整理，保留五入口的字段集合、类型与枚举；允许不完整的出生日期、地点和日期区间进入领域层，一次聚合缺失资料。结构错误拒绝执行，语义上的缺失、模糊子时、时区冲突等使用原生 `ToolResultBlock.suspended` 暂停。

澄清使用已有 Interaction API。答案只接受出生资料与日期修正，例如 `answer: {"birth":{"sex_for_chart":"male"}}`；同类嵌套资料合并，变更时间精度会替换旧时间表示，变更日期选择器会移除旧选择器。不允许通过答案修改身份、规则、问题意图或展示模式。接口先复核原 `ToolUseBlock`、action hash、state version、grant revision 和 release，再验证补全后的语义；未补全返回 `422 ziwei_input_incomplete`，原 Interaction 继续等待。

## 固定规则与边界

| 项目 | 本阶段行为 |
| --- | --- |
| 发布 | 新规则 `financeclaw-ziwei-java-civil@1.0.0`；快照包含引擎、历法、TZDB `2026b`、Schema 摘要、HMAC key version/ref、查询时区和预算，密钥值不进入发布 |
| 出生历法 | 公历或农历必须明确；农历必须确认是否闰月，日期经历法适配器校验 |
| 时制 | 保留旧契约的 `civil` 默认；明确传入 `null` 需澄清，真太阳时不支持 |
| 地点 | 离线解析北京、上海、广州、深圳、成都、香港、台北；已知城市与地区/时区冲突需确认；其他地点必须显式提供 IANA 时区，不做地理编码核验 |
| 时间精度 | 不猜默认时间；子时区分早晚；时辰和区间不生成精确 UTC；跨时辰、跨日、DST 缺口或未消歧的重复时刻需澄清；经纬度不触发真太阳时校正 |
| 相对日期 | 使用数据库 `business.turns.created_at`，查询时区来自固定发布；不会随着重试/澄清后的 attempt 或服务器当前日期变化，也不使用出生地时区代替查询时区 |
| 区间 | 半开 `[start,end)`、最多 366 天；流日最多 31 天；扫描每日所需层级，将相邻相同事实合并，最多 32 段 |
| 有效流运 | 拒绝出生前、尚未进入有效大限、超出大限表的目标；不把底层兼容算法的童限回退当作有效大限 |
| 主题 | 只裁剪投影；保留命宫、身宫、主题宫及关联宫位，包含动态宫位的关联本命宫，不改变完整盘面身份 |

新的 Java convention 不等于旧 Python convention。M6b 的旧输出兼容性测试与固定 JDK TZDB 检查仍有效，但不能据此声称所有地区、历史时刻与 Python tzdata 等价。原有 Turn 继续按其 release hash 执行；未加载的旧发布会失败关闭，不改用当前发布。

## 归属、隐私和预算

出生指纹采用 HMAC-SHA256，绑定 tenant、subject、出生资料、规范化日期/时辰/时区、规则和 key version。身份仅从 Claim 读取，不接受模型参数；没有共享“当前用户/当前命盘”缓存，也没有外部可提交的 BirthContext。每个 Turn 保存密钥绑定回执，同一版本下偷偷替换密钥会拒绝继续或重放。密钥轮换应使用新的 key version 和发布。

归档复用 `GovernanceService`、MyBatis、现有事务 fence 和 `CapabilitySteps`。同一 Turn 的同一 tool call 重复或并发执行只提交一个结果/Artifact；输入变化会冲突。不同调用可有相同 `chart_id`，但不共享跨用户制品。读取重新检查 tenant/subject、原会话授权、`ziwei:read`、发布可用性和内容摘要。

启用既有记忆治理时，忘记操作推进 privacy epoch，旧紫微 Artifact 读取及在途工具/澄清恢复随即失效。工具结果、盘面和澄清答案不作为长期记忆抽取源，也不自动生成记忆事实。**此处没有新增物理删除会话、SDK checkpoint 或 Artifact 的接口**；记忆的 forget 并非全系统数据删除。关闭记忆功能时，不提供其 epoch/forget 语义，仍执行用户归属与授权检查。完整会话保留/删除政策需在真实环境上线前确认。

完整 Artifact 上限 2 MiB；模型投影上限 14,000 UTF-8 字节，最终工具信封预留 1,024 字节。预算在写 Artifact 前检查，超限返回稳定错误码并要求缩小范围，不截断成“成功”结果，也不把缺失事实解释成不存在。模型调用仍经过现有完整请求预算机制，投影预算不会替代模型上下文预算。主模型会看见工具输入与必要盘面事实，因此只应使用已授权的模型部署。

原始出生参数不会进入工具进度事件或澄清问题文本。它们仍可能存在用户消息、已授权模型请求及 SDK 持久状态中；Artifact 中的盘面事实也属于受保护资料，不能当作匿名公开数据。

## 开发配置

在现有独立开发数据库及 API 配置上设置：

```bash
export FINANCECLAW_ZIWEI_ENABLED=true
export FINANCECLAW_ZIWEI_DEVELOPMENT=true
export FINANCECLAW_ZIWEI_KEY_REF=ZIWEI_HMAC_KEY
export FINANCECLAW_ZIWEI_KEY_VERSION=dev-v1
export FINANCECLAW_ZIWEI_QUERY_TIMEZONE=Asia/Shanghai
# 通过环境或 Spring configtree 注入 ZIWEI_HMAC_KEY，至少 32 UTF-8 字节。
# 如需验证 forget/epoch，启用既有 FINANCECLAW_MEMORY_ENABLED=true。
scripts/mvn.sh spring-boot:run
```

只设置 enabled 会拒绝启动；缺失或短密钥、无效时区、TZDB 变化同样拒绝。关闭时不读取紫微密钥。M6c 当时仅提供开发进程配置；后续 Compose secret 与容器验收见 [M6d](m6d-recovery.md)，仍未注入真实模型凭据。

## 验证与复现

```bash
scripts/mvn.sh -B -ntp clean verify spotless:check
python3 scripts/collect-evidence.py
python3 scripts/check-delivery.py
```

**本次完整回归 615 项通过**（495 单元/契约、120 集成），0 失败、0 错误、0 跳过；`clean verify spotless:check` 和静态交付检查通过。新增 56 项领域测试及 14 项集成测试，机器证据与源码指纹见 [M6c 测试记录](evidence/m6c-domain-results.json)。未重跑 M5 Compose，也未运行远程 CI。覆盖：

- 冻结旧领域服务的 8 组样本、4 种主题，共 32 项逐事实/区间/筛选或错误比较；包括跨农历新年与月界，不只比较结果摘要。
- 出生资料聚合澄清、日历、城市时区冲突、DST fold、精度、输入/答案越权、HMAC、发布与预算边界。
- 五入口原生 ToolBase、事务幂等、并发用户隔离、并发重复调用、隐私锁与授权锁竞争、权限撤销、密钥漂移、privacy epoch、无副作用预算失败。
- 真实 AgentState 暂停后换 attempt 恢复：不完整答案不被消费；新发布不会替代旧 Turn 的规则/查询时区；原创建时间固定相对年份。

测试使用合成资料、脚本模型及临时 PostgreSQL。旧算法只用于生成冻结基准，Java 测试和运行时无需 Python 算法包。刷新领域基准：

```bash
/path/to/FinanceClaw/.venv/bin/python -B scripts/export-ziwei-domain.py \
  --python-root /path/to/FinanceClaw --output target/m6/domain
```

生成器核查 M6a 源码/依赖指纹及冻结的旧 `service.py` 摘要，变化时拒绝替换基准。本次重新生成的 `domain.json`、`manifest.json` 两文件逐字节一致；输出先留在 target 审查，不在运行时查询基准。完整测试结果及代码指纹由 `collect-evidence.py` 记录。

后续 **[M6d](m6d-recovery.md)** 已补充独立进程崩溃/重启下的澄清和归档恢复、取消/隐私竞争、实际部署镜像的五工具无 Python 运行依赖验收，以及真实模型工具选择与引用核对的验收入口。真实模型尚未配置，普通启用门禁仍保留；当前结果与部署配置以 M6d 记录为准。

## 代码入口

| 入口 | 职责 |
| --- | --- |
| [ZiweiInputs](../../src/main/java/com/financeclaw/ziwei/ZiweiInputs.java) / [原生输入适配](../../src/main/java/com/financeclaw/infrastructure/agentscope/capability/AgentScopeZiweiInputs.java) | 出生/时间/地点语义，以及原生 Schema 校验和补充合并 |
| [ZiweiPolicy](../../src/main/java/com/financeclaw/domain/release/ZiweiPolicy.java) | 固定规则、Schema 摘要和预算配置 |
| [ZiweiCalculation](../../src/main/java/com/financeclaw/ziwei/ZiweiCalculation.java) | 事实、分段、主题投影和预算 |
| [ZiweiService](../../src/main/java/com/financeclaw/app/ziwei/ZiweiService.java) / [原生工具适配](../../src/main/java/com/financeclaw/infrastructure/agentscope/capability/ZiweiToolContribution.java) | R5b 接替原 ZiweiTools：Claim 鉴权、固定时钟、暂停/恢复、事务回执与归档；最新边界与验证见 [R5b](../architecture/r5b-implementation.md) |
| [GovernedTools](../../src/main/java/com/financeclaw/infrastructure/agentscope/execution/GovernedTools.java) | 原生 Toolkit 注册与恢复适配 |
| [领域测试](../../src/test/java/com/financeclaw/m6/ZiweiDomainTest.java) / [集成测试](../../src/test/java/com/financeclaw/m6/ZiweiToolsIT.java) | 可复现的验收证据 |
