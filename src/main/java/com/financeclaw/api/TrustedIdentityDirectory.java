package com.financeclaw.api;

import com.fasterxml.jackson.core.JsonParser;
import com.fasterxml.jackson.databind.DeserializationFeature;
import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.json.JsonMapper;
import com.financeclaw.domain.Task.Owner;
import com.financeclaw.domain.authorization.ResourceIdentity;
import java.nio.charset.StandardCharsets;
import java.security.MessageDigest;
import java.util.*;
import org.springframework.core.env.Environment;

/** 服务端配置的有界认证目录；只保存token摘要，不接受请求指定owner或角色。 */
public final class TrustedIdentityDirectory {
    public record Principal(Owner owner, Set<String> permissions) {
        public Principal {
            permissions = Set.copyOf(permissions);
        }
    }

    private record Entry(byte[] tokenHash, Principal principal) {}

    private static final Map<String, Set<String>> ROLES =
            Map.of(
                    "agent_manager", Set.of("agent:manage"),
                    "agent_sharer", Set.of("agent:share"),
                    "credential_manager", Set.of("credentials:manage"),
                    "scheduler_manager", Set.of("schedules:manage"),
                    "runtime_manager", Set.of("runtime:manage"));
    private final List<Entry> entries;

    public TrustedIdentityDirectory(Environment environment) {
        this(
                environment.getProperty("financeclaw.auth.token", ""),
                environment.getProperty("financeclaw.auth.tenant", "local"),
                environment.getProperty("financeclaw.auth.subject", "developer"),
                environment.getProperty("financeclaw.auth.identities-json", "[]"));
    }

    public TrustedIdentityDirectory(
            String legacyToken, String tenant, String subject, String configured) {
        try {
            var parser =
                    JsonMapper.builder()
                            .enable(JsonParser.Feature.STRICT_DUPLICATE_DETECTION)
                            .enable(DeserializationFeature.FAIL_ON_TRAILING_TOKENS)
                            .build();
            if (configured.length() > 131072) throw new IllegalArgumentException();
            JsonNode values = parser.readTree(configured);
            if (!values.isArray() || values.size() > 128) throw new IllegalArgumentException();
            var result = new ArrayList<Entry>();
            if (legacyToken != null && !legacyToken.isBlank())
                result.add(entry(legacyToken, tenant, subject, Set.of()));
            for (JsonNode value : values) {
                if (!value.isObject()
                        || value.size() != 4
                        || !value.has("token")
                        || !value.has("tenant")
                        || !value.has("subject")
                        || !value.has("roles")
                        || !value.path("roles").isArray()) throw new IllegalArgumentException();
                var permissions = new HashSet<String>();
                var roles = new HashSet<String>();
                for (JsonNode role : value.get("roles")) {
                    String name = text(role);
                    if (!roles.add(name) || !ROLES.containsKey(name))
                        throw new IllegalArgumentException();
                    permissions.addAll(ROLES.get(name));
                }
                result.add(
                        entry(
                                text(value.get("token")),
                                text(value.get("tenant")),
                                text(value.get("subject")),
                                permissions));
            }
            for (int i = 0; i < result.size(); i++)
                for (int j = 0; j < i; j++)
                    if (MessageDigest.isEqual(result.get(i).tokenHash(), result.get(j).tokenHash()))
                        throw new IllegalArgumentException();
            entries = List.copyOf(result);
        } catch (Exception invalid) {
            // 不能把包含token的配置内容或解析异常附加到日志。
            throw new IllegalArgumentException("Invalid trusted identity directory");
        }
    }

    private static String text(JsonNode node) {
        if (node == null || !node.isTextual()) throw new IllegalArgumentException();
        return node.textValue();
    }

    private static Entry entry(
            String token, String tenant, String subject, Set<String> permissions) {
        if (token == null
                || token.isBlank()
                || token.length() > 4096
                || token.chars().anyMatch(Character::isWhitespace)
                || !validIdentity(tenant)
                || !validIdentity(subject)) throw new IllegalArgumentException();
        return new Entry(digest(token), new Principal(new Owner(tenant, subject), permissions));
    }

    public static boolean validIdentity(String value) {
        return ResourceIdentity.valid(value);
    }

    private static byte[] digest(String token) {
        try {
            return MessageDigest.getInstance("SHA-256")
                    .digest(token.getBytes(StandardCharsets.UTF_8));
        } catch (java.security.NoSuchAlgorithmException impossible) {
            throw new IllegalStateException(impossible);
        }
    }

    public Principal authenticate(String token) {
        if (token == null || token.isBlank() || token.length() > 4096) return null;
        byte[] candidate = digest(token);
        Principal found = null;
        for (var entry : entries)
            if (MessageDigest.isEqual(candidate, entry.tokenHash())) found = entry.principal();
        return found;
    }
}
