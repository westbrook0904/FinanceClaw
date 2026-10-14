package com.financeclaw.domain;

import java.nio.ByteBuffer;
import java.nio.charset.StandardCharsets;
import java.security.MessageDigest;
import java.security.NoSuchAlgorithmException;
import java.util.HexFormat;
import java.util.List;

/** Server-owned identities. SDK keys never contain user-controlled path separators. */
public record ExecutionScope(
    String tenant, String subject, String conversation, String agent, String release) {
  public ExecutionScope {
    for (String value : new String[] {tenant, subject, conversation, agent, release}) {
      if (value == null || value.isBlank() || value.length() > 512) {
        throw new IllegalArgumentException("Invalid execution identity");
      }
    }
  }

  public String namespace() {
    return hash(List.of("financeclaw-state-v1", tenant, subject, agent, release));
  }

  public String userId() {
    return hash(List.of(tenant, subject));
  }

  public String sessionId() {
    return hash(List.of(conversation, agent, release));
  }

  public String key() {
    return hash(List.of(namespace(), userId(), sessionId()));
  }

  public static String hash(List<String> parts) {
    try {
      MessageDigest digest = MessageDigest.getInstance("SHA-256");
      for (String part : parts) {
        byte[] bytes = part.getBytes(StandardCharsets.UTF_8);
        digest.update(ByteBuffer.allocate(4).putInt(bytes.length).array());
        digest.update(bytes);
      }
      return HexFormat.of().formatHex(digest.digest());
    } catch (NoSuchAlgorithmException impossible) {
      throw new IllegalStateException(impossible);
    }
  }
}
