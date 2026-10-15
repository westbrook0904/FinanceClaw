# M6b：固定规则的 Java 排盘算法

日期：2026-10-15。历法部分已本地提交为 `a2b67b1`，未推送。本次在该提交上实现固定规则的完整本命与流运计算。**这是算法层的本地兼容验证，紫微工具仍未注册，`financeclaw.business.ziwei.enabled=true` 仍拒绝启动。** 出生资料治理、结果预算和原生 AgentScope 接入留在 M6c。

## 实现与来源

[ZiweiChartEngine](../../src/main/java/com/financeclaw/ziwei/ZiweiChartEngine.java)接受明确的公历出生日期、时辰、排盘性别，流运另需明确目标日期。使用 [ZiweiCalendar](../../src/main/java/com/financeclaw/ziwei/ZiweiCalendar.java)处理旧月界兼容，主辅星、杂曜、四化及宫位计算采用 `x-iztro 0.4.0` 固定源码的 Java 移植。

本地实现标识为 **`financeclaw-java/x-iztro-fixed-0.4.0/1`**，源 commit 为 `cd14dfd8cf18dcc1ab248ba3c20c86903f5bc07f`。已核对下载的 41 个 Rust 文件与该 commit 的 Git blob 摘要。实际移植的是默认排盘路径、星曜安置、流运和中文输出，不包含上游其他流派、查询接口、反推、语言或绑定层。

| 内容 | 实现 |
| --- | --- |
| 固定规则 | `yearDivide=normal`、`horoscopeDivide=normal`、`ageDivide=normal`、`dayDivide=forward`、`algorithm=default`、`astroType=heaven`、`fixLeap=true`、`zh-CN` |
| 本命 | 命身宫、五行局、14 主星、14 辅星、38 杂曜、亮度、四化、四组十二神、大限区间与小限年龄 |
| 流运 | 大限/童限、小限、流年、流月、流日及旧原始结果中的流时；目标时辰固定早子时，与五种工具旧约定一致 |
| 晚子时 | 按旧源码分别处理日柱和紫微起日，月末按实际农历月天数回卷；不预先将出生公历加一天 |
| 状态 | 每次计算独立；公开结果中的所有 Map/List 递归不可变，不保存用户、当前盘面或系统时钟 |
| 协议 | 只提供领域计算方法，没有新增 Agent、Skill、MCP 或工具协议 |

星曜表由 [import-ziwei-tables.py](../../scripts/import-ziwei-tables.py)从锁定源码提取：162 个名称标识、20 组亮度表、十干四化、命主和身主。**运行时只包含算法和常量表，不读取测试盘面或日期结果表。** 其余公式在 [ZiweiStars](../../src/main/java/com/financeclaw/ziwei/ZiweiStars.java)与引擎中按固定规则移植。

[依赖锁](../../ziwei-dependencies.lock.json)保存来源和文件摘要；[第三方声明](../../THIRD_PARTY_NOTICES.md)及应用 jar 保留 x-iztro、上游 iztro 和 lunar-java 的 MIT 声明。Java 构建及算法运行不引入 Rust、Python、Node、WASM 或 FFI 依赖。

## 完整盘面比对

原 M6a 的 9 组原始基准没有修改。新增 [冻结语料清单](../../src/test/resources/ziwei/charts/manifest.json)及压缩 JSONL，包含 **2,515 组完整本命和流运结果**：

| 样本组 | 数量 | 覆盖内容 |
| --- | ---: | --- |
| 六十甲子 × 13 时辰 × 男女 | 1,560 | 全部生年干支、时辰和顺逆行，变化的出生与目标日期 |
| 历法边界 | 492 | 两处兼容月界、闰月前后半月、大小月末晚子时、年界、节界、1901/2099 范围端点 |
| 明确农历输入 | 182 | 正常月与闰月、月末、全部时辰与男女 |
| 年龄区间 | 280 | 1–140 虚岁，起运前、各大限和旧算法表覆盖之后的行为 |
| 早于出生的目标日期 | 1 | 保留原始引擎的年龄下限行为，产品是否允许由 M6c 决定 |

生成器使用固定旧源码和精确包版本，不加载旧应用、用户记录或凭据。两次独立导出的压缩结果和清单逐字节一致；语料不使用 Java 实现生成期望值。

[ZiweiChartEngineTest](../../src/test/java/com/financeclaw/m6/ZiweiChartEngineTest.java)逐字段检查完整 JSON，包括类型、字段集合、空串与缺省、语言无关标识和数组顺序。只在有差异时报告字段路径，不过滤不一致字段。还验证全部五行局、六十组出生/目标年柱、13 时辰、两种性别、非法输入、嵌套结果不可变及并发隔离。

旧引擎在大限表范围外会退回童限映射，小限超过 120 岁会回到索引 0；本次明确保留并测试这种兼容行为，不能将其解释为独立算法正确性证明。M6c 应决定允许的业务年龄和目标范围，不能默默修改同一规则版本的输出。

## 时区与发布规则

Java 继续使用固定 **JDK 21.0.12+8 / TZDB 2026b**，不替换 JDK 或加载 Python 时区数据库。出生时间与查询日期通过 `ZiweiDates` 显式处理；排盘引擎本身接受日期标签，不读取默认时区。此前 9 个地区、4,218 个偏移样本的一致性证据仍归属[历法验证](m6b-calendar.md)。

发布策略确定为新的 Java 民用时间规则标识 **`financeclaw-ziwei-java-civil@1.0.0`**，其快照必须包含引擎、历法兼容规则和 TZDB 2026b。M6c 需要把它接入发布与归属校验。不能沿用 Python 的 `x-iztro-civil-candidate@1.0.0` 恢复旧任务，也不能把这次抽样验证写成所有 IANA 时区与 Python `tzdata 2026.3` 等价。将来升级任何规则或依赖须形成新的发布并重新验证。

## 验证和复现

完整回归 **545 项通过**（439 单元/契约、106 集成），0 失败、0 错误、0 跳过，格式和静态交付检查通过。本次新增 14 项算法测试和 1 项打包集成测试；前者内部逐字段对比 2,524 组完整盘面。机器记录见 [完整测试与源码指纹](evidence/m6b-chart-results.json)、[原 9 组独立比较器结果](evidence/m6b-chart-comparison.json)及[扩展基准复现](evidence/m6b-chart-baseline.json)。

[ZiweiPackagedIT](../../src/test/java/com/financeclaw/m6/ZiweiPackagedIT.java)从应用 jar 加载实际算法，以独立测试入口在临时目录运行；环境变量清空、PATH 为空、类路径不含测试资源。UTC 与 Pacific/Auckland 两个独立 JVM 均通过 9 组完整盘面比对，确认三份许可证随包保留，jar 不包含冻结盘面、历法测试表或 Python 脚本；见 [打包证据](evidence/m6b-chart-packaging.json)。这是独立 JVM 实验，未声称重跑 Compose 或生产容器验收。

```bash
scripts/mvn.sh -B -ntp clean verify spotless:check
python3 scripts/collect-evidence.py
python3 scripts/compare-ziwei-charts.py \
  --candidate target/m6/java-charts.json \
  --engine-ref 'financeclaw-java/x-iztro-fixed-0.4.0/1'
```

比较器针对原 9 组基准，预期 `matched=9`；新增 2,515 组由 Java 测试直接逐字段比对压缩语料。单独重跑算法测试：

```bash
scripts/mvn.sh -B -ntp -Dtest=ZiweiChartEngineTest test
```

刷新合成语料仅供维护基准，无须用于正常 Java 构建：

```bash
/path/to/FinanceClaw/.venv/bin/python -B scripts/export-ziwei-charts.py \
  --python-root /path/to/FinanceClaw
python3 scripts/import-ziwei-tables.py --source /path/to/pinned/x-iztro
```

输出留在 `target/m6/`，先审查再更新源文件；提取的 Java 常量表需运行项目的 Spotless 格式化。任何源码/依赖摘要不匹配都会拒绝导出。

下一步为 **M6c**：完整出生输入与澄清、固定发布规则、归属与隐私、区间事实分段、Artifact/投影预算，再接入 AgentScope 原生 Toolkit。之后由 M6d 验证重启与真实调用并决定启用。本次未调用真实供应商、MCP、飞书，也未重跑 M5 Compose 或远程 CI；新增算法改动尚未提交。
