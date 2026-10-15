package com.financeclaw.channel;

import com.fasterxml.jackson.databind.*;
import com.financeclaw.domain.Task.Problem;
import io.agentscope.extensions.channel.feishu.*;
import java.nio.charset.StandardCharsets;
import java.security.MessageDigest;
import java.time.Instant;
import org.springframework.stereotype.Component;

/** Authentication is stricter than the SDK's optional callback signature check. */
@Component
public class FeishuIngress {
  private final FeishuSettings settings;
  private final ObjectMapper json = new ObjectMapper();

  public FeishuIngress(FeishuSettings settings) {
    this.settings = settings;
  }

  public JsonNode verify(String body, String timestamp, String nonce, String signature) {
    if (!settings.enabled) throw new Problem(404, "not_found");
    if (body == null
        || body.getBytes(StandardCharsets.UTF_8).length > 65536
        || timestamp == null
        || nonce == null
        || nonce.length() > 128
        || signature == null) throw new Problem(401, "invalid_callback");
    try {
      long seconds = Long.parseLong(timestamp);
      long now = Instant.now().getEpochSecond();
      if (seconds < now - 300 || seconds > now + 300) throw new IllegalArgumentException();
      var crypto = new FeishuCrypto(settings.encryptKey);
      if (!crypto.verifySignature(signature, timestamp, nonce, body))
        throw new IllegalArgumentException();
      JsonNode root = json.readTree(body);
      if (root.has("encrypt")) root = json.readTree(crypto.decrypt(root.path("encrypt").asText()));
      String token =
          root.path("type").asText().equals("url_verification")
              ? root.path("token").asText()
              : root.path("header").path("token").asText();
      if (!MessageDigest.isEqual(
          token.getBytes(StandardCharsets.UTF_8), settings.token.getBytes(StandardCharsets.UTF_8)))
        throw new IllegalArgumentException();
      if (!root.path("type").asText().equals("url_verification")
          && (!root.path("header").path("app_id").asText().equals(settings.appId)
              || !root.path("header").path("tenant_key").asText().equals(settings.tenantKey)))
        throw new IllegalArgumentException();
      return root;
    } catch (Exception e) {
      throw new Problem(401, "invalid_callback");
    }
  }
}
