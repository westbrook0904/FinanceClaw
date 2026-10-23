package com.financeclaw.channel;

import org.springframework.core.env.Environment;
import org.springframework.stereotype.Component;

/**
 * 飞书配置边界：只在启用时读取必要凭据，并校验客户端参数。
 *
 * <p>业务组件共享该不可变配置，避免各自读取环境造成同一进程中的策略不一致。
 */
@Component
public class FeishuSettings {
    public final boolean enabled;
    public final String appId, appSecret, token, encryptKey, tenantKey, baseUrl;

    public FeishuSettings(Environment e) {
        enabled = e.getProperty("financeclaw.feishu.enabled", Boolean.class, false);
        appId = e.getProperty("financeclaw.feishu.app-id", "");
        appSecret = e.getProperty("financeclaw.feishu.app-secret", "");
        token = e.getProperty("financeclaw.feishu.verification-token", "");
        encryptKey = e.getProperty("financeclaw.feishu.encrypt-key", "");
        tenantKey = e.getProperty("financeclaw.feishu.tenant-key", "");
        baseUrl = e.getProperty("financeclaw.feishu.base-url", "https://open.feishu.cn");
        if (enabled
                && (appId.isBlank()
                        || appSecret.isBlank()
                        || token.isBlank()
                        || encryptKey.isBlank()
                        || tenantKey.isBlank())) {
            throw new IllegalArgumentException("Incomplete Feishu configuration");
        }
        if (enabled) {
            var uri = java.net.URI.create(baseUrl);
            if (!"https".equals(uri.getScheme())
                    && !("http".equals(uri.getScheme())
                            && java.util.Set.of("127.0.0.1", "localhost", "[::1]")
                                    .contains(uri.getHost()))) {
                throw new IllegalArgumentException("Feishu requires HTTPS");
            }
        }
    }
}
