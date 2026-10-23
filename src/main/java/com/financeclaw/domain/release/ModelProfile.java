package com.financeclaw.domain.release;

/** 固定模型配置：模型窗口、输出预留和安全余量共同限制真实请求预算。 */
public record ModelProfile(
        String name,
        String baseUrl,
        int inputLimit,
        int window,
        int outputReserve,
        int margin,
        boolean thinking) {
    public void validate() {
        if (thinking && !name.toLowerCase(java.util.Locale.ROOT).contains("qwen")) {
            throw new IllegalArgumentException("Thinking profile is only supported for Qwen");
        }
        if (inputLimit < 1 || outputReserve < 1 || window <= outputReserve + margin || margin < 0) {
            throw new IllegalArgumentException("Invalid model budget");
        }
        if (!name.equals("offline") && (baseUrl == null || baseUrl.isBlank())) {
            throw new IllegalArgumentException("Missing model endpoint");
        }
    }
}
