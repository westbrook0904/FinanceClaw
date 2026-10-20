package com.financeclaw.support;

import com.fasterxml.jackson.databind.ObjectMapper;
import com.financeclaw.domain.Governance.Artifact;
import com.financeclaw.domain.Governance.Blocked;
import com.financeclaw.domain.Governance.Page;
import com.financeclaw.domain.Task.Problem;
import java.nio.charset.StandardCharsets;
import java.util.List;

/**
 * Artifact storage bytes and bounded JSON Pointer reads; unrelated payloads use their own rules.
 */
public record ArtifactPayload(String json, int bytes) {
  private static final ObjectMapper JSON = new ObjectMapper();
  private static final int MAX_STORED_BYTES = 2097152;
  private static final int MAX_READ_BYTES = 16384;

  public static ArtifactPayload encode(Object payload) {
    String json = PayloadJson.canonical(payload);
    int bytes = utf8Bytes(json);
    if (bytes > MAX_STORED_BYTES) throw new Blocked("artifact_too_large");
    return new ArtifactPayload(json, bytes);
  }

  public String hash() {
    return PayloadDigest.sha256(json);
  }

  public static Page read(Artifact artifact, int start, int limit, String path) {
    if (!artifact.hash().equals(PayloadDigest.sha256(artifact.payload())))
      throw new Problem(409, "artifact_corrupt");
    Object data = PayloadJson.parse(artifact.payload());
    if (path == null || path.length() > 256 || (!path.isEmpty() && !path.startsWith("/")))
      throw new Problem(422, "invalid_artifact_path");
    if (!path.isEmpty()) {
      try {
        var selected = JSON.valueToTree(data).at(path);
        if (selected.isMissingNode()) throw new Problem(422, "artifact_path_missing");
        data = JSON.convertValue(selected, Object.class);
      } catch (IllegalArgumentException error) {
        throw new Problem(422, "invalid_artifact_path");
      }
    }
    if (data instanceof List<?> values) {
      if (start < 0 || limit < 1 || limit > 100) throw new Problem(422, "invalid_pagination");
      int offset = Math.min(start, values.size()), end = Math.min(offset + limit, values.size());
      while (end > offset
          && utf8Bytes(PayloadJson.canonical(values.subList(offset, end))) > MAX_READ_BYTES) end--;
      if (end == offset && offset < values.size())
        throw new Problem(422, "artifact_item_too_large");
      return new Page(
          artifact.id(),
          artifact.hash(),
          values.size(),
          offset,
          end - offset,
          values.subList(offset, end));
    }
    if (utf8Bytes(PayloadJson.canonical(data)) > MAX_READ_BYTES)
      throw new Problem(422, "artifact_object_too_large");
    return new Page(artifact.id(), artifact.hash(), 1, 0, 1, data);
  }

  private static int utf8Bytes(String value) {
    return value.getBytes(StandardCharsets.UTF_8).length;
  }
}
