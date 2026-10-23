package com.financeclaw.infrastructure.release;

import com.financeclaw.domain.release.BusinessFeatures;
import com.financeclaw.domain.release.RemoteEndpoint;
import com.financeclaw.domain.release.ZiweiPolicy;
import com.financeclaw.infrastructure.agentscope.skills.PackagedSkills;
import java.util.*;
import org.springframework.core.env.Environment;

/** 把环境配置转换为发布中的远程端点、固定市场数据和紫微策略。 未启用的集成不解析地址或凭据；启用紫微时先验证开发模式、时区版本与密钥。这里只装配配置值，不调用模型或远程业务工具。 */
public final class BusinessConfigurationLoader {
    private BusinessConfigurationLoader() {}

    public static BusinessFeatures defaults() {
        return new BusinessFeatures(List.of(), PackagedSkills.hash(), "{}", null);
    }

    public static BusinessFeatures load(Environment env) {
        ZiweiPolicy ziwei = null;
        if (env.getProperty("financeclaw.business.ziwei.enabled", Boolean.class, false)) {
            if (!env.getProperty("financeclaw.business.ziwei.development", Boolean.class, false)) {
                throw new IllegalArgumentException("Ziwei requires explicit development mode");
            }
            ziwei =
                    ZiweiPolicy.development(
                            env.getRequiredProperty("financeclaw.business.ziwei.key-ref"),
                            env.getRequiredProperty("financeclaw.business.ziwei.key-version"),
                            env.getRequiredProperty("financeclaw.business.ziwei.query-timezone"));
            ziwei.verifyRuntime();
            byte[] key =
                    ZiweiPolicy.key(
                            EnvironmentCredentials.lookup(ziwei.keyRef(), env::getProperty));
            Arrays.fill(key, (byte) 0);
        }
        List<RemoteEndpoint> remotes = new ArrayList<>();
        for (String name : List.of("hotel", "taibu")) {
            String key = "financeclaw.business." + name;
            // Disabled integrations do not even resolve their credential or endpoint settings.
            if (!env.getProperty(key + ".enabled", Boolean.class, false)) {
                continue;
            }
            remotes.add(
                    new RemoteEndpoint(
                            name,
                            env.getRequiredProperty(key + ".url"),
                            split(env.getRequiredProperty(key + ".allowed-hosts")),
                            env.getProperty(key + ".credential-ref", ""),
                            env.getProperty(key + ".external", Boolean.class, true),
                            split(env.getProperty(key + ".tenants", "")),
                            name.equals("hotel")
                                    ? List.of(
                                            "searchHotels", "getHotelDetail", "getHotelSearchTags")
                                    : List.of("almanac", "bazi"),
                            env.getProperty(key + ".timeout-ms", Integer.class, 5000)));
        }
        String market = env.getProperty("financeclaw.business.market-file", "");
        return new BusinessFeatures(
                remotes,
                PackagedSkills.hash(),
                market.isBlank() ? "{}" : BusinessResources.read(market),
                ziwei);
    }

    private static List<String> split(String text) {
        return Arrays.stream(text.split(",")).map(String::trim).filter(s -> !s.isBlank()).toList();
    }
}
