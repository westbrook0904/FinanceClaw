# Skills：使用、发布与排错

Skill 是随项目发布的方法说明包：模型先看目录，选择后加载正文，需要时再读取包内参考资料。
它帮助模型按约定步骤完成任务，不增加工具、交易、记忆或审批权限，也不执行包内脚本。

当前根发布为 `finance_agent@1.8.0`，`FINANCECLAW_SKILLS_ENABLED` 默认 true，包含以下固定内置技能。
首次接入先完成[本地完整链路](local-full-stack.md)和[模型配置](model-configuration.md)。

| 技能 | 用途 | 业务依赖 |
| --- | --- | --- |
| `market-brief@1.0.0` | 整理行情简报，保留来源和日期 | `market_snapshot@1.0.0`、`market:read`；当前工具仍返回演示行情 |
| `cocktail-from-what-i-have@1.0.0` | 按已有酒、饮料及器具提供配方、替代方案、多人份和无酒精版本 | 无外部 API、MCP、脚本或行情权限要求 |

## 1. 先运行一个调酒任务

### 飞书：先选技能，再提交任务

在已配置机器人的**单聊**中单独发送 `/skills`，机器人回复“FinanceClaw · 新建技能任务”表单：

1. 在“选择技能”下拉框中选择当前可用的技能。当前发布显示“行情简报”和“现有材料调酒”，列表按会话发布和当前权限过滤。
2. 填写“任务描述”，支持多行，最多 1000 字。例如选择“现有材料调酒”，填写“我有金酒、柠檬、苏打水、蜂蜜和冰块，没有摇壶，做两杯清爽的。请用中文和毫升给出配方、步骤、替代方案，并附一个无酒精版本。”
3. 点击“开始执行”。提交成功后显示“已受理 · 现有材料调酒”和任务编号，原表单卡转为任务进度卡。

打开表单时，服务端只保存可选技能与原卡片关联。在控件中选择技能、填写描述不会创建 Turn、调用模型或加载技能。点击提交时才校验填写内容，重新检查用户、会话、权限和冻结的技能版本，在同一事务中创建任务并绑定原卡片。随后由现有任务执行流程在第一次回答模型调用前完整加载所选技能，展示准备状态、执行进度和最终结果。

选择只对本次任务生效。重复点击或重复回调复用同一任务编号，不能把已提交表单改成另一个任务；新任务重新发送 `/skills`。未提交表单有效期为 30 分钟，过期或技能发布变化时提示重新打开。上一条任务仍在处理中时可以打开表单，但提交会提示先完成或停止原任务；等待回答时发送 `/skills` 也不会被当作原问题的答案。

表单复用现有 `card.action.trigger` 和 CardKit 投递，无需为 Skills 新增事件订阅。
列表只展示已发布且当前可用的技能；目录中没有的能力需要先完成包发布与授权。

### 飞书：直接命令

仍可在单聊中直接发送 `/skill <技能 ID> <任务描述>`，用于快速提交或超过表单字数上限的任务：

```text
/skill cocktail-from-what-i-have 我有金酒、柠檬、苏打水、蜂蜜和冰块，没有摇壶，做两杯清爽的。请用中文和毫升给出配方、步骤、替代方案，并附一个无酒精版本。
```

卡片先显示技能准备状态，再显示回答或补充问题；若询问材料、器具或人数，在原单聊直接回复即可恢复同一任务。新任务需要确定使用它时，再次选表单或加上 `/skill cocktail-from-what-i-have`。

也可以直接说“我有金酒、柠檬和苏打水，没有摇壶，能做什么清爽的鸡尾酒？”，由真实模型根据目录选择技能；自然语言触发并不保证每次加载。明确指定时使用表单或命令。当前渠道只支持 P2P 单聊，不通过群聊中的 `@` 触发。

### 调酒技能：HTTP API

使用普通 Conversation/Turn 入口，无需专门的 Skills 路由。下面示例在仓库终端执行，需要 `curl` 和 `jq`；令牌是产品 API 的 Bearer Token，本地开发对应 `FINANCECLAW_API_AUTH_TOKEN`，生产使用 OIDC 访问令牌。

```bash
FC_BASE_URL='http://127.0.0.1:8000'
FC_API_TOKEN='替换为你的产品 API 令牌'

# 创建绑定当前根发布的新会话。
FC_CONVERSATION_ID=$(curl --fail-with-body -sS -X POST "$FC_BASE_URL/v1/conversations" \
  -H "Authorization: Bearer $FC_API_TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{}' | jq -er '.conversation_id')

# 新任务生成一个新 key；同一请求重试时保留原 key 和原正文。
FC_REQUEST_KEY=$(.venv/bin/python -c 'from uuid import uuid4; print(uuid4())')
FC_TURN_ID=$(curl --fail-with-body -sS -X POST \
  "$FC_BASE_URL/v1/conversations/$FC_CONVERSATION_ID/turns" \
  -H "Authorization: Bearer $FC_API_TOKEN" \
  -H "Idempotency-Key: $FC_REQUEST_KEY" \
  -H 'Content-Type: application/json' \
  -d '{"message":"/skill cocktail-from-what-i-have 我有金酒、柠檬、苏打水、蜂蜜和冰块，没有摇壶，做两杯清爽的。请用中文和毫升给出配方、步骤、替代方案，并附一个无酒精版本。"}' \
  | jq -er '.turn_id')

# 查询状态；202 仅表示已受理，completed 才表示任务完成。
curl --fail-with-body -sS \
  "$FC_BASE_URL/v1/conversations/$FC_CONVERSATION_ID/turns/$FC_TURN_ID" \
  -H "Authorization: Bearer $FC_API_TOKEN" | jq

# 等待完成后读取最终正文，也可查询该会话的 /messages 持久化记录。
curl --fail-with-body -sS \
  "$FC_BASE_URL/v1/conversations/$FC_CONVERSATION_ID/turns/$FC_TURN_ID" \
  -H "Authorization: Bearer $FC_API_TOKEN" | jq -r '.output.messages[]?.content'
```

在状态 URL 后加 `/events`，以 `curl -N` 读取 SSE `turn.snapshot`；遇到 `waiting` 时按 `pending_interactions` 返回的交互要求回答，流程见 [Turn 运行手册](turn-control.md)。请求体只包含 `message`，客户端不能指定 package hash、原生 thread 或 checkpoint。

## 2. 部署与已有开发库

配置 `FINANCECLAW_SKILLS_ENABLED=true`、`FINANCECLAW_OFFLINE_MODEL=false`，并按 [模型配置](model-configuration.md) 填写供应商和密钥。飞书还需要 `FINANCECLAW_FEISHU_ENABLED=true`、应用凭据、用户白名单及现有事件/CardKit 配置，见 [本地完整链路](local-full-stack.md)。Skills 不需要额外飞书事件订阅。

包随镜像发布；修改源码后需要构建并启动同一版本的 API、Worker 和 integrations。环境与当前 schema 准备好后，使用现有部署入口：

```bash
.venv/bin/python scripts/deploy.py
curl --fail-with-body -sS http://127.0.0.1:8000/v1/health/ready
```

使用当前 `finance_agent@1.8.0` 发布。飞书单聊仍绑定旧版本时，下一条消息会先检查是否存在未完成任务：空闲会话自动向前更新版本，并为后续任务分配新的原生线程，原会话 ID、Journal、旧任务快照和通知目标保留；活动任务不会被迁移。若旧版本已下线且仍有未完成任务，`/skills` 会提示先停止旧任务，确认停止后重新发送即可。滚动部署中的旧进程不能把会话降级，旧表单仍需按当前发布重新打开。

旧发布的 checkpoint 不会交给新版本恢复；HTTP API 可直接新建 Conversation。当前没有 `/new` 控制命令，初始化数据库仍遵循项目的空库迁移要求。

表单草稿复用 `notification_targets`，其 `turn_id` 在提交前允许为空，提交后指向新任务。
Skills 还为 `conversation_messages` 新增 `skill_access_refs`，为 `model_context_manifests`
新增 `skill_catalog_hash`、`skill_catalog_omitted`、`skill_refs`、`skill_resource_refs` 和
`skill_access_refs`。唯一初始迁移 `0001_initial` 已包含全部变化，但已初始化的库不会重跑该迁移。

已具备本版其他 Stage 11 结构的开发库，先运行经过验证的字段补齐脚本，再部署同版服务：

```bash
docker compose exec -T postgres psql -U financeclaw -d financeclaw_app \
  -v ON_ERROR_STOP=1 < deploy/postgres/skills-runtime.sql
```

脚本在单一事务中新增缺列，旧消息和清单的来源字段回填空数组、遗漏数量回填 0，保留已有行、
外键和唯一约束；重复执行不会覆盖已记录的技能来源。这不是通用的旧库升级脚本。空库继续使用
初始迁移。API/Worker 启动及 API 就绪检查会核对消息和清单字段，记忆 Worker 也检查消息表；
缺列或旧表单非空约束不能视为就绪。部署脚本不会自动清库或执行该补齐脚本。

## 3. 一次任务怎样加载技能

显式选择在受理事务中写入 `requested_skills`，在首个回答模型请求前完成候选准备。普通任务可由模型通过 `load_skill` 主动选择；`$AAPL`、`$100`、`$market-brief` 均为普通文字。只有真实新 Turn 开头的 `/skill` 是控制语法，引用、代码块和 Worker 的任务描述不能形成根显式选择。

`load_skill` 必须独占工具批次；加载和业务工具混合提交会整批拒绝。`read_skill_resource` 读取已激活包内的 UTF-8 文本页，按 `next_cursor` 连续读取。二者不能执行脚本、安装 MCP、下载新技能或访问宿主任意文件。

同一 Turn 的原生恢复保留固定绑定，新 Turn 或其他 Worker 调用重新选择。正文以带来源的合成消息加入请求副本，不保存在 checkpoint 的 messages、Journal 或长期记忆中。默认每个 scope 最多激活 2 个技能；当前根发布的单份正文检查上限为 4096 tokens，全部激活投影（含来源包装）合计仍不超过 4096 tokens。正文保留完整原文，容量不足会拒绝新增激活。

模型请求中的顺序为：系统规则/记忆/技能目录 → 历史摘要 → 保留的历史轮次 →
当前激活技能正文 → 本轮原始用户输入 → 本轮工具往返和补充消息。
技能正文在预算检查与最终请求中共用同一插入逻辑，以本轮原始用户消息 ID 为边界；
工具加载或澄清恢复后仍保持这一边界，不拼接、改写用户输入，不把正文加入历史摘要。

显式准备卡片显示“正在准备技能 / 技能准备就绪 / 技能准备失败”。模型发起的真实工具调用继续使用工具进度。准备就绪仅表示候选准备成功，最终任务是否完成由 checkpoint 和业务 Turn 判定。

`FINANCECLAW_SKILLS_ENABLED=false` 关闭该部署的技能发布及两个工具。API 与 Worker 必须使用同一配置和镜像；关闭或修改发布后不能继续使用旧版本 checkpoint。确定性 `OfflineFinanceModel` 用于协议测试，不能代替真实模型的自动技能选择验收。

## 4. 新增或更新内置包

包目录为 `financeclaw/shared/skills/builtin/<skill-id>/`，入口为 `SKILL.md`，
固定版本/hash 清单为 [builtin/index.json](../../financeclaw/shared/skills/builtin/index.json)。
主文 frontmatter 必须包含 `name` 和 `description`。

新增包的顺序是：准备正文与资源 → 在 `shared/releases/skills.py` 声明平台策略 →
生成并审阅清单 → 检查 wheel 包含全部文件 → 与 API/Worker 同版部署。
更新已有正文或资源时先更新清单中的发布版本，再重新生成 hash；仅修改文件但保留旧 hash 会导致启动拒绝。
运行时不会从 GitHub 拉取文件，也不会自动安装未知目录。

可选 `agents/openai.yaml` 支持 `policy.allow_implicit_invocation`。设为 false 会关闭模型自动选择，但保留用户通过表单或命令显式选择。首期不支持非空包依赖自动映射；业务 ToolRef、scopes、租户白名单和飞书展示名称 `display_name` 由平台 `SkillRelease` 声明，缺少固定依赖会阻止装配。展示名称参与发布策略指纹，表单不会接受客户端传入的技能版本或权限。

调酒技能的上游提交与许可记录在包内
[SOURCE.json](../../financeclaw/shared/skills/builtin/cocktail-from-what-i-have/SOURCE.json)和
[LICENSE](../../financeclaw/shared/skills/builtin/cocktail-from-what-i-have/LICENSE)。正文属于固定发布内容，
维护时保留来源和许可，并同步核验版本/hash。

`builtin_skill_release` 对每个内置包分别声明平台策略。只有清单和已审阅声明同时存在才可装配，未知技能目录不能默认获得行情依赖或发布资格。

每文件最多 256 KiB、每包最多 64 文件/1 MiB。发布拒绝符号链接、特殊文件、重复 YAML 键、别名、未知策略、路径逃逸和缺失引用。所有文件的路径、大小、类型和字节 hash 组成包 hash；启动后只读不可变快照。主文变更应更新发布版本并审阅清单差异；生成脚本保留清单中的现有版本，新包默认 1.0.0。

```bash
.venv/bin/python scripts/skill_manifest.py
.venv/bin/python scripts/skill_manifest.py --check
uv build --wheel --out-dir build/skill-wheel
.venv/bin/python scripts/check_skill_wheel.py build/skill-wheel/financeclaw-0.1.0-py3-none-any.whl
```

新增资源类型或目录时一并检查 `pyproject.toml` 的 package-data。wheel 校验会在独立解包目录解析清单，防止 editable 安装掩盖缺失文件。CI 执行清单、wheel 和常规代码检查。

## 5. 来源、容量与故障定位

资源页、模型输出和 WorkingContext 保留平台生成的 `skill_access_refs`。工件 `access_policy` 持久保存同一依赖；通用 `read_artifact@3.0.0` 也要求当前 scope 激活相同固定包并逐项通过授权，缺少受信任校验器时拒绝读取。相邻资源范围合并，未读取的缺口和不同来源身份保留。

新 Turn 会清除不可用的派生正文和参数，已完成调用保持 call ID 配对；待执行/待审批的受限调用不能改写后继续。混合摘要整体失效。Journal 的 assistant 来源由该 Turn 实际模型 Manifest 的依赖并集固定，历史恢复不能洗掉约束；后台历史索引和记忆提取不使用此类受限 assistant 正文，原始用户证据仍按既有策略处理。

目录、主文、记忆、摘要、工具 schema、回执和最终收尾指令共用 `ContextBudgetPlanner`。激活先对候选执行既有记忆裁剪、工具清理和原生摘要，成功后一次 Command 更新 active、上下文及回执。失败不提交候选正文删除或摘要，已发生的摘要计量保留。显式失败把计量及错误写入 checkpoint 后受控结束，BFF 映射为失败；不会产生假最终回答。模型重试/fallback、摘要传输、工具派发均重新验证授权。

Manifest 区分本次主文 `skill_refs`、真正进入请求的资源范围 `skill_resource_refs` 与派生访问约束 `skill_access_refs`；摘要输入也保留实际资源来源。审计事件 `skill.load_prepared`、`skill.load_rejected`、`skill.resource_read` 不表示原生 checkpoint 已提交。

公开错误采用 `code + message`，API 受理错误为 HTTP 422。飞书对确定的选择错误返回可修正提示。

| 现象或错误码 | 处理方式 |
| --- | --- |
| `/skills` 没有目标技能 | 检查功能开关、会话发布、包清单、权限和依赖；新目录不会自动出现在菜单 |
| `SKILL_DIRECTIVE_INVALID` | 使用新 Turn 开头的 `/skill <技能 ID> <任务>`，不要放在引用或代码块中 |
| `SKILL_UNAVAILABLE` / `SKILL_DEPENDENCY_UNAVAILABLE` | 检查固定包、所需 ToolRef、scopes 与租户策略 |
| `SKILL_EXPLICIT_REQUIRED` | 包禁止隐式选择，改用表单或 `/skill` 明确指定 |
| `SKILL_ACTIVATION_LIMIT` | 当前 scope 的激活数量已满；精简同轮技能需求 |
| `SKILL_CONTEXT_BUDGET_EXCEEDED` | 完整正文无法装入当前预算；缩小任务上下文，正文不会被截断为成功 |
| `SKILL_RESOURCE_INVALID` | 只读取已激活包内的声明资源，使用工具返回的分页 cursor |
| `SKILL_RELEASE_MISMATCH` | 核对镜像、版本/hash 与 API/Worker 配置；过期表单重新打开，旧 checkpoint 不跨发布恢复 |
| 表单提交提示忙碌 | 先完成或停止原任务；打开新表单本身不会创建第二个有效任务 |
| 健康检查提示缺列 | 确认数据库是当前初始结构，或符合第 2 节补齐脚本的前置条件；部署不自动修复旧库 |

## 6. 验证与评测

```bash
.venv/bin/python -m pytest tests/skills -q
TIKTOKEN_CACHE_DIR='' .venv/bin/python -m pytest tests/skills -q
.venv/bin/python scripts/evaluate_skills.py
```

最后一条只验证固定问题集，默认不调用模型。`evals/skills-market-brief-v1.json` 包含 30 个合成问题，比较关闭、自动和显式三组。配置好模型供应商后，可运行：

```bash
.venv/bin/python scripts/evaluate_skills.py --live --output build/skills-evaluation.json
```

该命令执行最多 90 个合成任务，只开放演示行情和计算业务工具，记录真实尝试、供应商 usage（缺失保留空值）、耗时、工具、激活和待人工审阅的回答。它不经过生产受理、Worker 委派或飞书渠道；质量、成本及渠道验收需分别报告，不能将问题集和离线响应当成真实质量结果。

历史实现与验收见 [Skills 实现与验证](../../.redesign/stages/skills-实现与验证.md)，其中的测试数量、
数据库操作和部署状态属于当时记录。本页更新不表示重新执行了真实模型评测或飞书端到端验收。

## 7. 源码入口

| 入口 | 负责什么 |
| --- | --- |
| [builtin/](../../financeclaw/shared/skills/builtin) | 固定技能包、清单、资源与来源许可 |
| [releases/skills.py](../../financeclaw/shared/releases/skills.py) | 每个技能的展示名、工具依赖与 scopes |
| [packages.py](../../financeclaw/shared/skills/packages.py) | 包文件、路径、YAML 和 hash 校验 |
| [directives.py](../../financeclaw/shared/skills/directives.py) | `/skill` 显式选择语法 |
| [skills/service.py](../../financeclaw/agent_server/skills/service.py) | 激活准备、资源读取与请求来源 |
| [middleware/skills.py](../../financeclaw/agent_server/middleware/skills.py) | 模型请求中的目录、正文、授权和容量边界 |
| [skill_task_forms.py](../../financeclaw/api/application/skill_task_forms.py) | 飞书表单创建、提交、幂等受理与冻结选择 |
