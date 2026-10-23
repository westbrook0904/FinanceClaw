package com.financeclaw.infrastructure.memory;

import com.financeclaw.app.memory.port.EmbeddingProfiles;
import org.springframework.stereotype.Component;

/**
 * 实现 EmbeddingProfiles，登记 embedding revision 对应的模型指纹并按版本读取。 插入采用存在即保留的语义，由模型适配器核对既有指纹，避免相同 revision
 * 被悄悄换成另一模型。这里的 profile 是模型配置，不是用户画像。
 */
@Component
public class MyBatisEmbeddingProfiles implements EmbeddingProfiles {
    private final MemoryMapper db;

    public MyBatisEmbeddingProfiles(MemoryMapper db) {
        this.db = db;
    }

    @Override
    public void registerProfile(String revision, String fingerprint, String model) {
        db.insertEmbeddingProfileIfAbsent(revision, fingerprint, model);
    }

    @Override
    public String profile(String revision) {
        return db.getEmbeddingProfileFingerprint(revision);
    }
}
