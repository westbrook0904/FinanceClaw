# Stage 7：文本解读热修复实施与验证

日期：2026-09-07

状态：用户已确认实施与推送；候选环境限制继续有效，真实模型质量与生产部署不因代码推送自动验收。

## 1. 已确认的修复范围

新发布的紫微子 Agent 保持“预检 → 受治理工具取证 → 一次解读”流程，但模型只生成自由文本／
Markdown，不再要求生成 `answer_summary`、`interpretations[]`、`topic` 和 `evidence_refs[]`。

移除新解读路径的 `response_format=json_object`、`InterpretationDraft.model_validate_json()`、
段落字段／长度约束、逐条引用成员校验，以及一次格式修复调用。建议 500 字只保留为提示，
不是正文逐字段硬校验；整体输入／交付字节预算仍保持原样。

不调整 temperature、thinking、模型供应商、工具权限、五种排盘工具、计算规则、隐私策略或默认启用开关。
正文不被解析为业务指令，即便模型输出 JSON 样式文字，也不能覆盖代码生成的 outcome、命盘或版本。

## 2. 程序生成的结果外壳

新结果为 `ZiweiTextResult`，`schema_version=2`，继续写入 `ziwei_result` state 字段。
外层既有 `DelegationResult@1` 和 typed handoff V2 不变，不要混淆领域结果版本与委派协议版本。

| 字段 | 来源与语义 |
|---|---|
| `outcome` | 程序判断 answer／chart_only／needs_clarification／unsupported |
| `answer_text` | 模型可见正文，保留内容，不解析其 JSON、不校验逐条论断 |
| `charts_used` | 程序从本次实际工具计算结果装配；只表示实际盘面来源，不声称证明每句话 |
| `question`、`subject_label` | 冻结的请求 |
| `warnings`、`missing_fields`、`error_code` | 已有预检与计算业务结果 |

保留出生输入、对象／规则／时间匹配、真实命盘存在性、深层盘面数据契约、权限、发布快照、
数据分级、根树预算和整体字节上限。澄清／不支持不能伪装为成功解读，没有命盘仍不能返回 answer。

模型响应仅作必要的交付检查：

- 必须有非空可见正文；结构化文本块仅提取 text，不展示 reasoning 内容。
- 已标记 `length`、`content_filter`、`insufficient_system_resource` 或仍有工具调用的响应，
  不得当作完整解读；明确报错，不通过格式修复重试掩盖。
- 保留 API 异常和模型／交付预算错误，不能把空结果、截断文本或错误字符串包装为成功。
- 只校验已取得的格式／终止元数据；不声称能从自然语言自动证明完整性或预测正确性。

取证最多仍为 6 次模型轮次；新发布最多 6＋1＝7 次，不因删除修复空间而扩大取证预算。
正常单次工具路径仍为子模型 3 次、根与子总计 5 次；删除的是额外格式修复，不是所有最终解读调用。

## 3. 为什么委派层仍然校验一次

图节点内校验的是“生产结果时的条件”，委派层校验的是“跨进程／远程运行／持久化后的接收契约”。
即使子图内通过，远程返回仍可能缺少约定 state 字段、属于不兼容发布、数据损坏或错误终态。

`DelegationService` 继续按固定 Profile 从 `ziwei_result` 提取 V2 外壳，并校验其业务状态和盘面结构。
它不解释 `answer_text`，不检查文风、预测正确性、段落结构或逐句引用，也不会再次调用 LLM 修复。
恢复父 Agent 时既有委派 ID、目标版本、输入摘要与运行归属保护保持不变。

测试包含“子图完成后，远程返回的领域 schema_version 被改成 99”：接收端必须交付 failed，
不能因为子图之前执行成功就把损坏结果交给父模型作为成功事实。

## 4. 发布绑定

| 组合 | 根发布 | 紫微发布／服务端 graph | 结果协议 |
|---|---|---|---|
| 当前发布 | `finance_agent@1.4.0` | `ziwei_doushu_agent@2.0.0`／`ziwei_doushu_agent_v2_0_0` | V2 文本解读 |

`langgraph.json` 只注册当前根与当前紫微图。旧 `finance_agent@1.2.0/1.3.0`、
`ziwei_doushu_agent@1.0.0`、V1 Schema 及 JSON 兼容分支已在本地数据库重建后移除。
测试覆盖当前发布的真实根子图闭环。

新会话统一选择根 1.4.0；紫微关闭时保留普通金融能力，开启时增加紫微委派工具。
本次不新增 `finance_agent` 版本。如果部署中仍存在绑定 1.2.0/1.3.0 的会话，
必须先排空任务并新建会话再使用当前发布；不能让旧检查点
静默改用新协议。

部署时：

1. BFF 与 Agent Server 同步部署，确认当前根／子 graph 均可用。
2. 用新会话验证 V2。需要让既有飞书绑定使用当前版本时，先排空或明确取消旧活动任务，再单独授权
   执行显式会话迁移；不能在每个 Turn 自动切换。
3. 若部署使用自定义 graph 配置（例如 `langgraph.local.json`），需同步新增
   `finance_agent_v1_4_0` → `server_graphs.py:finance_agent` 与
   `ziwei_doushu_agent_v2_0_0` → `server_graphs.py:ziwei_doushu_agent_text`，并移除旧映射。
4. 未将自定义本地 Compose、环境变量或飞书身份配置夹带入本次提交。此热修复不包含 Stage 8 后台推进。

## 5. 验证

验证使用合成出生资料、真实本地 x-iztro、原生 LangGraph/checkpoint、测试数据库及离线模型；
不读取真实生日、不调用付费线上 LLM，也不向真实飞书发送测试消息。

新增／调整的回归覆盖：

- 普通文字、Markdown、超过旧字段长度的正文、无引用正文及不完整 JSON 样式文字均可交付。
- 不启用 JSON mode、不调温；解读只调用一次，无格式修复。
- 空文本、reasoning-only、生成截断、平台中断和意外工具调用不冒充完整答案。
- 无真实盘面、权限／发布漂移、完整结果超限及持久根预算耗尽仍失败。
- 当前根／child 的真实图委派与恢复、损坏结果的委派边界拒绝。
- 旧根／child Profile 不再可解析，避免旧映射被误注册。

实际执行：

```bash
.venv/bin/pytest tests/stage7 tests/stage1/test_models_settings_architecture.py -q
.venv/bin/pytest -m 'not external' --ignore=tests/stage6/test_feishu_lifecycle.py -q
.venv/bin/ruff check financeclaw tests
git diff --check
```

结果：紫微专项与模型／架构测试 **70 passed**；本提交范围的全仓非 external 回归
**230 passed、7 skipped、2 deselected**。Ruff 检查、11 个变更 Python 文件的格式检查、
文档本地链接检查及差异格式检查通过。

另运行了包含工作区原有、未跟踪飞书生命周期文件的全量测试：**234 passed、7 skipped、
2 deselected**；该文件不属于本次提交，因此上面的可复现命令显式排除它。
两条 warning 来自既有 lark-channel-sdk 的 datetime／event loop 弃用提示。

真实模型的解读质量、成功率和线上延迟仍需独立评测；未进行真实 Agent Server HTTP 或飞书
部署联调，不能从离线通过推断已达到某个线上成功率。
