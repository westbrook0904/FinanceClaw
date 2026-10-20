# HTTP 请求类型化与 Bean Validation

实施日期：2026-10-20。前两批原生执行与治理用例拆分已保存为本地检查点 `4f4f7b4`，未推送。本批在该检查点上继续静态请求校验整改，改动尚未提交。

## 接入范围

| 接口 | 请求对象 | 静态必填约束 |
| --- | --- | --- |
| `POST /v1/conversations/{conversation}/turns` | [SubmitTurnRequest](../../src/main/java/com/financeclaw/api/request/SubmitTurnRequest.java) | `message` 必须存在且非 null |
| `PUT /v1/operations/admission` | [ChangeAdmissionRequest](../../src/main/java/com/financeclaw/api/request/ChangeAdmissionRequest.java) | `paused`、`revision` 必须存在且非 null |

使用 Boot BOM 管理的 `spring-boot-starter-validation`，由 Spring MVC 的 `@Valid` 调用 Hibernate Validator。控制器接收类型明确的 record，不再逐字段读取 JsonNode。结构校验失败仍返回 `422 {"error":"invalid_body"}`，不返回被拒绝的原始值或验证器错误详情。

本批解析到的组件版本为 Boot/Validation starter 3.5.16、Hibernate Validator 8.0.3.Final、Jakarta Validation 3.0.2；已有 Spring Web 6.2.19 与 Jackson 2.21.4 沿用原版本。

`@NotNull` 只负责上述必填约束。消息空白和长度、幂等键、修订号、所有权和当前业务状态继续按现有用例顺序校验。若直接把 `@NotBlank`、`@Size` 或 `@Min` 放在控制器入口，会使消息或修订号错误先于幂等键错误，改变现有协议；固定版本 Hibernate Validator 的 `@NotBlank` 也不等价于当前 `String.isBlank()` 的 Unicode 空白语义。

创建会话与取消任务的空对象/可缺体协议保持现状。审批回答、授权、记忆、分页与 SSE 请求不纳入本次迁移；动态工具 Schema 仍使用 AgentScope 的校验机制。

## Jackson 兼容边界

[HttpRequestConfiguration](../../src/main/java/com/financeclaw/bootstrap/HttpRequestConfiguration.java) 通过 Spring `WebMvcConfigurer` 配置既有 Jackson 消息转换器，只为两个请求类型注册专用 mapper，保留 converter 支持的 JSON 媒体类型。配置由 HTTP 角色显式导入，不修改全局 ObjectMapper。

沿用 Jackson `StdDelegatingDeserializer` 先解析为完整 JsonNode，再用严格的 Jackson 映射转换成请求对象。这里没有另写 JSON 解析器、字段校验框架或同形 SPI。这一步保留旧接口的完整解析顺序和重复字段行为；直接流式绑定 record 会提前报告字段错误，而且不支持原有的重复字段最后值覆盖。

| 输入或条件 | 保留的结果 |
| --- | --- |
| 缺体、仅空白、JSON 语法损坏 | `422 invalid_request` |
| JSON `null`、非对象、未知字段、缺失/null 字段 | `422 invalid_body` |
| 数字/布尔转 message，字符串转 paused/revision | 拒绝隐式转换，`422 invalid_body` |
| 浮点或指数形式的 revision，超出 long 范围 | `422 invalid_body` |
| 重复字段 | 最后一个值生效，与旧 JsonNode 相同 |
| 第一份完整 JSON 后的尾随值 | 保持旧处理方式，不在本次收紧 |
| 字段类型错误后又出现 JSON 语法错误 | 先完整解析，仍为 `invalid_request` |
| 同时存在错误的幂等键和空白消息/越界修订号 | `invalid_idempotency_key` 优先 |
| 字符串中的首尾空白、大小写、Unicode | 原样传给用例，不归一化 |

JSON 语法异常保留现有 `ApiErrors` 的映射；树到 DTO 的映射失败归类为 `invalid_body`；缺失/null 必填字段由 Bean Validation 处理。异常分类不依赖 Jackson 的错误消息文本，也不读取第二次 HTTP 请求流。

## 验证入口

```bash
# HTTP 契约和真实 Spring MVC 自动配置，不需要 Docker
scripts/mvn.sh -B -ntp test \
  -Dtest=TaskHttpContractTest,OperationsHttpContractTest,HttpValidationWiringTest

# 全部单元/HTTP/架构测试及产品打包检查，同时编译数据库集成测试
scripts/mvn.sh -B -ntp clean package spotless:check \
  failsafe:integration-test failsafe:verify -Dit.test=RuntimePackagingIT

python3 scripts/check-delivery.py
```

验证已完成：

- 迁移前，原 JsonNode 控制器通过 137 项 HTTP 契约测试；迁移后保持同一批断言与参数源，仅接入生产转换器配置和真实 Validator，再次通过。基线运行时已添加依赖及尚未接线的 DTO/配置类，控制器、异常处理与角色装配仍与 `4f4f7b4` 一致并校验了指纹。
- 新增 5 项真实 Boot MVC 自动配置测试，覆盖 Hibernate Validator、JSON 后缀媒体类型、错误体与未迁移 mapper 隔离；合计 142 项定向 HTTP 测试通过。
- 全部 821 项单元、HTTP、架构测试，以及 1 项产品打包检查通过，零失败、零错误、零跳过。本批新增 136 项测试；上述 142 项已包含在 821 项内。
- 17 项架构门禁通过，生产包图仍无循环；Spotless 与静态交付检查通过。
- 331 个构建输入指纹已留档。上一批保护集中的 109 个文件未改，唯一预期变动是 `pom.xml` 增加 validation starter；用例、领域、Mapper SQL、Flyway 与工具协议未修改。

详细结果、基线与源码指纹见[机器证据](evidence/http-request-validation.json)。测试中的持久化端口和应用依赖使用 mock，不能替代真实 PostgreSQL 回滚、角色完整启动、独立进程恢复或 Compose 验收。Docker Engine 本轮只读检查仍返回 HTTP 500，相关集成测试只完成编译；未重启或修改 Docker。

## 后续工作

当前优先事项仍是 Docker 恢复后的整体验证，命令见[治理用例分层记录](governance-decoupling.md)。后续[记忆接口校验切片](memory-http-validation.md)已将两个既有 typed 请求的静态判断迁到 Bean Validation，保留普通 Jackson 绑定与原错误码；本文的 821 项结果保留为上一批基线。其他接口须逐个固定错误顺序，再决定是否迁移。R7 原生替换实验、真实供应商、飞书租户与生产切换分别保留独立验收范围。
