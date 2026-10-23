package com.financeclaw.domain;

import com.financeclaw.domain.Task.Problem;

/** 外部幂等键值：保留调用方原始字节语义并验证非空与长度约束，不能通过自动修剪改变重试身份。 */
public record IdempotencyKey(String value) {
    public IdempotencyKey {
        if (value == null || value.isBlank() || value.length() > 256) {
            throw new Problem(422, "invalid_idempotency_key");
        }
    }
}
