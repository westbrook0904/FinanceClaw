# 记忆接口接入 Bean Validation

实施日期：2026-10-20。承接[任务提交与受理状态的 HTTP 校验切片](http-request-validation.md)，继续复用已引入的 Spring Boot validation starter。当前本地检查点为 `4f4f7b4`，两批 HTTP 校验改动尚未提交，未推送。

## 本批改动

[MemoryController](../../src/main/java/com/financeclaw/api/MemoryController.java) 保留原有类型化请求，删除两个入口的手写静态校验：

| 请求 | 标准约束 | 校验失败的 HTTP 响应 |
| --- | --- | --- |
| `POST /v1/memory/{id}/decision` | `Decision.revision` 使用 `@Min(1)`，`accept` 使用 `@NotNull` | `422 {"error":"invalid_memory_decision"}` |
| `POST /v1/memory/forget` | `Forget.epoch` 使用 `@Min(1)` | `422 {"error":"invalid_memory_epoch"}` |

请求体参数使用 `@Valid`，由 Spring MVC 调用 Hibernate Validator。[ApiErrors](../../src/main/java/com/financeclaw/api/ApiErrors.java) 根据这两种请求类型返回固定错误码；其他请求的 Bean Validation 错误仍为 `invalid_body`。错误响应不包含原值、字段校验消息或解析器详情。

没有增加自定义约束、验证接口或 DTO 工厂，也没有增加依赖。应用服务、领域规则、事务、数据库写入和隐私恢复机制保持原实现。

## 保留的协议

记忆接口的校验顺序为：认证 → JSON 绑定 → 静态参数检查 → 记忆功能开关 → 幂等键 → 事务内的回放、所有权和当前状态核对。因此参数不合法时，仍优先返回对应记忆错误码；参数合法但记忆功能关闭时，仍先返回 `memory_disabled`，再考虑幂等键。

`revision` 和 `epoch` 保留 primitive `long`。缺失/null 字段仍按 Jackson 原有规则变为零，再由 `@Min(1)` 返回记忆错误码。缺请求体、JSON root `null`、语法错误、未知字段或无法绑定的类型仍走 `invalid_request`。

这两个接口之前已经采用普通 Jackson record 绑定。本批保留原字符串/数字/布尔转换、浮点转整数、重复字段和尾随内容处理方式，没有把它们加入任务/受理接口的严格 mapper。静态校验迁移与输入协议收紧应分开处理。

`GET /v1/memory`、隐私查询、搜索和重建入口保持原样。授权与审批接口也不前移校验：它们的 scopes、revision 和 reason 校验位于幂等键、所有权核对之后，直接增加入口约束会改变现有错误优先级。

## 验证入口

```bash
# HTTP 契约和真实 Boot MVC 校验装配，无需 Docker
scripts/mvn.sh -B -ntp test \
  -Dtest=MemoryHttpContractTest,HttpValidationWiringTest

# 全部单元/HTTP/架构测试及产品打包检查，同时编译数据库集成测试
scripts/mvn.sh -B -ntp clean package spotless:check \
  failsafe:integration-test failsafe:verify -Dit.test=RuntimePackagingIT

python3 scripts/check-delivery.py
```

验证结果：

- 改动前，82 项记忆 HTTP 契约测试在原控制器上通过；改动后，同一组输入与断言再次通过。迁移前后的测试源码只经过四处格式调整，原控制器与测试文件指纹已留档。
- 真实 Boot MVC 装配新增 3 项验证，确认静态约束触发 `MethodArgumentNotValidException`，两个固定记忆错误码、普通 Jackson 转换和 JSON 后缀媒体类型保持原样。
- 包含既有任务与受理接口的 227 项定向 HTTP 测试通过。
- 全部 906 项单元、HTTP、架构测试，以及 1 项产品打包检查通过，零失败、零错误、零跳过。本批比上一批增加 85 项测试；上述定向测试已包含在全量计数内。
- 17 项架构门禁通过，生产包图无循环；Spotless 和静态交付检查通过。332 个构建输入指纹已留档，上一批工作树中的 110 个受保护文件（含已有 validation starter 的 POM）保持不变。

验证范围与证据见[机器记录](evidence/memory-http-validation.json)。HTTP 测试中的数据库端口或应用服务使用 mock，不能替代真实 PostgreSQL 事务、隐私竞态、角色完整启动与 Compose 恢复验收。Docker Engine 本轮只读检查仍返回 HTTP 500，数据库相关测试只完成编译，未重启或修改 Docker。

后续优先补跑环境恢复后的完整集成验证，命令见[治理用例分层记录](governance-decoupling.md)。本批与上一批 HTTP 校验改动仍保留在工作区，历史测试证据不作覆盖。
