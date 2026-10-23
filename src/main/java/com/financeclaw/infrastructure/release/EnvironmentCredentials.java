package com.financeclaw.infrastructure.release;

import com.financeclaw.app.release.port.Credentials;
import com.financeclaw.domain.Governance.Blocked;
import org.springframework.core.env.Environment;
import org.springframework.stereotype.Component;

/**
 * 实现 Credentials，解析 Spring 环境中的模型密钥及受控凭据引用。
 * 只允许规定格式的环境键名，缺少所需凭据时返回固定阻断原因；发布配置与业务结果仅携带引用，不在此记录密钥。静态入口供独立配置加载流程使用。
 */
@Component
public class EnvironmentCredentials implements Credentials {
    private final Environment environment;
    private final String modelApiKey;

    public EnvironmentCredentials(Environment environment) {
        this.environment = environment;
        this.modelApiKey = environment.getProperty("financeclaw.model.api-key", "");
    }

    @Override
    public String modelApiKey() {
        return modelApiKey;
    }

    @Override
    public String resolve(String reference) {
        return lookup(reference, environment::getProperty);
    }

    public static String lookup(String ref) {
        return lookup(ref, System::getenv);
    }

    public static String lookup(String ref, java.util.function.Function<String, String> lookup) {
        if (ref.isBlank()) {
            return "";
        }
        if (!ref.matches("[A-Z][A-Z0-9_]{0,100}")) {
            throw new Blocked("invalid_credential_reference");
        }
        String value = lookup.apply(ref);
        if (value == null || value.isBlank()) {
            throw new Blocked("mcp_credentials_unavailable");
        }
        return value;
    }
}
