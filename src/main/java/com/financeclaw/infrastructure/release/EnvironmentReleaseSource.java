package com.financeclaw.infrastructure.release;

import com.financeclaw.app.release.port.ReleaseSource;
import com.financeclaw.domain.release.*;
import com.financeclaw.domain.release.ModelProfile;
import com.financeclaw.domain.release.ReleaseDefinition;
import org.springframework.core.env.Environment;
import org.springframework.stereotype.Component;

/**
 * 实现 ReleaseSource，生成初始 Agent、主备模型预算及业务配置的发布定义。 环境只在构造定义时读取，后续 Turn 使用 ReleaseService
 * 固定的发布身份，不能在执行中随意切换到新配置。未配置供应商时使用明确的离线模型。
 */
@Component
public class EnvironmentReleaseSource implements ReleaseSource {
    private final Environment env;

    public EnvironmentReleaseSource(Environment env) {
        this.env = env;
    }

    @Override
    public ReleaseDefinition initial() {
        var primary = profile(env, "financeclaw.model", "offline");
        String fallback = env.getProperty("financeclaw.fallback.name", "");
        return new ReleaseDefinition(
                "initial-v1",
                "finance_agent",
                ReleaseDefinition.SCOPES,
                primary,
                fallback.isBlank() ? null : profile(env, "financeclaw.fallback", fallback),
                12,
                32,
                BusinessConfigurationLoader.load(env));
    }

    private static ModelProfile profile(
            org.springframework.core.env.Environment e, String prefix, String defaultName) {
        return new ModelProfile(
                e.getProperty(prefix + ".name", defaultName),
                e.getProperty(prefix + ".base-url", ""),
                e.getProperty(prefix + ".input-limit", Integer.class, 65536),
                e.getProperty(prefix + ".context-window", Integer.class, 131072),
                e.getProperty(prefix + ".output-reserve", Integer.class, 2048),
                e.getProperty(prefix + ".safety-margin", Integer.class, 512),
                e.getProperty(prefix + ".thinking", Boolean.class, false));
    }
}
