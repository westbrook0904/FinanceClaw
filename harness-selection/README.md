# harness-selection

对 Registry 返回的 Capability Provider 候选执行 Eligibility、Health 与确定性 Priority
选择，并产生可解释的 `SelectionDecision`。本模块不调用 Provider，不管理模型，也不负责图
调度或 checkpoint。
