package com.financeclaw.app.memory.port;

import com.financeclaw.domain.Memory.Extraction;

/** 证据提取模型边界：提取结果仅是待校验建议，内容和证据引文必须能够从原始用户消息核实。 */
@FunctionalInterface
public interface MemoryExtraction {
    Extraction extract(String source);
}
