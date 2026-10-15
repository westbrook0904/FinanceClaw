# M6：紫微迁移基准与日期契约

日期：2026-10-15。实现基线为 M5 本地提交 `b9ff13d`。本次完成 **M6a：冻结旧算法基准、迁移日期处理、建立全量盘面比对入口**。M6 的 Java 排盘引擎、领域结果治理和 AgentScope 工具接入尚未完成；`financeclaw.business.ziwei.enabled=true` 仍明确拒绝启动。

## 当前可运行的部分

[ZiweiDates](../../src/main/java/com/financeclaw/ziwei/ZiweiDates.java)使用 JDK `java.time`，不调用模型、不读取当前时钟、不访问数据库或远端服务。它负责日期与出生时间处理，**不是完整出生资料校验器或排盘引擎**，暂未注册到运行中的 AgentScope Toolkit。

| 契约 | 当前行为 |
| --- | --- |
| 五个固定工具名 | 本命、大限、流年、流月、流日分别解析；拒绝通用 `level` / `target`、执行身份和请求时钟参数 |
| 本命 | 不接受查询日期，返回无流运目标 |
| 其他层级 | 显式日期、区间、工具对应的相对偏移，或流年/流月年月；缺失目标不默认今天，多种日期表示不能混用 |
| 相对时间 | 只使用调用方传入的固定 `QueryClock`；时区来自执行上下文，区分出生地和查询地 |
| 日期区间 | `[start, end)`，1–366 天，实际包含日期在 1901–2099 年之间；整年/月保留完整区间 |
| 出生钟表时间 | 早子时与晚子时分开；不提前把晚子时日期加一天，日界调整留给引擎执行一次 |
| 时间精度 | 时辰和区间不生成虚构 UTC 时刻；跨日或跨时辰区间需澄清 |
| 夏令时 | 使用 `ZoneRules.getValidOffsets` 拒绝不存在的时间；重复时刻必须提供 `fold`；闭区间逐分钟检查 |
| 并发 | 不持有当前用户、当前盘面或可变全局时钟；不同请求的时钟与时区各自固定 |

出生历法、闰月 round-trip、地点与时区一致性、性别及规则、归属 HMAC、事实分段、Artifact、模型投影预算仍需要后续领域实现。工具参数中的 `birth` / `question` 等公共字段在本类中只允许通过，不视为已完成语义校验。数值参数只接受整数，拒绝字符串、布尔值及小数的隐式转换。

## 基准来源与复现

[基准目录](../../src/test/resources/ziwei/reference/manifest.json)冻结了旧项目实际读取文件的 SHA-256、源 commit、依赖版本及生成结果摘要。仅使用合成资料与公开示例日期，没有读取用户数据库或真实出生记录。

- 121 个目标日期样本：五入口、缺失/冲突目标、相对时区、跨年、闰年、区间边界、额外字段。
- 26 个出生时间样本：时辰边界、纽约夏令时缺口与重复时刻、上海历史夏令时、明确 fold、区间和模糊子时。
- 9 组完整引擎输出：男女、公历/农历等价、晚子时、闰月前后半月、1901/2099 边界，每组保存本命与流运原始 JSON。
- 旧实现五种工具的完整 JSON Schema，作为后续 AgentScope 原生工具接入的对照。

普通 Java 构建只读取这些已提交的测试资源，**不需要安装 Python、x-iztro 或运行旧服务**。刷新基准才需要源项目解释器，且必须精确安装 `x-iztro==0.4.0`、`tzdata==2026.3`、`pydantic==2.13.5`：

```bash
/path/to/FinanceClaw/.venv/bin/python -B scripts/export-ziwei-baseline.py \
  --python-root /path/to/FinanceClaw --output target/m6/reference
scripts/mvn.sh -B -ntp -Dtest=ZiweiDatesTest test
```

生成器不导入旧应用、加载 `.env`、启动数据库或写入旧仓库。旧源码指纹与冻结清单不一致时直接失败，不能悄悄更新期望值掩盖差异。输出先留在 `target/`，审查后才替换测试资源。当前已验证再次生成四个文件逐字节一致。

这些是**旧算法兼容性基准**，并非独立排盘真值认证，也没有把源 Python 全套测试标为通过。

## Java 引擎如何接入比对

候选实现按 [charts.json](../../src/test/resources/ziwei/reference/charts.json) 中的输入，在独立调用中输出相同结构的数组：`id`、`input`、`solar_date`、`natal`、`horoscope`。不得把基准文件当作运行时查询结果；应记录实现的固定版本或 commit。

```bash
python3 scripts/compare-ziwei-charts.py \
  --candidate target/m6/java-charts.json \
  --engine-ref 'candidate-name@exact-version'
```

预期退出 0 且报告 `matched=9`。比较器验证基准摘要、完整样本集合、JSON 类型、所有字段和数组顺序，拒绝重复 ID、重复 JSON 键和非有限数值；对象属性顺序不影响结果。差异报告只列字段路径，不输出原始出生数据。算法和规则字段同样比较，不能静默忽略。

本次使用参考输出验证比较器自身能通过，并故意修改一个盘面字段、重复样本、删除样本，确认均返回失败。**这不是 Java 算法通过比对**；`--engine-ref` 只记录调用者提供的来源，不认证候选引擎。

## 算法与依赖核查

截至本次核查，还没有选定、引入或验证可替换旧引擎的 Java 算法依赖：

| 候选来源 | 核查结果与采用条件 |
| --- | --- |
| [x-iztro 0.4.0](https://pypi.org/project/x-iztro/0.4.0/) | 当前固定基准；上游为 Rust 核心与 Python/Go 绑定，包声明 MIT。调用 Python 或嵌入 Rust/WASM 不能被称为纯 Java 算法替换 |
| [iztro](https://github.com/SylarLong/iztro) | JavaScript/TypeScript 实现，[许可证为 MIT](https://github.com/SylarLong/iztro/blob/main/LICENSE)。可作为实现来源候选，但本次没有搬运算法、加入 JavaScript 运行时或核实其与旧固定版本逐字段兼容 |
| [xuan-deduct](https://github.com/H1d3rOne/xuan-deduct) | 提供 Java 紫微模块的候选项目；本次只核查公开项目说明，未审完具体实现、传递依赖或样本兼容性，未引入依赖 |

后续选定的精确版本需保存源码/制品摘要、许可证及必要声明，并独立核对传递依赖。这里记录技术筛选依据，不把仓库首页的许可标签视为整个供应链审核完成。

固定 JDK **21.0.12+8 的 TZDB 为 `2026b`**，旧 Python 为 `tzdata==2026.3`。147 个日期/时间样本当前一致，但不能认定两个版本对所有地区和历史时间完全一致。后续必须扩大时区差异检查，选择明确固定的时区规则及新的领域 convention，不能改动用户要求固定的 JDK，或把新规则冒充旧发布继续恢复。

## 验证与剩余步骤

[ZiweiDatesTest](../../src/test/java/com/financeclaw/m6/ZiweiDatesTest.java)包含 147 个跨语言日期样本，以及基准完整性、并发隔离和严格输入检查，共 **150 项通过**。完整 Java 回归 **294 项通过**（189 单元/契约、105 PostgreSQL 集成），0 失败、0 错误、0 跳过；格式与静态交付检查通过。测试及源码指纹见 [M6a 机器记录](evidence/m6-results.json)，生成器复现与比较器自测见 [基准工具记录](evidence/m6-baseline-results.json)。M5 的 9 项 Compose 证据仍对应提交 `b9ff13d` 中的 M5 jar，本次没有将它们标作新 jar 的容器复测结果。

后续按以下顺序推进：

1. **M6b 算法实现**：确定精确 Java 候选、许可证及依赖，完成公历/农历/闰月和五类盘面的逐字段对比；扩展性别、时辰、节界、年界和历法范围样本。确认时区规则的版本差异处理。
2. **M6c 领域接入**：补齐出生资料澄清、固定规则、归属、区间事实分段和有界 Artifact 投影；复用现有治理能力与 AgentScope 原生 Toolkit，不另建 Skill/MCP/Agent 协议。
3. **M6d 启用验收**：验证重启恢复、并发隔离、权限与隐私、真实模型工具调用，以及镜像无 Python 算法运行依赖；通过后才允许启用紫微，仍保持开发/测试定位。

M5 的真实供应商、MCP、飞书租户、远程 CI 和生产恢复验收继续独立待办，M6a 的通过不替代这些条件。
