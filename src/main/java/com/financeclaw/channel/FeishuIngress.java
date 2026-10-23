package com.financeclaw.channel;

import com.fasterxml.jackson.databind.*;
import com.financeclaw.domain.Task.Problem;
import io.agentscope.extensions.channel.feishu.*;
import java.nio.charset.StandardCharsets;
import java.security.MessageDigest;
import java.time.Instant;
import org.springframework.stereotype.Component;

/**
 * 飞书入站验证：在解析业务字段前核验签名、时间与原始正文。
 *
 * <p>验证失败只返回稳定错误，不回显密钥或未验证事件；业务去重和归属检查由 FeishuService 负责。
 */
@Component
public class FeishuIngress {
    public static final int MAX_CALLBACK_BYTES = 64 * 1024;
    private static final int MAX_NONCE_CHARACTERS = 128;
    private static final long CALLBACK_CLOCK_SKEW_SECONDS = 300;
    private final FeishuSettings settings;
    private final ObjectMapper json = new ObjectMapper();

    public FeishuIngress(FeishuSettings settings) {
        this.settings = settings;
    }

    public JsonNode verify(String body, String timestamp, String nonce, String signature) {
        if (!settings.enabled) {
            throw new Problem(404, "not_found");
        }
        if (body == null
                || body.getBytes(StandardCharsets.UTF_8).length > MAX_CALLBACK_BYTES
                || timestamp == null
                || nonce == null
                || nonce.length() > MAX_NONCE_CHARACTERS
                || signature == null) {
            throw new Problem(401, "invalid_callback");
        }
        // 先做有界形状检查，再验签，只有认证通过的正文才能作为事件解释。
        try {
            long seconds = Long.parseLong(timestamp);
            long now = Instant.now().getEpochSecond();
            if (seconds < now - CALLBACK_CLOCK_SKEW_SECONDS
                    || seconds > now + CALLBACK_CLOCK_SKEW_SECONDS) {
                throw new IllegalArgumentException();
            }
            var crypto = new FeishuCrypto(settings.encryptKey);
            if (!crypto.verifySignature(signature, timestamp, nonce, body)) {
                throw new IllegalArgumentException();
            }
            JsonNode root = json.readTree(body);
            if (root.has("encrypt")) {
                root = json.readTree(crypto.decrypt(root.path("encrypt").asText()));
            }
            String token =
                    root.path("type").asText().equals("url_verification")
                            ? root.path("token").asText()
                            : root.path("header").path("token").asText();
            if (!MessageDigest.isEqual(
                    token.getBytes(StandardCharsets.UTF_8),
                    settings.token.getBytes(StandardCharsets.UTF_8))) {
                throw new IllegalArgumentException();
            }
            if (!root.path("type").asText().equals("url_verification")
                    && (!root.path("header").path("app_id").asText().equals(settings.appId)
                            || !root.path("header")
                                    .path("tenant_key")
                                    .asText()
                                    .equals(settings.tenantKey))) {
                throw new IllegalArgumentException();
            }
            return root;
        } catch (Exception e) {
            throw new Problem(401, "invalid_callback");
        }
    }
}
