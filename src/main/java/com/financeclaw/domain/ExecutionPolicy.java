package com.financeclaw.domain;

import java.util.List;

/** 执行权限及预算快照：调用范围和上限来自固定发布配置，不由模型输出决定。 */
public record ExecutionPolicy(List<String> scopes, int maxModelCalls, int maxToolCalls) {
    public ExecutionPolicy {
        scopes = List.copyOf(scopes);
    }
}
