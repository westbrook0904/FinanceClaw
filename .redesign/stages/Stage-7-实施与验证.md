# Stage 7：候选实现与验证记录

状态：第一批候选实现；默认关闭，不代表 Stage 7 全部验收或批准真实资料灰度。

日期：2026-09-06

后续更新（2026-09-07）：已按用户确认新增文本解读热修复，详见
[文本解读热修复实施与验证](./Stage-7-文本解读热修复-实施与验证.md)。下文保留首批 V1 的历史记录；
当前新会话使用根 `1.4.0`／紫微 `2.0.0`，不再要求 LLM 输出解读 JSON 或逐条引用，温度未变。

依据：[设计说明](./Stage-7-Ziwei-Domain-Agent-设计说明.md)、
[设计审视与待确认决议](./Stage-7-设计审视与待确认决议.md)。
用户本轮授权按最新设计开始实现；未将未回复的排盘基准、默认时间口径视为已批准。

## 1. 本轮实际交付

- `ziwei_doushu_agent@1.0.0`：独立 child graph，task-only、无长期 Memory、无下级委派。
- 五个受治理 READ Tool：本命、大限、流年、流月、流日，均为 `1.0.0`。
- 确定性规范化、排盘、区间拆段、证据投影和受保护 Artifact 存储。
- 原生 LangGraph 执行路径：预检 → ReAct 取证 → 结构化 finalization；缺资料直接结束 child，
  由根 Agent 追问，不创建紫微 child 的用户交互 interrupt。
- 通过既有 typed delegation V2 交付 `ziwei_result`，不从最后一句自然语言猜测成功。
- 显式启用后，新会话使用 `finance_agent@1.3.0`；已有根 `1.2.0` 与市场 Agent `1.2.0`
  保持原发布，不给旧根追加工具。根只看到紫微委派 Tool，看不到五个排盘 Tool。

代码入口：

| 层 | 文件／目录 | 职责 |
|---|---|---|
| 领域 | `financeclaw/modules/ziwei/` | 冻结 DTO、规则、输入规范化、纯计算、投影 |
| 基础设施 | `financeclaw/infrastructure/ziwei/x_iztro.py` | 本地候选引擎、固定时区数据库 |
| 应用 | `financeclaw/application/ziwei_service.py` | 身份复验、HMAC 隔离、Artifact |
| 工具 | `financeclaw/orchestration/tools/ziwei.py` | 隐藏 runtime 注入、范围检查、真实证据写 state |
| Agent | `financeclaw/orchestration/agents/ziwei.py` | 发布档案、领域 Prompt、权限与预算 |
| Graph | `financeclaw/orchestration/graphs/ziwei_agent.py` | 预检、取证、finalization、结果校验 |
| 装配 | `financeclaw/bootstrap.py`、`server_graphs.py` | 候选开关、版本化 graph、显式白名单 |
| 测试 | `tests/stage7/` | 真实引擎、原生父子 graph、持久预算和失败路径 |

## 2. 对原设计前置缺口的核对

开始本轮时，Stage 6 Fix A/B/C 已在仓库实现。以下能力直接复用，不再造一套：

- 领域输入／输出 Schema、`output_state_key`、版本化 assistant、发布快照；
- child task-only 上下文、授权上界、固定 Tool 解析；
- 根任务树持久预算、operation 领取／回执／对账、父恢复交付和交互持久化。

现有迁移头为 `0008_stage6fix_c`，前一迁移为 `0007_stage6fix_ab`。本轮没有新增数据库迁移。
设计稿中的拟定 `0007_stage7` 不再执行。既有恢复／并发／审批测试纳入全仓库回归。

本轮只补本功能必需的薄扩展：Agent 数据分类、Factory 自定义 state／middleware／有界重试配置、
结构化 Tool Result 禁止 offload 截断、已处理 Tool 错误的失败审计，以及紫微预检和 finalization
恢复时的发布快照复验。默认档案的序列化不增加 `internal` 字段，避免无故破坏旧快照比较。

## 3. 引擎与规则的真实状态

已安装并执行 `x-iztro==0.4.0`，固定 `tzdata==2026.3`，以可选 `ziwei` extra 和 `uv.lock` 管理。
仅开启候选时加载引擎；版本不匹配启动失败，不使用系统时区数据库静默兜底。

候选规则引用为 `x-iztro-civil-candidate@1.0.0`：

- 民用钟表时间；`year_divide/horoscope_divide/age_divide=normal`；
- `day_divide=forward`、`algorithm=default`、`fix_leap=true`；
- 日级查询使用明确的当地历法日期标签，交给引擎的流运时辰固定为早子时；
- 晚子时换日只由引擎执行一次，规范化层不提前加一天；
- 每次计算使用独立的 Astro／ChartConfig，不按用户修改全局规则。

这是可复现的验证候选，不是已选定的产品默认流派。依赖安装元数据分别声明 x-iztro 为 MIT、
tzdata 为 Apache-2.0；未以此替代发布前依赖漏洞与许可证审查。

上游与独立历法依据：

- [x-iztro 0.4.0 包与接口说明](https://pypi.org/project/x-iztro/0.4.0/)。
- [iztro 配置说明](https://docs.iztro.com/en_US/posts/config-n-plugin)：规则开关的上游含义。
- [tzdata 包](https://pypi.org/project/tzdata/)：版本化时区数据。
- [香港天文台 2026 年 2 月历表](https://www.hko.gov.hk/en/gts/astron2026/files/2026cal02.pdf)：
  2026-02-17 为农历正月初一，用于独立核对测试的年界日期；不能据此证明紫微算法正确。

本轮没有完成用户指定排盘软件对齐、独立紫微黄金样例审定、真实 DeepSeek finalization 联调，
也没有做统计意义上的命理预测有效性验证。离线模型的解释明确标为测试输出。

## 4. 当前契约与有意收敛

- `ZiweiAnalysisRequest` 显式包含 `level`；`context_refs` 留在已有外层 handoff，避免重复授权字段。
- Tool 只允许与任务一致的 `focus`；目标从 child state 注入。候选阶段不开放 Tool 内二次修改
  selector，改变目标必须由根发起新委派。这比设计的“合法细化”更收敛。
- `ChartProjection` 承担设计中 bundle 的模型侧职责，完整规范化事实在 `ChartCalculation`。
  每个事实有层级和稳定 ID；事实内容为确定性 JSON 字符串，星曜使用固定位置的紧凑数组。
- 完整事实 Artifact 不保存随 Turn 变化的 `target.request_clock`；该时钟保留在返回的目标 envelope
  和执行上下文。相同盘面跨 Turn 复算不会因为执行时间变化造成幂等内容冲突。
- `charts_used` 保留完整有界投影，确保根 Agent 汇总时仍能看到真实事实，而不只看到不可回读的 ID。
- 出生公历／农历支持范围为 1901–2099；农历闰月必须明确且通过公农历往返校验。
- 七个城市别名可离线解析时区，其他地点要求明确 IANA 时区并附未地理核验警告。
- 时辰不伪造 UTC；钟表时间检查 DST 缺口和重复，重复时刻要求 `fold`；跨时辰区间需澄清。
- 查询支持公历时点、年／月区间、相对日期／年月和有界日期区间；农历查询区间、带时分秒的目标、
  真太阳时和在线地理编码尚未支持，明确拒绝，不静默换成民用时间或代表日。
- 大限支持“某日期／有界区间实际落在哪个大限”，返回虚岁范围及查询覆盖段；
  按第 N 大限定位、返回完整十年绝对起止日期仍待独立规则样例验证，不宣称已完成。

## 5. 预算与失败语义

服务内最多扫描 366 个目标日，逐日层级最多 31 天，最多 32 个分段。这些只是计算上限，
不保证任意这样的区间都能放入最终输出预算。一次日盘 Tool 同时生成所需上层盘面，不要求五次调用。

默认投影预算 14,000 字节，另受 Artifact 内联阈值约束；完整 child 结果还需为父委派 envelope
预留 1,024 字节。最终父 ToolMessage 再次检查实际内联大小。超限失败，不对 JSON 截字符。

模型输入按实际消息、系统指令和 Tool Schema 的 UTF-8 字节作保守 token 上界检查，默认至多
24,000，且不超过已配置输入预算扣除输出预留后的余额。不依赖估算出的中文字数当 token 数。
取证最多 6 次模型轮次，为 finalization 和最多一次格式修复预留 2 次；总计模型 8、业务工具 6。
取证不做模型重试／fallback，finalization 与修复均计入既有持久根任务树预算。

缺资料为 `needs_clarification`；未支持功能为 `unsupported`；权限、引擎异常、伪造引用、
不完整盘面或预算耗尽走失败路径。不将这些状态包装成成功解读。即使引用存在，仍不能证明传统解释成立。

## 6. 验证记录

执行环境：CPython 3.13.15、仓库锁定依赖、本地真实 x-iztro、合成出生资料、离线模型，未调用外部模型。

```bash
uv sync --frozen --extra dev --extra ziwei
uv run --frozen --extra dev --extra ziwei pytest tests/stage7 -q
uv run --frozen --extra dev --extra ziwei pytest -m 'not external' -q
```

2026-09-06 实际结果：Stage 7 专项 **36 passed**；全仓库非 external 回归
**200 passed、7 skipped、2 deselected**。两条 warning 来自既有 lark-channel-sdk 的
datetime／event loop 弃用提示，不是新增紫微错误。Ruff 检查、格式检查和 `git diff --check` 通过。
没有把跳过的外部／服务依赖用例计为通过。

重点用例包括：

- 五个层级和一次工具的上层事实完整性、公农历等价及非法闰月；
- 早晚子时、DST、未知时辰、跨时辰区间、固定 request_clock 与查询时区；
- 公历 2026 全年覆盖按真实农历年界分成两段，不以年中某一天代表全年；
- 同对象确定性、并发隔离、跨 owner 拒绝、Artifact 幂等复算；
- 默认禁用、旧 Profile 不变、根 Tool 白名单、权限与伪造 graph state；
- 无盘声称成功、伪造事实引用、完整 Prompt 和最终交付结果过大；
- 原生根 → child → 按 interrupt ID 恢复根，真正的 graph/checkpoint 和业务数据库，
  只有 Agent Server 传输由测试替身替代；正常解读共享模型预算为 5，缺资料为 2；
- 根树预算耗尽、发布漂移、重复 status 不重复恢复或写最终 Journal、失败审计；
- 既有金融、Workflow/HITL、飞书、Stage 6 Fix 恢复和架构测试。

尚未执行真实 Agent Server HTTP／PostgreSQL 崩溃窗口联调、飞书到紫微的实机闭环、外部模型质量评测。
现有持久化恢复测试不等于验证了所有外部服务故障，也不宣称模型调用 exactly-once。

## 7. 发布与后续门槛

本轮未更改 `.env`、未自动授予 `ziwei:read`、未开启已运行环境、未创建出生档案表、未改变保存 TTL。
新能力仅允许 development/test，须显式选择候选规则、配置不少于 32 字节的私密 HMAC key，
关闭完整 I/O 并隐藏 LangSmith 原始输入输出。BFF 与 Agent Server 需使用一致发布与配置。

阶段判定：Stage 7A 已有可运行候选但规则验收未完成；Stage 7B 复用既有 Fix 并完成新增集成测试；
Stage 7C 已跑通候选闭环但真实模型兼容性仍待验收；Stage 7D 未开放。

继续前最需要确认的仍是：指定排盘对齐来源，以及默认使用民用钟表时间还是真太阳时。
启用方法与保存事实见[紫微 Agent 开发验证手册](../../docs/operations/ziwei-agent.md)。
