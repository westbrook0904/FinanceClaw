package com.financeclaw.app.release.port;

import com.financeclaw.domain.release.ReleaseDefinition;

/** 启动发布配置来源；只提供待登记的初始定义，数据库中的持久指针始终决定当前版本。 */
public interface ReleaseSource {
    ReleaseDefinition initial();
}
