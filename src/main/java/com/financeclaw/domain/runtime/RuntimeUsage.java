package com.financeclaw.domain.runtime;

import java.math.BigDecimal;

/** 预算提交计数不是provider token账单；估计仅按显式配置的每次调用单价计算。 */
public record RuntimeUsage(
        long modelCalls,
        long toolCalls,
        long activeWorkers,
        long expiredWorkers,
        BigDecimal configuredCallCostEstimate,
        String costBasis) {}
