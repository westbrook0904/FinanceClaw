# harness-registry

维护 Capability 与 Provider 的 1:N 注册关系。Registry 保存可信 Provider 实例和 descriptor，
只读 `CapabilityCatalog` 只暴露 Capability 元数据，供 Agent 工具投影与已发布 Workflow 使用。

Registry 不注册 LLM 厂商模型；模型配置和 provider integration 由未来的 LangChain
`ModelRuntime` 管理。Registry 也不做选择，Eligibility/Health/Priority 由
`harness-selection` 负责。
