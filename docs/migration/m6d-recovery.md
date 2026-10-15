# M6d：紫微进程恢复与部署验收

日期：2026-10-15。基线为本地提交 `ef4057b`（M6c，未推送）。本阶段复用已有 AgentScope ReActAgent、Toolkit、AgentState、Interaction、Worker、MyBatis 回执和 Artifact。没有新增恢复框架或紫微专用执行协议。**本地验证和真实模型验证分开记录；真实模型尚未配置，普通启用门禁继续保留。**

M6d 已提交为本地 commit `72e0655`，未推送。本页保留该阶段的历史验证结果；后续补充的 Java 受理暂停与部署恢复检查见 [M5b](m5b-admission.md)。

## 独立进程中的恢复边界

验收启动实际 Spring Boot JAR，仅将脚本模型和故障驱动放入额外测试 launcher。宿主进程环境清空、PATH 为空、工作目录隔离，测试资料为合成数据；恢复进程没有上一进程的内存或会话对象。故障用 `Runtime.halt(73)` 制造，不执行优雅关闭。

| 故障或竞争位置 | 应有结果 |
| --- | --- |
| 原生澄清已保存 AgentState、Interaction 和封存回执，尚未提交最终 Turn 状态 | 新进程恢复为 waiting；同一答案幂等提交，再由另一进程恢复原生工具；只有一个 Artifact |
| 工具结果已归档、SDK 执行已封存，尚未提交完成状态 | 新进程补齐 completed、会话输出和 outbox，不重复工具或新增 attempt |
| 工具结果已经归档，但没有封存 Turn 完成回执 | 新进程进入 `blocked / unconfirmed_execution`；保留已有受控 Artifact，不自动重跑、补造成功或新建命令 |
| 澄清答案已入库、Worker 尚未领取时取消 | 原 Turn cancelled，答案不触发新工具 |
| Worker 重启前同版本密钥变化、旧发布不可用、授权变化或忘记操作 | 分别按密钥回执、发布、grant revision、privacy epoch 拒绝继续，零 Artifact |
| 工具调用前或结果返回后，取消/忘记与执行竞争 | 取消确认后为 cancelled；忘记后为 blocked；没有 assistant 完成记录；忘记后已有紫微 Artifact 不可读 |

原 Turn 创建时间固定为测试边界 `2025-12-31T16:00:00Z`；查询时区来自原发布，独立 JVM 的默认时区设为 Pacific/Auckland。恢复后的 `year_offset=0` 仍覆盖上海时区的 `[2026-01-01,2027-01-01)`。发布不可用时拒绝恢复，不用新发布代替旧规则。

竞争测试使用文件屏障停在脚本模型边界，由另一进程提交取消或忘记，再继续执行。测试不修改生产代码的恢复流程。取消发生在 Artifact 已提交后，不声称撤回该事实；忘记使用既有记忆 epoch 使其不可读，也不等于物理删除会话、checkpoint 和 Artifact。

## 容器和 secret 配置

实际 [Dockerfile](../../Dockerfile) 仍固定 Temurin 21.0.12+8 JRE 镜像摘要。镜像验收使用 `docker build` 与同一个应用 JAR；容器内确认没有 `python`/`python3`，通过 AgentScope 五个原生工具分别写入五份受控 Artifact。脚本模型只存在额外测试 launcher 中，不进入应用 JAR 或部署镜像。

Compose 的 API、Worker、Integrations 共享以下发布参数及同一 HMAC secret；Memory Worker 和迁移角色不接收该 secret：

```dotenv
FINANCECLAW_ZIWEI_ENABLED=true
FINANCECLAW_ZIWEI_DEVELOPMENT=true
FINANCECLAW_ZIWEI_KEY_VERSION=dev-v1
FINANCECLAW_ZIWEI_QUERY_TIMEZONE=Asia/Shanghai
```

先运行 `python3 scripts/init-local.py` 补齐 `.secrets/ziwei-key`，已有文件不会覆盖；脚本生成至少 32 字节的随机密钥，不打印值。Compose 通过 Spring configtree 挂载为 `ZIWEI_HMAC_KEY`，密钥不写入 `.env`、发布快照或镜像。不同角色不得各自生成不同密钥；轮换必须同步更新 key version，并处理旧 Turn 发布可用性，不能在旧版本下静默替换。

默认 `enabled=false`、`development=false`。只设置 enabled、缺少 key version、密钥过短或规则/TZDB 不匹配都会拒绝启动。已有 `.env` 不会被初始化脚本覆盖，需要手动加入开发测试参数。

部署回归复用 [validate-compose.py](../../scripts/validate-compose.py) 的随机隔离项目、真实 PostgreSQL、四运行角色、SIGKILL、故障恢复和备份还原流程；`--ziwei` 额外启用上述共享发布和密钥配置。该流程仍使用 offline 模型，五工具调用由独立镜像测试验证，不能当作真实模型选工具的证据。

## 真实模型验收入口

当前本地模型为 offline，模型地址及模型密钥为空。本阶段没有访问真实模型，也不解除开发模式门禁。

在已授权、已启用 Ziwei 开发配置的独立测试部署中，配置 `FINANCECLAW_LIVE_BASE_URL`、`FINANCECLAW_LIVE_API_TOKEN` 并显式设置 `FINANCECLAW_LIVE_ACCEPTANCE=true`。沿用现有 [validate-live.py](../../scripts/validate-live.py)，不用新增 API 或模型客户端：

```bash
for tool in ziwei_natal_chart ziwei_decadal_chart ziwei_yearly_chart ziwei_monthly_chart ziwei_daily_chart; do
  python3 scripts/validate-live.py --kind ziwei --expected-tool "$tool" || break
done
python3 scripts/validate-live.py --kind ziwei --expected-tool ziwei_yearly_chart --clarify
```

每次生成新测试会话，只授予 `ziwei:read`、`artifact:read`，使用固定合成出生资料和只读查询。提示词用“本命/大限/流年/流月/流日”表达需求，没有强制工具函数名。澄清场景故意省略排盘性别，只回答原生 input Interaction 的该字段；异常工具、审批或第二次澄清不会自动批准或猜填。

通过条件为 Turn completed、预期原生工具事件、严格 `chart_only` JSON、Artifact/chart/层级/target 一致、目标日期与合成请求一致、13 个本命核心事实匹配冻结的合成基准、完整分段覆盖、至少两个不同事实的标量引用值与受保护 Artifact 一致，并包含本命和所请求层级。读取遵循现有分页与字节预算，检查分页进度和稳定摘要。编造引用、值不一致、缺段、文本回答、猜测缺失资料均不通过。

脚本只保存 Turn/会话 ID、提示摘要、Artifact 摘要、计数和结果，不保存出生资料或模型全文。开始执行后即保存原 Turn 标识；失败或超时不重试提交，不用新 key 重放不确定执行。证据位于 `target/m6/live-*.json`。CI 的手动 live workflow 也支持 `ziwei` 与 `clarify`，使用已有受保护环境的凭据。

验收器仅读取仓库中已冻结的 JSON 基准，不调用旧 Python 算法。

这些自动检查验证合成盘面、事实引用与结构，不证明自然语言解读质量、所有出生样本的准确率、供应商身份或生产容量。真实环境完成五入口及澄清验收、审查输出和资料保留政策后，才能单独决定是否解除普通启用门禁。

## 复现与结果

```bash
scripts/mvn.sh -B -ntp clean verify spotless:check
python3 scripts/test-live-acceptance.py
python3 scripts/collect-evidence.py
python3 scripts/init-local.py
python3 scripts/validate-compose.py --ziwei
python3 scripts/check-delivery.py
```

本次完整 Java 回归 **628 项通过**（495 单元/契约、133 集成），0 失败、0 错误、0 跳过，`spotless:check` 通过。其中 M6d 新增 13 项真实 PostgreSQL/独立进程/部署镜像测试；验收器另有 **8 项 Python 离线契约测试通过**，不计入 Java 数量。源码指纹、JAR/镜像摘要和用例列表见 [M6d 本地测试证据](evidence/m6d-local-results.json)。

启用 Ziwei 开发配置的 Compose 实验 **10 项检查通过**，使用同一份已验证 JAR。包括四角色健康启动、secret 角色隔离、幂等提交、Worker SIGKILL 后审批继续且只执行一次、数据库故障、忘记操作、33 张表备份还原的一致性及恢复后接收新 Turn。离线合成负载 12 次全部成功，P95 为 1.12 秒；不代表真实模型性能或生产容量。见 [M6d Compose 证据](evidence/m6d-compose-results.json)。随机隔离的测试项目与卷已清理，没有切换已有部署。

远程 CI、真实模型、真实 MCP、飞书租户和生产恢复均未在本轮执行。下一步需要配置已授权的真实模型测试环境，完成五入口及澄清验收并审查结果；门禁继续保留。

代码入口：[ZiweiRecoveryIT](../../src/test/java/com/financeclaw/m6/ZiweiRecoveryIT.java)、[ZiweiRuntimeProbe](../../src/test/java/com/financeclaw/m6/ZiweiRuntimeProbe.java)、[离线验收器契约测试](../../scripts/test-live-acceptance.py)。前置领域规则与隐私边界见 [M6c](m6c-domain.md)。
