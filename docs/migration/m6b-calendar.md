# M6b 第一部分：Java 历法与旧规则兼容

日期：2026-10-15。基线为 M6a 本地提交 `89216ca`，未推送。本次完成 M6b 的历法选择、适配和逐日验证；**星曜安置、本命盘、五类流运的 Java 算法尚未完成**，运行中的紫微能力仍保持关闭。

## 已实现

[ZiweiCalendar](../../src/main/java/com/financeclaw/ziwei/ZiweiCalendar.java)复用固定的 `cn.6tail:lunar:1.7.7`，提供公历日期到农历、出生农历到公历的转换。只使用显式年月日，不读取当前时钟或系统时区，不返回第三方可变对象，不修改第三方全局规则。转换结果为不可变 Java record。

输入边界保持旧领域规则：出生年份 1901–2099，农历必须明确是否闰月，拒绝不存在的闰月、无效日和转换后越界的公历日期。农历转换回读验证，异常只暴露稳定错误码与字段路径。公历 1901 年初转换出的农历年份可能为 1900，但不因此允许用户填写农历出生年 1900。

这仍是历法适配器，未注册 AgentScope 工具，也没有创建新的 MCP、Skill、工作流或 Agent 框架。

## 实测差异与处理

根据冻结的 `x-iztro==0.4.0`，逐日生成 **1901-01-01 至 2099-12-31 的 72,684 个公历→农历结果**。三个未修改的 Java 库候选结果如下，原始记录见[候选证据](evidence/m6b-calendar-candidates.json)：

| Maven 版本 | 比较日期数 | 不同的日期数 | 差异范围 |
| --- | ---: | ---: | --- |
| `cn.6tail:lunar:1.7.4` | 72,684 | 30 | 1925 年一处月界 |
| `cn.6tail:lunar:1.7.6` | 72,684 | 60 | 1925、2057 年两处月界 |
| `cn.6tail:lunar:1.7.7` | 72,684 | 60 | 1925、2057 年两处月界 |

例如 `1925-05-22`：旧引擎返回农历四月三十，1.7.7 返回闰四月初一；`2057-09-28`：旧引擎返回九月初一，1.7.7 返回八月三十。这些差异来自实际执行，未通过修改期望结果消除。

选择 1.7.7，并以独立规则标识 **`x-iztro-0.4.0-calendar-compat/1`** 保存旧月界。适配层仅覆盖以下四个月的起始日期与天数，其他日期全部委托第三方库：

| 农历月 | 旧规则的公历首日 | 天数 |
| --- | --- | ---: |
| 1925 年四月 | 1925-04-23 | 30 |
| 1925 年闰四月 | 1925-05-23 | 29 |
| 2057 年八月 | 2057-08-30 | 29 |
| 2057 年九月 | 2057-09-28 | 30 |

两个相邻月份一起处理，保证正向、反向和无效日期拒绝一致。没有使用完整测试表作为运行时查询库，没有修改第三方 jar，也没有把规则交给模型生成。第三方原始差异测试会继续保留，见[完整 60 天明细](evidence/m6b-native-calendar-differences.json)。

该选择的依据是**保留旧算法结果**，不是认定旧库或新库具有独立天文正确性。后续若改变算法规则，应发布新的 convention，不能沿用此兼容标识；后续排盘实现必须经过本适配器取得农历日期及月界，不能直接调用第三方对象绕过兼容层。

## 版本、源码与许可证

[依赖锁文件](../../ziwei-dependencies.lock.json)记录 Maven jar、source jar、POM 的 SHA-256，以及上游 commit `0194eb4574f33ab056fe7cac62a9d8bf24272478`。发布源码中的 **31 个 Java 文件**与该 commit 的 Git blob 全部一致。Maven POM 声明 MIT，运行时没有传递依赖。

[原始 MIT 声明](../../src/main/resources/META-INF/licenses/lunar-1.7.7.txt)已随应用资源打包，见 [第三方声明](../../THIRD_PARTY_NOTICES.md)。构建测试核对实际加载的 jar 摘要和许可证资源，依赖版本、JDK 21.0.12+8、Spring Boot 3.5.16、AgentScope 2.0.3 均保持固定。

另外核查了 [`xuan-deduct` 的固定源码](https://github.com/H1d3rOne/xuan-deduct/tree/30725e09f0149492010a7cc832d1f57c5e2360f8)：其年龄计算读取 `LocalDate.now()`，没有等价的固定目标日期五类流运入口。本次未引入该项目、其 Spring/JPA/MySQL 依赖或算法源码。[x-iztro 固定版本](https://github.com/x-haose/x-iztro/tree/cd14dfd8cf18dcc1ab248ba3c20c86903f5bc07f)仍作为兼容基准，Rust 核心尚未移植到 Java。

## 时区验证范围

JDK 自带 TZDB `2026b`，旧环境为 `tzdata==2026.3`，两个版本标识没有混用。本次对下列 9 个区域，在 1901–2099 年逐 UTC 日检测偏移变化，并二分定位变化秒，验证边界前一秒、边界和后一秒及范围端点：

`Asia/Shanghai`、`Asia/Hong_Kong`、`Asia/Taipei`、`America/New_York`、`Pacific/Auckland`、`Europe/London`、`Australia/Lord_Howe`、`Pacific/Apia`、`UTC`。

共 **4,218 个偏移样本一致**。M6a 的缺口、重复时刻、fold 和时间精度样本继续执行。本结果仅覆盖这些区域和采样方法，不能推广到全部 IANA 时区；尚未决定未来紫微发布是否限制区域或采用新的时区 convention。

## 复现

常规构建只需要固定 JDK/Maven，无须 Python；测试读取压缩后的冻结数据。

```bash
scripts/mvn.sh -B -ntp -Dtest=ZiweiCalendarTest test
scripts/mvn.sh -B -ntp clean verify spotless:check
python3 scripts/collect-evidence.py
```

重新导出旧算法的合成历法基准，需要旧项目的精确依赖解释器。生成器验证 M6a 的源码/版本清单，不读取用户记录、不启动应用或修改旧仓库：

```bash
/path/to/FinanceClaw/.venv/bin/python -B scripts/export-ziwei-calendar.py \
  --python-root /path/to/FinanceClaw
```

输出位于 `target/m6/calendar-reference/`。将 `calendar.tsv.gz` 解压后，每行依次为公历 ISO 日期、农历年、月、日、闰月标记，以制表符分隔；`calendar-manifest.json` 保存摘要、来源、非法日期样本及偏移样本。本次两次独立生成的完整历法压缩文件逐字节相同；第二次增加了 8 个已定位月界的正反例。

复现未修改的 Java 库差异，可在下载指定版本后运行 [CalendarProbe](../../scripts/CalendarProbe.java)：

```bash
scripts/mvn.sh -B -ntp dependency:get -Dartifact=cn.6tail:lunar:1.7.7 -Dtransitive=false
# java 必须指向项目固定 JDK；macOS 项目自带路径为 .tools/jdk/Contents/Home/bin/java
java --class-path .cache/m2/cn/6tail/lunar/1.7.7/lunar-1.7.7.jar \
  scripts/CalendarProbe.java src/test/resources/ziwei/calendar/calendar.tsv.gz \
  target/m6/native-calendar-differences.tsv
```

预期打印 `total=72684 differences=60`。该工具验证的是未修改库；应用适配器的验收要求为 **0 个日期差异**。

## 验收与下一步

完整回归 **530 项通过**（425 单元/契约、105 PostgreSQL 集成），0 失败、0 错误、0 跳过，格式检查通过。新增历法验证为 236 项 JUnit 测试，内部对比 72,684 天正向转换与 72,635 天合法出生年份反向转换。机器记录见 [M6b 历法回归](evidence/m6b-results.json)，[打包核对](evidence/m6b-packaging.json)确认依赖/许可证摘要一致，Java jar 不包含测试基准与 Python 脚本。验证包含全部支持日期的正向转换、合法出生年份的反向转换、24 个出生日期正反例、9 个时区、不同年份并发转换、缺失闰月标记、制品/许可证摘要，以及原始库差异审计。

仍待实现：固定规则的星曜、本命盘和五类流运，完整盘面与旧基准逐字段一致；随后再完成出生资料治理、Artifact/投影预算、原生 AgentScope 工具接入和启用验收。当前不能把历法一致称为 M6b 全部完成或紫微可用。M5 的真实服务与生产验收继续单独待办。

本次新增改动尚未提交，未运行真实供应商、远端 MCP/飞书或远程 CI，也未重跑 M5 Compose 交付实验。
