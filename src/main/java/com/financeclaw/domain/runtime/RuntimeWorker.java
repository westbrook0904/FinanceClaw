package com.financeclaw.domain.runtime;

import java.util.List;

/** Registry 是存活执行资源清单；live 由数据库当前时钟计算，不是执行授权。 */
public record RuntimeWorker(
        String workerId,
        int protocolVersion,
        String runtimeHash,
        List<String> capabilities,
        int parallelism,
        int activeAttempts,
        String state,
        boolean live) {
    public RuntimeWorker {
        capabilities = List.copyOf(capabilities);
    }
}
