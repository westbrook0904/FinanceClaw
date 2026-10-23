# HTTP 请求校验

所有 Jackson HTTP 转换器使用[统一严格配置](../../src/main/java/com/financeclaw/bootstrap/HttpRequestConfiguration.java)：拒绝未知字段、字符串到数字/布尔值的隐式转换、小数到整数截断，以及数字/布尔值到消息正文的自动转换。拒绝重复键及多个尾随 JSON 值，不再维护部分 DTO 专用的二次树转换链。

请求先解析并按声明类型绑定，再由 Bean Validation 检查必填项和范围。复杂业务前置条件、幂等键、所有权、修订号和当前状态继续由应用服务核验。手工 Markdown 画像不经过 JSON，使用严格 UTF-8、有界读取和主体绑定的 If-Match。

| 错误类别 | HTTP / 错误码 |
| --- | --- |
| JSON 语法损坏、缺少正文或路径参数类型不合法 | `422 invalid_request` |
| JSON 值不符合声明类型、未知字段、普通 DTO 必填校验失败 | `422 invalid_body` |
| 记忆确认/遗忘的字段约束失败 | 对应 `invalid_memory_decision` / `invalid_memory_epoch` |
| 应用业务约束 | 应用服务声明的稳定错误码 |

错误响应不回显原正文、SQL、解析器详情或凭据。重复字段、多个同时错误的优先顺序不作为旧接口兼容承诺；当前绑定规则由真实 MVC 契约测试验证。
