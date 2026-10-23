# Java 代码规范与逻辑注释

本项目参考[阿里巴巴 Java 开发手册](https://alibaba.github.io/Alibaba-Java-Coding-Guidelines/)，结合 Java 21 record、虚拟线程和 Spring Boot 的实际使用方式落地。手册中的强制、推荐与参考规则有不同级别，项目约定不冒称手册原文。

- 命名表达职责：Mapper 单对象查询用 `get`，集合用 `list`，数量用 `count`，写入用 `insert/update/delete`。布尔探针采用项目约定的 `is/exists`；锁、CAS 和去重条件用明确后缀说明。
- 控制流始终带大括号，真实重写方法标记 `@Override`，常量使用完整含义的名称；异常保持稳定业务语义，不把远程失败误当作空结果或成功。
- 使用四空格缩进，由 Spotless 的 AOSP 格式统一执行，同时清理无用 import。集合不可变性、线程池边界和外部资源关闭按实际生命周期处理。
- 类和接口注释说明职责及边界，核心方法说明前置条件、返回语义与失败处理；复杂流程分阶段解释为什么这样排序。
- 锁、事务和恢复代码重点解释锁顺序、版本校验、受影响行数、提交后通知及远程调用所在的事务边界。Mapper 注释明确 `FOR SHARE/UPDATE/SKIP LOCKED`、CAS、UPSERT 和数据库时间语义。
- 注释不能代替清晰命名，也不逐行复述赋值。权限、摘要、隐私和投递说明必须与实际行为一致；不把本地模拟验证写成真实外部服务已验收。

本轮整理覆盖任务受理、执行/恢复、交互/授权、结果发布、画像/证据记忆、受限 Worker、AgentScope 适配、六个 Mapper、HTTP/SSE、飞书渠道和独立初始化/隐私恢复入口。新的类应沿用相同的职责与边界说明。

## 验证

```bash
scripts/mvn.sh -B -ntp spotless:check
scripts/mvn.sh -B -ntp clean verify
python3 scripts/check-delivery.py
```

初始化结构、报告原子性、owner 共享/独占锁、租约、隐私恢复和统一进程装配使用真实 PostgreSQL 测试；纯格式调整不新增镜像实现的测试。
