# 测试与验证指南

从仓库根目录执行以下命令。项目使用 Python 3.13，依赖版本由 [`uv.lock`](../uv.lock) 固定；完整 CI 定义见 [`.github/workflows/ci.yml`](../.github/workflows/ci.yml)。初次了解项目可先读[文档导航](../docs/README.md)和[开发指南](../docs/development.md)。

`tests/` 随当前代码维护，`stage1`～`stage11` 是历史分组名，不表示正在测试旧版系统。例如 `stage3` 的记忆测试已验证 Stage 11 的 SQL 事实与来源治理。阶段当时的通过记录保存在 [`.redesign/`](../.redesign/README.md)，不能替代本次执行结果。

## 先跑基础回归

```bash
uv sync --frozen --extra dev
uv run --no-sync pytest -q -m "not external"
```

基础测试主要使用确定性模型、合成凭据、临时 SQLite、内存 checkpoint、模拟 HTTP 或 SDK 传输。[全局 fixture](conftest.py) 固定读取 `tests/fixtures/models.toml` 和 `tests/fixtures/mcp.toml`，并禁用 Settings 自动加载本地 `.env`。修改个人模型配置不会把普通单测变成真实模型验收。

`-m "not external"` 排除当前标记为 `external` 的飞书连接测试。PostgreSQL 用例另由环境变量控制；若已经注入 `FINANCECLAW_TEST_POSTGRES_URL`，上述命令也会执行相应数据库测试。使用测试专用环境可让运行条件更明确。

查看跳过原因时使用 `pytest -q -rs`。跳过不是验证通过；应同时记录代码版本、依赖组、命令、外部条件和 pytest 汇总。

## 按改动选择测试

| 改动范围 | 测试入口 | 主要验证内容 |
|---|---|---|
| 服务装配、依赖、认证和发布 | `architecture/`、`stage5/` | 模块依赖、角色启动、权限、网络出口、审计、脱敏和制品隔离 |
| 模型配置与基础工具 | `stage1/` | 模型档案、供应商参数、工具调用、重试、审批和工具审计 |
| Artifact 与记忆权限 | `stage2/`、`stage3/` | 工件归属、迁移约束、记忆来源和所有者校验 |
| Workflow 与紫微 Subagent | `stage4/`、`stage7/` | Worker 执行范围、工作流审批、紫微计算和文本结果契约 |
| 飞书接入与交互 | `stage6/`、`stage6fix/`、`stage6fixc/` | 单聊准入、资源预算、批次隔离、原生中断和恢复 |
| 通知与卡片发送 | `stage8/` | 持久通知、分片、发送不确定性、撤销和回执 |
| 根图和子图集成 | `stage8_hotfix/` | 原生子图、澄清、流式卡片、工具进度、上下文容量和初始 Schema |
| 上下文与历史索引 | `stage9/` | 原生消息处理、Artifact 视图、历史保留、索引和恢复 |
| 当前产品运行模型 | `stage10/` | Turn 受理、幂等命令、Journal、SSE、状态观察和 HTTP 契约 |
| 异步记忆与上下文治理 | `stage11/` | 来源提取、候选确认、纠正/遗忘、检索快照、预算、独立进程与 PostgreSQL 并发 |
| Skills 与飞书技能表单 | `skills/` | 技能包、权限、请求投影、调酒技能、表单受理和发布刷新 |
| 通用 MCP 与酒店结果读取 | `mcp_integration/` | 服务发现、工具发布、执行、部署准备和酒店结构化结果视图 |
| 太卜 MCP | `taibu/` | 固定 Schema、SDK 传输、黄历/八字工具、原始结果和工件 |

例如，只检查 Skills 与通用 MCP：

```bash
uv run --no-sync pytest -q tests/skills tests/mcp_integration
```

领域测试会按需要注入固定 Worker scope；根图集成测试保留发布和权限校验。使用真实图或真实 SDK 类型，只说明执行框架参与验证，不能据此认定已经连接真实供应商、MCP 服务或飞书租户。

## 可选能力与 CI 对齐

紫微真实计算依赖 `ziwei` extra。未安装时，依赖 `x_iztro` / `tzdata` 的用例按条件跳过，其余契约和禁用行为仍可测试。

```bash
uv sync --frozen --extra dev --extra ziwei
uv run --no-sync python -c "from financeclaw.agent_server.domains.ziwei.adapters.x_iztro import XIztroEngine; XIztroEngine()"
uv run --no-sync pytest -q tests/stage7 tests/stage8_hotfix/test_production_subgraphs.py
```

CI 分 `base` 和 `ziwei` 两组，分别安装 `dev` 和 `dev + ziwei`。每组运行以下检查，并执行依赖漏洞审计：

```bash
uv run --no-sync ruff check financeclaw tests scripts deploy experiments/stage10 experiments/taibu
uv run --no-sync ruff format --check financeclaw tests scripts deploy experiments/stage10 experiments/taibu
uv run --no-sync python scripts/check_secret_leaks.py
uv run --no-sync python scripts/skill_manifest.py --check
uv build --wheel --out-dir build/skill-wheel
uv run --no-sync python scripts/check_skill_wheel.py build/skill-wheel/financeclaw-0.1.0-py3-none-any.whl
TIKTOKEN_CACHE_DIR="" uv run --no-sync pytest -q
uv run --no-sync python scripts/generate_sbom.py
```

`TIKTOKEN_CACHE_DIR=""` 对齐 CI 的 tokenizer 缓存设置，用于暴露本机已有缓存可能掩盖的问题；它不会禁止全部网络访问。软件物料清单（SBOM）、生产依赖导出及 `pip-audit` 的准确参数以 workflow 为准。CI 没有配置 PostgreSQL 服务或真实飞书凭据，也没有运行下方独立实验。

## PostgreSQL：显式提供隔离测试环境

以下测试读取进程环境中的 `FINANCECLAW_TEST_POSTGRES_URL`，未提供时跳过。请通过私有环境文件或凭据管理方式注入连接串，使用可丢弃的测试实例，不使用业务数据库。

| 测试文件 | 数据隔离方式 | 连接账号需要的能力 |
|---|---|---|
| `stage11/test_worker_postgres.py`、`stage11/test_domain_postgres.py` | 每次新建随机 schema，结束只删除该 schema | 创建/删除 schema、建表及正常读写 |
| `stage11/test_worker_role_postgres.py` | 每次新建数据库和角色，结束删除这两个测试对象 | 创建/删除数据库与角色，并执行角色授权；建议专用测试集群管理员 |

先运行并发与迁移回归，再按权限条件单独运行角色验证：

```bash
uv run --no-sync pytest -q -rs tests/stage11/test_worker_postgres.py tests/stage11/test_domain_postgres.py
uv run --no-sync pytest -q -rs tests/stage11/test_worker_role_postgres.py
```

这些测试验证真实数据库连接、租约、锁、版本冲突、迁移和最小权限。SQLite 通过不证明 PostgreSQL 的 `SKIP LOCKED` 或跨连接并发行为。

`stage8` 中通知并发和发送进程接管用例仍有 PostgreSQL 条件，但其共享 fixture 当前固定使用临时 SQLite，默认会跳过。设置 `FINANCECLAW_TEST_POSTGRES_URL` 不会切换该 fixture；这两项跳过不能算作已覆盖的真实进程恢复验收。

## 独立探针与真实服务

这些入口不属于普通 CI，运行前先阅读各自说明。实验可能启动本机服务、创建临时数据或调用外部服务；依赖、端口、凭据和清理方式应按对应 README 准备。

| 入口 | 运行条件 | 能证明什么 |
|---|---|---|
| [原生子图探针](../experiments/stage8_hotfix/README.md) | `dev` 依赖；完整模式需要 loopback 监听 | 合成模型下的子图、interrupt/resume、checkpoint 和本机 HTTP 契约 |
| [Stage 9 Store 探针](../experiments/stage9/README.md) | 独立 Store 探针需 `agent-server` extra、pgvector 及 `FINANCECLAW_TEST_POSTGRES_DSN`；原生 HTTP 探针仅供历史复现 | 合成向量下的 Store 写入、索引过滤、删除和隔离；旧 HTTP 探针使用已删除接口，不作为当前版本验收入口 |
| [Stage 10 持久运行探针](../experiments/stage10/README.md) | Docker、专用 Compose 项目、AgentServer 所需 license/key | 离线模型下的产品受理、持久恢复、迁移、多 API 和容量测量 |
| [太卜联调](../experiments/taibu/README.md) | 可访问的 MCP 服务端点 | 合成输入下真实 MCP HTTP、SDK、结果归档和回读 |

Stage 8/9/10 实验用于复现对应契约和阶段证据，不能覆盖后续 Skills、记忆治理或所有新增能力。PostgreSQL Store 探针使用的 `FINANCECLAW_TEST_POSTGRES_DSN` 与应用数据库测试的 `FINANCECLAW_TEST_POSTGRES_URL` 是不同变量。

### 真实模型

普通 pytest 没有一个“提供 Provider key 即自动运行全部模型验收”的开关。先按[统一模型配置](../docs/operations/model-configuration.md)准备模型档案与密钥，再选择具体评测或产品流程。

Skills 提供独立的三组对照评测脚本；不加 `--live` 时只检查问题集。下面真实模式每组执行 1 个合成问题，共 3 个任务，会调用已配置的供应商：

```bash
uv run --no-sync python scripts/evaluate_skills.py
uv run --no-sync python scripts/evaluate_skills.py --live --limit 1 --output build/skills-evaluation.json
```

报告包含供应商用量、耗时、工具和回答，质量标记为待人工审阅。该脚本不经过产品受理、Worker 委派或飞书；完整技能验收见 [Skills 运行说明](../docs/operations/skills.md)。记忆提取、摘要与 embedding 质量也需要独立语料和真实模型评估，不能用确定性模型的结果代替。

### 真实飞书连接

通过进程环境注入 `FINANCECLAW_FEISHU_E2E_APP_ID`、`FINANCECLAW_FEISHU_E2E_APP_SECRET`、`FINANCECLAW_FEISHU_E2E_OPEN_ID`，然后运行：

```bash
uv run --no-sync pytest -q -rs -m external tests/stage6/test_feishu_live.py
```

这个用例只验证官方 SDK WebSocket 连接和 ready 状态，探针服务忽略收到的业务消息，不创建会话、不发送卡片。真实卡片展示、点击、澄清/审批、最终答复与重复点击仍需按[通知与飞书](../docs/operations/notifications.md)及[发布检查表](../docs/operations/release-checklist.md)在测试租户验收。

## 如何报告验证结果

把静态检查、自动回归、真实数据库/HTTP 探针、真实模型质量和飞书客户端验收分别记录。例如“定向回归通过，PostgreSQL 因未配置而跳过，未执行真实飞书点击”，便于后来者判断风险和复现。

新增证据应记录执行日期、代码版本、依赖、命令、退出码、通过/跳过原因和未覆盖范围。历史报告中的成功数量属于当时版本；当前交付使用本次实际运行的输出。
