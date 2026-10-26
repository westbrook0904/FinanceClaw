package com.financeclaw.domain.authorization;

import java.nio.charset.StandardCharsets;

public final class ResourceIdentity {
    private ResourceIdentity() {}

    public static boolean valid(String value) {
        return value != null
                && !value.isBlank()
                && value.length() <= 200
                && value.indexOf('\0') < 0
                && StandardCharsets.UTF_8.newEncoder().canEncode(value);
    }
}
