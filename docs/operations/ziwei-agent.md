# 紫微 Agent：开发验证手册

当前是默认关闭的候选功能，只能在 development/test 使用合成资料验证；不是正式排盘服务。
实施与未完成项见 [Stage 7 验证记录](../../.redesign/stages/Stage-7-实施与验证.md)。

## 安装与验证

```bash
uv sync --frozen --extra dev --extra ziwei
uv run --frozen --extra dev --extra ziwei pytest tests/stage7 -q
```

`ziwei` extra 固定 x-iztro 0.4.0 和 tzdata 2026.3。没有安装此 extra 时普通金融链路仍可启动，
引擎集成测试会跳过；必须确认 Stage 7 测试实际执行后再记录排盘验证通过。
若使用项目内 conda 环境，按根 README 设置 `UV_PROJECT_ENVIRONMENT`，不要混用两个解释器。

## 显式候选配置

仅在隔离的开发验证环境设置以下项，BFF 与 Agent Server 两边一致：

```dotenv
FINANCECLAW_ENVIRONMENT=development
FINANCECLAW_ZIWEI_ENABLED=true
FINANCECLAW_ZIWEI_CONVENTION=x-iztro-civil-candidate@1.0.0
FINANCECLAW_ZIWEI_KEY_VERSION=1
FINANCECLAW_ZIWEI_PROJECTION_BYTES=14000
FINANCECLAW_DEBUG_FULL_IO=false
FINANCECLAW_LANGSMITH_HIDE_INPUTS=true
FINANCECLAW_LANGSMITH_HIDE_OUTPUTS=true
```

另从 Secret Manager／未跟踪的本地秘密配置注入 `FINANCECLAW_ZIWEI_HMAC_KEY`，至少 32 字节；
不要复制测试 fixture 密钥，不要把密钥写入仓库。BFF 和 Agent Server 需使用同一把密钥和版本。
密钥轮换必须增加 key version，并保留旧配置供旧运行完成或先排空；不能在相同版本下静默换 key。

候选配置未明确、依赖不匹配、未显式放行的原文调试或环境为 staging/production 时启动会被拒绝。
设置隐藏 trace 不等于模型提供方不接收模型输入；真实资料接入仍需单独确认模型数据处理与用户告知。

开发/测试联调需要查看完整输入输出时，可在 BFF 与 Agent Server 两份环境文件中显式配置：

```dotenv
FINANCECLAW_ZIWEI_ALLOW_FULL_IO=true
FINANCECLAW_DEBUG_FULL_IO=true
FINANCECLAW_LANGSMITH_HIDE_INPUTS=false
FINANCECLAW_LANGSMITH_HIDE_OUTPUTS=false
LANGSMITH_HIDE_INPUTS=false
LANGSMITH_HIDE_OUTPUTS=false
```

`ZIWEI_ALLOW_FULL_IO` 默认 false，只放行这项启动校验，不自动修改日志、隐藏或追踪开关。
完整 I/O 可能包含出生资料；启用 LangSmith tracing 时完整输入输出会发送到 LangSmith。
该开关仅允许 development/test；规则、密钥、权限和其他校验仍然有效。
配置变更后重启两侧服务；Docker Agent Server 需重新构建以包含支持该开关的代码。

本轮没有修改当前运行环境。基础启动方式见[根 README](../../README.md#运行)，
请使用隔离开发环境，不要直接开启真实出生资料测试。

## 入口、权限与发布

产品入口仍是创建 Conversation 和提交 message-only Turn；不增加直连排盘 REST API。
可信登录身份需明确获得 `ziwei:read`；读取 Artifact 另外需要 `artifacts:read`。
按既有机制配置开发 BFF scopes 或飞书白名单身份 scopes，保留原有合法权限，不使用 `*` 兜底。
本轮没有自动扩大任一用户的权限。

新建会话统一绑定根 `finance_agent@1.4.0`。候选启用时增加紫微委派工具；
关闭时仍可执行普通金融请求，工具白名单不包含紫微委派。
`ziwei_doushu_agent_v2_0_0` 与 `finance_agent_v1_4_0` 已登记在 `langgraph.json`；
若使用独立的本地 graph 配置，也必须同步这两个映射，否则 BFF 会成功创建 thread、但提交
run 时收到 422，thread 将保持 idle。旧紫微 `1.0.0` 以及金融根 `1.2.0/1.3.0` 已移除。
Agent Server 仍必须是受保护的内部执行平面。
旧版本会话不能继续提交新任务，应新建会话；已有运行的冻结配置不自动迁移。

已有 `0007_stage6fix_ab`、`0008_stage6fix_c` 提供快照、预算和可靠交付。本功能没有新增 migration；
升级旧部署仍要先按既有流程执行 `alembic upgrade head`。

## 请求示例

合成测试消息：

> /agent ziwei_doushu_agent 请只看一个合成样例的 2026 年 9 月 6 日日盘：女性，
> 公历 2000 年 8 月 16 日，上海当地民用钟表时间 03:30。不要生成命理解读。

根 Agent 整理出的领域参数示例（不是新增 HTTP 请求体）：

```json
{
  "question": "只看合成样例的日盘",
  "subject_label": "合成样例",
  "mode": "chart_only",
  "birth": {
    "calendar": "solar",
    "date": {"year": 2000, "month": 8, "day": 16},
    "time": {"kind": "clock", "clock": "03:30"},
    "time_basis": "civil",
    "place": {"name": "上海"},
    "sex_for_chart": "female"
  },
  "level": "daily",
  "focus": "overall",
  "target": {"kind": "point", "on_date": "2026-09-06"}
}
```

五种层级为 `natal/decadal/yearly/monthly/daily`。本命不传 target；其余必须指定目标。
公历整年使用 `{"kind":"calendar_period","unit":"year","year":2026}`，相对今年使用
`{"kind":"relative_period","unit":"year","offset":0}`。相对时间取可信 Turn 时钟和查询时区，
不是子任务执行日期。`bounded_range` 的 `end` 不含当天。

农历出生需 `calendar=lunar` 和明确 `is_leap_month`。只知道时辰时使用 `time.kind=shichen`，
例如 `shichen=yin`；“子时”仍需区分 `zi_early/zi_late`。不要补造 12:00 或猜测性别。

默认 `OfflineFinanceModel` 不是通用自然语言解析器，不能用它来验收上述任意消息的自动路由。
`tests/stage7/test_conversation.py` 使用明确的根模型测试替身，真正运行父子 graph 和恢复流程；
`OfflineZiweiModel` 只用于闭环测试，生成带实际引用的测试文本，不代表真实解读质量。
真实模型自动提取参数仍待单独联调；当前文本解读不使用 JSON mode。

## 当前限制与故障判断

- 仅民用时间；真太阳时明确返回 unsupported。
- 地名可离线识别北京、上海、广州、深圳、成都、香港、台北；其他地点要补 IANA 时区。
- 出生时间范围跨时辰、夏令时缺口／重复、资料缺失时先澄清，不生成猜测盘。
- 查询区间目前只有公历日／年月／日期范围，农历目标区间未实现。
- 工具一次返回目标层级及上层依据，不必按五种盘顺序调用。
- 大限工具用于日期定位，不支持按第 N 大限索取完整十年日历区间。
- 单次最多 366 天、逐日最多 31 天、分段最多 32；仍可能因事实体积超限而拒绝。
- `ZIWEI_CONTEXT_BUDGET_EXCEEDED` 应缩小时间或主题，不通过提高摘要截断阈值隐藏问题。
- 结果的 outcome 与委派传输 completed 不同；needs_clarification 应由根提问，用户回答后新委派。
- 规则仍待独立核验；解释只作传统文化参考，不能作为医疗、投资或其他重大决定依据。

## 隐私与回滚

出生资料不进入新增长期档案或紫微 Memory；但原始会话、委派、checkpoint 和完整事实 Artifact
仍会按项目现有策略保存。本实现不是零留存，也没有自动删除或改变 TTL。
本地排盘不调用外部命理服务／地理编码，但根和子模型可能接收相关资料。

根和子任务按 confidential 分类处理；默认不开完整 I/O，trace 配置在 Agent Server 装配前生效。
不要通过共享 Tool 实例保存“当前用户命盘”，也不要把 Artifact ID 当成跨用户读取授权。

停止候选需先排空运行，随后两侧关闭 `FINANCECLAW_ZIWEI_ENABLED`。
关闭后普通金融请求继续使用根 1.4.0，紫微委派不再可见。开关变化会改变发布配置指纹，
不能用新配置恢复旧的在途任务；须先排空任务并同步重启 BFF 与 Agent Server。
