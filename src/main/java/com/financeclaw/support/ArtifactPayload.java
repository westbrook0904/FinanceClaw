package com.financeclaw.support;

import com.fasterxml.jackson.databind.ObjectMapper;
import com.financeclaw.domain.Governance.Artifact;
import com.financeclaw.domain.Governance.Blocked;
import com.financeclaw.domain.Governance.Page;
import com.financeclaw.domain.Task.Problem;
import java.nio.charset.StandardCharsets;
import java.util.List;

/**
 * 归档正文的编码、摘要和有界分页规则。
 *
 * <p>对象和数组分别按稳定路径与偏移读取，返回内容受 UTF-8 字节预算限制；正文摘要与持久元数据一起核验。
 */
public record ArtifactPayload(String json, int bytes) {
    private static final ObjectMapper JSON = new ObjectMapper();
    private static final int MAX_STORED_BYTES = 2097152;
    private static final int MAX_READ_BYTES = 16384;

    /** text 是归档 JSON 的一段 UTF-8 文本，可能不是独立 JSON；下一次必须使用 nextStart。 */
    public record Range(
            String artifactId,
            String hash,
            int totalBytes,
            int start,
            int count,
            int nextStart,
            boolean complete,
            String encoding,
            String text) {}

    /** 已校验的完整 JSON 文件，不携带可由调用者控制的服务器路径。 */
    public record Document(String artifactId, String hash, byte[] content) {
        public Document {
            content = content.clone();
        }

        @Override
        public byte[] content() {
            return content.clone();
        }
    }

    public static ArtifactPayload encode(Object payload) {
        String json = PayloadJson.canonical(payload);
        int bytes = utf8Bytes(json);
        if (bytes > MAX_STORED_BYTES) {
            throw new Blocked("artifact_too_large");
        }
        return new ArtifactPayload(json, bytes);
    }

    public String hash() {
        return PayloadDigest.sha256(json);
    }

    public static Page read(Artifact artifact, int start, int limit, String path) {
        verifiedBytes(artifact);
        Object data = PayloadJson.parse(artifact.payload());
        if (path == null || path.length() > 256 || (!path.isEmpty() && !path.startsWith("/"))) {
            throw new Problem(422, "invalid_artifact_path");
        }
        if (!path.isEmpty()) {
            try {
                var selected = JSON.valueToTree(data).at(path);
                if (selected.isMissingNode()) {
                    throw new Problem(422, "artifact_path_missing");
                }
                data = JSON.convertValue(selected, Object.class);
            } catch (IllegalArgumentException error) {
                throw new Problem(422, "invalid_artifact_path");
            }
        }
        if (data instanceof List<?> values) {
            if (start < 0 || limit < 1 || limit > 100) {
                throw new Problem(422, "invalid_pagination");
            }
            int offset = Math.min(start, values.size()),
                    end = Math.min(offset + limit, values.size());
            while (end > offset
                    && utf8Bytes(PayloadJson.canonical(values.subList(offset, end)))
                            > MAX_READ_BYTES) {
                end--;
            }
            if (end == offset && offset < values.size()) {
                throw new Problem(422, "artifact_item_too_large");
            }
            return new Page(
                    artifact.id(),
                    artifact.hash(),
                    values.size(),
                    offset,
                    end - offset,
                    values.subList(offset, end));
        }
        if (utf8Bytes(PayloadJson.canonical(data)) > MAX_READ_BYTES) {
            throw new Problem(422, "artifact_object_too_large");
        }
        return new Page(artifact.id(), artifact.hash(), 1, 0, 1, data);
    }

    /** 直接按归档文件字节读取，绕过对象/数组分页的单项大小限制。 起点必须位于 UTF-8 字符边界，终点向前缩至完整字符；无进展时明确报错，禁止用替代字符损坏正文。 */
    public static Range readRange(Artifact artifact, int start, int maxBytes) {
        byte[] bytes = verifiedBytes(artifact);
        if (start < 0 || start > bytes.length || maxBytes < 1 || maxBytes > MAX_READ_BYTES) {
            throw new Problem(422, "invalid_artifact_range");
        }
        if (start < bytes.length && continuation(bytes[start])) {
            throw new Problem(422, "invalid_artifact_range");
        }
        int end = Math.min(start + maxBytes, bytes.length);
        while (end < bytes.length && end > start && continuation(bytes[end])) {
            end--;
        }
        if (end == start && start < bytes.length) {
            throw new Problem(422, "artifact_range_too_small");
        }
        return new Range(
                artifact.id(),
                artifact.hash(),
                bytes.length,
                start,
                end - start,
                end,
                end == bytes.length,
                "utf-8",
                new String(bytes, start, end - start, StandardCharsets.UTF_8));
    }

    public static Document document(Artifact artifact) {
        return new Document(artifact.id(), artifact.hash(), verifiedBytes(artifact));
    }

    private static byte[] verifiedBytes(Artifact artifact) {
        byte[] bytes = artifact.payload().getBytes(StandardCharsets.UTF_8);
        if (artifact.bytes() != bytes.length
                || !artifact.hash().equals(PayloadDigest.sha256(artifact.payload()))) {
            throw new Problem(409, "artifact_corrupt");
        }
        return bytes;
    }

    private static boolean continuation(byte value) {
        return (value & 0xc0) == 0x80;
    }

    private static int utf8Bytes(String value) {
        return value.getBytes(StandardCharsets.UTF_8).length;
    }
}
