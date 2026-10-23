package com.financeclaw.app.memory.port;

import com.financeclaw.domain.memory.EmbeddingVector;

/** 记忆嵌入模型边界：只接收经治理的证据文本，模型网络调用必须在主库事务外执行。 */
@FunctionalInterface
public interface MemoryEmbedding {
    EmbeddingVector embed(String text);
}
