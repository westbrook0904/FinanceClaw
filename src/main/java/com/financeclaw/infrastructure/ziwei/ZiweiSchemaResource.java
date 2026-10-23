package com.financeclaw.infrastructure.ziwei;

import com.financeclaw.domain.release.ZiweiPolicy;
import com.financeclaw.support.*;
import com.financeclaw.ziwei.*;
import java.io.IOException;
import java.nio.charset.StandardCharsets;
import java.util.Map;
import org.springframework.core.io.ClassPathResource;

/** 紫微工具 schema 从受控 classpath 资源加载，计算内核无需依赖文件系统。 */
public final class ZiweiSchemaResource {
    private static final String RESOURCE = load();
    private static final Map<String, Object> DOCUMENT =
            ZiweiValues.immutable(PayloadJson.object(RESOURCE));

    private ZiweiSchemaResource() {}

    private static String load() {
        try (var input = new ClassPathResource("ziwei/inputs.json").getInputStream()) {
            String resource = new String(input.readAllBytes(), StandardCharsets.UTF_8);
            if (!ZiweiPolicy.INPUT_SCHEMA_HASH.equals(PayloadDigest.sha256(resource))) {
                throw new IllegalStateException(
                        "Ziwei input schema differs from the fixed release contract");
            }
            return resource;
        } catch (IOException e) {
            throw new IllegalStateException("Ziwei input schema unavailable", e);
        }
    }

    public static String hash() {
        return PayloadDigest.sha256(RESOURCE);
    }

    public static Map<String, Object> document() {
        return DOCUMENT;
    }
}
