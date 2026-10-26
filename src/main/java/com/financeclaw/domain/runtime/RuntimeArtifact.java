package com.financeclaw.domain.runtime;

import java.util.List;

/** 文件摘要与运行类摘要分开保存；前者保护完整归档，后者供冻结发布的兼容检查。 */
public record RuntimeArtifact(
        String artifactHash,
        String runtimeHash,
        String relativePath,
        long bytes,
        int protocolVersion,
        List<String> capabilities) {
    public RuntimeArtifact {
        capabilities = List.copyOf(capabilities);
    }
}
