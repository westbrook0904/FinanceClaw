package com.financeclaw.app.release.port;

/** 凭据解析边界：发布配置只保存引用，真实密钥按调用时需求解析，不进入发布摘要或用户产物。 */
public interface Credentials {
    String modelApiKey();

    String resolve(String reference);
}
