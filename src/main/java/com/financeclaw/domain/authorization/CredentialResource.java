package com.financeclaw.domain.authorization;

import com.financeclaw.domain.Task.Owner;

/** 服务端凭据目录元数据；reference 不能作为 HTTP 响应返回。 */
public record CredentialResource(String resourceId, String reference, Owner owner) {
    /** 引用重绑定需要重新授权，摘要不包含密钥值。 */
    public String bindingHash() {
        try {
            return java.util.HexFormat.of()
                    .formatHex(
                            java.security.MessageDigest.getInstance("SHA-256")
                                    .digest(
                                            reference.getBytes(
                                                    java.nio.charset.StandardCharsets.UTF_8)));
        } catch (java.security.NoSuchAlgorithmException impossible) {
            throw new IllegalStateException(impossible);
        }
    }
}
