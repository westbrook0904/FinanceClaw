package com.financeclaw.app.release.port;

import com.financeclaw.domain.release.ReleaseDefinition;

/** 发布配置来源边界：提供当前发布指针和按固定身份读取的不可变定义。 */
public interface ReleaseSource {
    ReleaseDefinition initial();
}
