package com.financeclaw.app.memory.port;

/** 嵌入配置注册边界：固定 revision 对应唯一配置指纹，防止同一索引版本静默切换模型。 */
public interface EmbeddingProfiles {
    void registerProfile(String revision, String fingerprint, String model);

    String profile(String revision);
}
