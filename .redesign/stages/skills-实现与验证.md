# Skills 实现与验证

日期：2026-09-16。基于 `1fc4ca466b3ae7fd7ee8ca95aad18be161410607` 的本地实现，已部署至本机 Compose，尚未提交 commit。设计入口为 [v1.1 实施方案](skills-运行时接入实施方案.md)，使用和发布步骤见 [运行手册](../../docs/operations/skills.md)。

## 已落地

- `SkillRef/SkillRelease`、固定 UTF-8 包和 manifest 校验、根 `finance_agent@1.8.0` 绑定、API/Worker 共用目录及 wheel 数据。
- 受理快照中的 `/skill` 选择，原生 `load_skill` Command 候选提交及独占批次，`read_skill_resource` 双限额分页。
- 请求副本正文、既有上下文准备复用、逐次模型/摘要/工具授权、HITL 和原生 checkpoint 恢复、按 Turn/scope 重置、收尾保留正文并关闭工具。
- 资源、模型、摘要、Journal 和工件回读的来源约束；普通后台索引/记忆提取不读取受限 assistant 正文。
- Manifest/审计、`skill.preparation` custom 流、现有游标/CAS/飞书卡片、统一有界错误。
- 飞书 `/skills` 原生表单、按当前权限展示技能、提交后创建本次任务、原卡进度及重复点击回执。
- `market-brief`、`cocktail-from-what-i-have`、30 个行情问题三组评测入口、发布校验脚本和 CI 步骤。

显式初始化沿用候选准备中的记忆冻结与容量裁剪，随后正常节点复用同一快照。初始化失败把错误及真实摘要计量写入 checkpoint 后结束，BFF 映射为业务失败；模型没有被调用，初始化完成标志不置位。工具初始化失败则返回错误 Command。SQL 审计、费用和原生 checkpoint 仍不宣称跨库原子事务。

## 自动化证据

`tests/skills` 使用真实工厂、原生节点/reducer/checkpoint、SQLite Journal/Artifact 及通知事务；替换模型和外部飞书传输。原表单实现共 92 项，会话发布修复增加 7 项，缺列修复再增加 7 项，合计 106 项，其中 1 项需要 PostgreSQL；具体运行证据见下文。以下表格给出覆盖边界，场景编号沿用实施方案。

| 场景 | 已执行证据 | 仍需单独验收的边界 |
| --- | --- | --- |
| S01–S05 | 安全 YAML、固定包 hash、链接/路径、源码及 wheel、受理幂等、美元/引用语法、隐式/租户/权限规则 | 大规模包目录与生产发布流程 |
| S06–S08、S21 | 同步/异步原生加载与资源工具、批次拒绝、重复加载、候选失败不提交、显式失败计量、Command 当前 call 审计 | PostgreSQL/原生服务在提交边界的进程故障注入 |
| S09–S11、S25 | 独立 Worker invocation scope 校验；真实 HITL 暂停、磁盘 PersistentDict 重建、同轮恢复、新轮清理、损坏发布拒绝 | 生产 AgentServer 的 checkpoint 后端与多进程恢复 |
| S12–S16 | 多字节完整分页、游标快照、目录省略、真实用户判定、主文唯一、retry/fallback、原有写审批 | 真实供应商对方法指令和工具选择的遵从性 |
| S18–S20 | 原始及结构化工件回读、再归档、撤销激活、Journal 来源、未完成批次拒绝、真实摘要及整体失效 | PostgreSQL/对象存储并发撤权及生产消息链 |
| S22–S23 | 工厂装配下的同步/异步请求、共同预算；原生摘要调用前授权与来源、供应商重试撤权阻断 | 实际模型窗口、外部系统并发与长任务规模 |
| S24、S26 | 原生准备事件、错误/重放、真实通知 CAS/卡片投影、失败不冒充完成、最后调用关闭工具但保留正文 | 真实飞书租户、模型输出质量 |
| 飞书技能表单 | 打开不执行、中文下拉与多行输入、内部 HTTP 回调、原子提交与原卡更新、双击去重、提交异常/超时回滚、过期/撤权/伪造拒绝 | 真实飞书租户的控件渲染和点击、PostgreSQL 多进程并发 |
| S17、P4 质量指标 | 已建立 30 个固定合成问题及关闭/自动/显式三组运行器；默认预览已执行 | 未运行 90 个真实模型任务；不报告质量或成本达标 |

技能表单最终实现的本机正常缓存完整回归：`894 passed, 11 skipped`（199.75 秒，另有飞书 SDK 的两条既有弃用提示），包含全部 92 项 Skills 测试。空 tokenizer 缓存 Skills 专属回归为 `92 passed`（18.60 秒），表单单文件回归为 `26 passed`（8.31 秒）。Ruff、格式（427 个文件）、compileall、密钥扫描与源码/wheel 清单核验通过；运行手册的 5 段 shell 示例通过 `bash -n`，5 份相关文档的本地链接均可解析。

先前运行时实现阶段，空缓存完整回归的一次独立运行为 `858 passed, 11 skipped, 1 failed`，失败项及未修改基线复现见下节；该运行早于相邻范围合并及调酒用例。本次没有重跑空缓存的全量套件，也不把 Skills 专属通过表述为该已知问题已修复。

验证命令：

```bash
.venv/bin/python -m pytest -q --disable-warnings --tb=short
TIKTOKEN_CACHE_DIR='' .venv/bin/python -m pytest tests/skills -q
.venv/bin/ruff check financeclaw tests scripts deploy experiments/stage10 experiments/taibu
.venv/bin/ruff format --check financeclaw tests scripts deploy experiments/stage10 experiments/taibu
.venv/bin/python -m compileall -q financeclaw scripts tests/skills
.venv/bin/python scripts/check_secret_leaks.py
.venv/bin/python scripts/skill_manifest.py --check
```

wheel 使用当前本地 setuptools 构建，再由 `scripts/check_skill_wheel.py` 在干净解包目录验证。依赖锁中仅将既有 `pyyaml==6.0.3` 加入项目直接依赖，没有重解析升级其他依赖。本机未提供 uv 命令，未宣称执行过 `uv sync --frozen`；CI 的 uv 流程仍需远端执行。

## 调酒技能集成

按用户指定仓库接入 `cocktail-from-what-i-have@1.0.0`，固定上游提交 `5a0326ce34b44c015fc26b5c28f6118092c806e4`。`SKILL.md` 原文和 MIT `LICENSE` 的 Git blob 均与上游一致，`SOURCE.json` 记录可核验来源；附加中文展示信息与隐式调用策略，全部随 wheel 和包 hash 发布。运行期不下载或执行上游内容。

逐技能声明 `SkillRelease`，调酒包无需行情工具或 `market:read`；未知包缺少已审阅策略时阻止启动。根发布升至 `1.8.0` 并允许使用已发布技能完成日常任务。为保留原文，在根 Profile 中将单正文上限设为 4096，覆盖无分词缓存时的 3086 字节保守估计；激活投影合计和模型共同输入上限保持原值。

新增 8 项自动化用例覆盖来源/许可、依赖隔离、未审阅包拒绝、显式/模型选择及正常/空缓存组合、HTTP/飞书同一命令的受理与重放。图测试记录的是替身模型实际收到的完整正文，不评判真实模型的调酒配方质量；渠道测试使用合成用户和模拟传输，不发送飞书消息。

飞书与 curl 示例、生效开关、新版本会话条件均写入 [运行手册](../../docs/operations/skills.md)。集成阶段未部署服务；后续故障修复的部署与验证见下节，环境凭据未修改。

## 飞书技能任务表单

`/skills` 在既有待答路由之前处理，只创建会话绑定的通知草稿。技能下拉框展示固定发布中的“行情简报”和“现有材料调酒”，按当前用户权限过滤；禁止隐式选择的技能仍可供用户显式选择。任务描述使用 JSON 2.0 `multiline_text`，前后端均限制为 1000 字，按钮只通过 `form_action_type=submit` 提交整个表单。

选项冻结 SkillRef、发布策略指纹和展示名称；提交时重新核对受信任渠道身份、原单聊、卡片送达回执、30 分钟有效期、当前 Profile 与技能策略。先锁会话，再锁表单目标，沿用 Turn admission 的活动任务约束。admission 支持加入既有同库事务，因此新 Turn、Journal、初始命令、原卡片关联与幂等回执一起提交；受理错误或提交前检测到耗时超过 1.8 秒均整笔回滚。事务提交后才唤醒原有生命周期，不在回调内调用模型或原生服务。

应用库仍为 18 张表，只将 `notification_targets.turn_id` 改为可空以容纳未提交草稿。提交后关联新任务，继续使用原 CardKit 实例、确认序号及持久通知投递，展示“已受理 · 技能名称”和任务编号。选择不写入用户默认设置，新 Turn 重新选择。旧 `/skill` 文本和 HTTP API 契约保持可用。唯一初始迁移及通知 readiness 已同步，已有库不会自动变更。

新增 26 项表单用例覆盖持久打开/重放不创建任务、服务对象重建后并发双击只受理一次、原卡更新、精确技能绑定、提交后才派发、回调归属和非法字段、输入边界、过期/撤权/发布变化、异常及超时回滚、投递未知、待答任务不被误回答、空目录，以及认证后的内部 HTTP 入口。测试复用 SQLite 事务、正式 admission/通知仓库/发送器，模型和飞书 SDK 传输仍使用替身；没有向真实飞书发送表单，也没有验证真实模型回答质量。

## 飞书旧会话故障修复与本机验证

真实失败会话仍绑定 `finance_agent@1.6.0`，镜像仅发布 `1.8.0`，打开 `/skills` 时按旧版本查目录触发 `LookupError`。空闲单聊现在持有与 Turn admission 相同的会话写锁，确认无活动任务后向前更新发布并分配新原生线程；保留会话 ID、Journal、旧 Turn 快照与通知目标。活动旧任务不切换发布，旧进程不得降级会话；不可用时返回明确提示。

数据库回放又确认旧 `notification_targets.turn_id` 仍为非空，草稿插入会触发 `NotNullViolation`。已在本机应用库中执行 `DROP NOT NULL`，保留全部记录、外键与唯一约束；该次操作只补齐已确认的字段差异，不重建数据库。启用飞书时，正式 API 就绪端点现在调用已有通知 schema 校验，旧结构返回 503。

`tests/skills/test_feishu_release_refresh.py` 新增 7 项，覆盖历史保留与新线程执行、旧任务停止后更新、并发只更新一次、防降级、身份隔离、旧表单失效，以及真实旧列结构下就绪失败/修正后恢复。完整回归在前 6 项加入后为 `900 passed, 11 skipped`（307.85 秒）；随后第 7 项和就绪检查加入后，该文件为 `7 passed`（4.91 秒）。工件 `path/fields` 描述补充后，酒店读取、工件视图和上下文容量共 `24 passed`（8.62 秒）。最终空 tokenizer 缓存的 Skills 全套为 `99 passed`（33.26 秒），Ruff 与格式检查（428 个文件）、compileall 通过。

部署后以原失败消息对应的真实 PostgreSQL 会话运行正式渠道/仓库/表单路径，外层事务最终回滚，回复传输为本地替身。结果为 `skill_form`，事务内发布由 `1.6.0` 更新至 `1.8.0` 并换新线程，卡片标题为“FinanceClaw · 新建技能任务”，选项包含“现有材料调酒”和“行情简报”。任务与 Journal 增量均为 0，未调用原生任务或模型，未发送飞书消息；回滚后旧会话版本及通知目标数量保持原值。用户下一次真实消息会按新代码更新空闲会话。

最终镜像再次部署并重复该回滚验证成功。API、Worker、integrations 中 5 个相关修复文件的 SHA-256 均与工作区一致，Compose 的 6 个运行服务全部健康，迁移进程退出码为 0，`/v1/health/ready` 返回 `{"ready": true}`。

## 技能表单提交缺列修复

用户在 2026-09-16 21:27:25 提交表单时，API 记录 `ProgrammingError`；同一时刻 PostgreSQL 明确报告 `conversation_messages.skill_access_refs` 不存在。对真实应用库的全部 ORM 字段进行比对，又发现 `model_context_manifests` 缺少全部 5 个 Skills 字段。此前的打开表单探针不写 Journal 或模型清单，不能证明提交和后续结果持久化可用。

新增 [已有开发库字段补齐 SQL](../../deploy/postgres/skills-runtime.sql)，在事务中补齐 6 个字段和表单可空约束。历史来源回填空数组，遗漏数量回填 0；随后移除临时数据库默认值，与初始迁移保持一致，保留既有数据和约束。已在本机实际应用库执行，全量 Alembic/ORM 结构比对无差异。API/Worker 启动与 API 就绪检查现在检查会话、消息和模型清单的全部列；记忆 Worker 的启动检查也包含它读取的消息表。

新增 6 项缺列场景分别验证启动校验拒绝和就绪返回 503。另在独占 PostgreSQL schema 中执行与现场相同的 SQL，验证旧行保留、字段回填、完整结构一致、新技能来源读写，以及重复执行不覆盖数据。该文件真实 PostgreSQL 运行 `7 passed`（8.04 秒）；Skills 全套与初始迁移、记忆进程回归合计 `107 passed, 1 skipped`（30.72 秒），其中跳过的 PostgreSQL 场景已通过前述独立运行。Ruff、格式（429 个文件）、compileall、密钥扫描与 diff 检查通过。

使用真实已送达表单的 event、原单聊和卡片回执，构造明确的合成任务输入，经过正式 `FeishuCardActions.accept` 提交路径进行 PostgreSQL 回滚验证。首次提交、同 event 重放和新 event 再次点击均返回同一受理结果，只创建一个 Turn、一个命令和一条用户消息；技能 Manifest 与模拟回答来源成功写入。外层事务回滚后，任务/命令/消息/清单/通知事件数量与原值一致，原表单仍未提交。没有唤醒原生执行、调用模型或发送飞书消息。这是数据库提交路径验证，不是模型产出质量或实际点击体验验收。

## 已发现的基线问题

空 tokenizer 缓存的完整回归中，酒店结构化结果测试 `tests/mcp_integration/test_hotel_result_views.py::test_large_hotel_results_complete_within_existing_budget` 超出 65536 的共同输入上限。通过 `git archive HEAD` 在临时目录独立复现未修改提交，同样失败（73595 > 65536）；Skills 版本为 74845 > 65536。二者均在最终检查处停止，没有发送超限模型请求。

该酒店测试的共同输入上限与测试数据未调整，也没有新增跳过条件。当前空缓存下的 Skills 专属 92 项全部通过；已有酒店测试仍需单独处理。新增目录/工具 Schema 带来的输入增长已纳入同一计数器。

2026-09-17 提交前复核：最新正常缓存完整回归为 `916 passed, 12 skipped`；单独以
`TIKTOKEN_CACHE_DIR=''` 重跑上述酒店用例，仍在最终检查处拒绝 `76540 > 65536` 的输入。
该已知空缓存问题尚未修复，不能把正常缓存通过表述为 CI 等效套件全部通过。
本次 Ruff、格式、技能 manifest、wheel 技能资源核验及暂存内容密钥扫描均通过。

## 未执行的环境验证

先前运行时实现阶段 Docker 不可连接；后续故障修复已完成本机服务部署、应用库列约束修正及 PostgreSQL 回滚验证。仍未执行真实飞书租户中的表单投递/点击、真实模型任务或多进程故障注入。P4 保持未完成，不能把离线替身、回滚探针或静态检查当成真实模型/渠道验收。
