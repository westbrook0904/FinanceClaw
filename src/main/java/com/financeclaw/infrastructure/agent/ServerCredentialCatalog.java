package com.financeclaw.infrastructure.agent;

import com.fasterxml.jackson.core.JsonParser;
import com.fasterxml.jackson.databind.DeserializationFeature;
import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.json.JsonMapper;
import com.financeclaw.domain.Task.Owner;
import com.financeclaw.domain.authorization.CredentialResource;
import com.financeclaw.domain.authorization.ResourceIdentity;
import java.util.*;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.core.env.Environment;
import org.springframework.stereotype.Component;

/** 只接受服务端配置的凭据引用；管理 API 不接受环境变量名或凭据值。 */
@Component
public final class ServerCredentialCatalog {
    public static final String MODEL = "@model";
    private final Map<String, CredentialResource> byId;
    private final Map<String, CredentialResource> byReference;
    private final Owner legacyOwner;
    private final boolean legacy;

    @Autowired
    public ServerCredentialCatalog(Environment environment) {
        this(
                environment.getProperty("financeclaw.auth.credential-resources-json", "[]"),
                new Owner(
                        environment.getProperty("financeclaw.auth.tenant", "local"),
                        environment.getProperty("financeclaw.auth.subject", "developer")),
                environment.getProperty("financeclaw.auth.identities-json", "[]"));
    }

    public ServerCredentialCatalog(String configured, Owner legacyOwner, String identities) {
        try {
            var parser =
                    JsonMapper.builder()
                            .enable(JsonParser.Feature.STRICT_DUPLICATE_DETECTION)
                            .enable(DeserializationFeature.FAIL_ON_TRAILING_TOKENS)
                            .build();
            if (configured.length() > 131072) throw new IllegalArgumentException();
            var values = parser.readTree(configured);
            var principals = parser.readTree(identities);
            if (!values.isArray() || values.size() > 256 || !principals.isArray())
                throw new IllegalArgumentException();
            var ids = new LinkedHashMap<String, CredentialResource>();
            var references = new HashMap<String, CredentialResource>();
            for (JsonNode value : values) {
                if (!value.isObject()
                        || value.size() != 4
                        || !value.has("resourceId")
                        || !value.has("reference")
                        || !value.has("tenant")
                        || !value.has("subject")) throw new IllegalArgumentException();
                String id = text(value.get("resourceId")), ref = text(value.get("reference"));
                String tenant = text(value.get("tenant")), subject = text(value.get("subject"));
                if (!id.matches("[A-Za-z][A-Za-z0-9_.-]{0,99}")
                        || !(MODEL.equals(ref) || ref.matches("[A-Z][A-Z0-9_]{0,100}"))
                        || !ResourceIdentity.valid(tenant)
                        || !ResourceIdentity.valid(subject)) throw new IllegalArgumentException();
                var resource = new CredentialResource(id, ref, new Owner(tenant, subject));
                if (ids.putIfAbsent(id, resource) != null
                        || references.putIfAbsent(ref, resource) != null)
                    throw new IllegalArgumentException();
            }
            byId = Collections.unmodifiableMap(ids);
            byReference = Map.copyOf(references);
            this.legacyOwner = Objects.requireNonNull(legacyOwner);
            legacy = values.isEmpty() && principals.isEmpty();
        } catch (Exception invalid) {
            throw new IllegalArgumentException("Invalid credential resource catalog");
        }
    }

    private static String text(JsonNode node) {
        if (node == null || !node.isTextual()) throw new IllegalArgumentException();
        return node.textValue();
    }

    public CredentialResource resource(String id) {
        return byId.get(id);
    }

    public CredentialResource reference(String ref) {
        return byReference.get(ref);
    }

    public List<CredentialResource> owned(Owner owner) {
        return byId.values().stream().filter(r -> r.owner().equals(owner)).toList();
    }

    public boolean permitsLegacy(Owner owner) {
        return legacy && legacyOwner.equals(owner);
    }
}
