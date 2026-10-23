# 历史设计与验证记录

此目录保留过去批次的方案、固定样本说明、源码指纹与验证输出。日期、测试数量、提交及未完成项只描述当时状态；文中的旧运行角色、迁移版本、外部基准生成方式不构成当前独立应用的使用步骤。

当前说明从[运行与恢复](../operations.md)、[新库结构](../architecture/fresh-schema.md)和[用户画像与跨会话证据](../architecture/user-profile-memory.md)开始。当前主库只初始化一份 V1 结构；不执行旧应用数据回填，也不提供历史版本升级链。

- [R0–R6 整改方案](architecture/agentscope-cola-remediation.md)：当时的拆分依据和分批验证。
- [R6 角色装配记录](architecture/r6-implementation.md)：历史拆分部署的隔离验证；当前已统一业务进程。
- [原生执行依赖收缩](architecture/runtime-decoupling.md)、[治理用例拆分](architecture/governance-decoupling.md)：保留用例与适配器边界的演进过程。
- [职责拆分后的完整验证](architecture/post-refactor-verification.md)：只覆盖记录所列构建输入。
- [独立遗忘账本](m5f-forget-ledger.md)、[备份隐私核验](m5e-restore-privacy.md)：保留恢复设计与故障实验，执行命令以当前运行说明为准。

历史 Markdown 的源码链接指向当前仍保留的入口；链接可用不表示该入口与历史提交逐字一致。精确历史信息应结合文中提交号和原证据文件判断。证据 JSON 保留原始内容，不为配合当前目录或实现重写历史结果。
