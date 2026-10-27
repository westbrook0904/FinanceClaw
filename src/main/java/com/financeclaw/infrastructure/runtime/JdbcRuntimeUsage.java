package com.financeclaw.infrastructure.runtime;

import com.financeclaw.app.runtime.port.RuntimeUsageGateway;
import com.financeclaw.domain.runtime.RuntimeUsage;
import java.math.BigDecimal;
import org.springframework.jdbc.core.JdbcTemplate;

/** 只导出全局固定维度聚合，不返回身份、参数、正文或原始异常。 */
public final class JdbcRuntimeUsage implements RuntimeUsageGateway {
    private final JdbcTemplate sql;
    private final BigDecimal modelRate, toolRate;

    public JdbcRuntimeUsage(JdbcTemplate sql, BigDecimal modelRate, BigDecimal toolRate) {
        if (modelRate.signum() < 0
                || toolRate.signum() < 0
                || modelRate.compareTo(new BigDecimal("1000000")) > 0
                || toolRate.compareTo(new BigDecimal("1000000")) > 0)
            throw new IllegalArgumentException("invalid_call_cost_rate");
        this.sql = java.util.Objects.requireNonNull(sql);
        this.modelRate = modelRate;
        this.toolRate = toolRate;
    }

    @Override
    public RuntimeUsage snapshot() {
        return sql.queryForObject(
                "SELECT (SELECT COALESCE(sum(model_calls),0) FROM business.turn_policies) AS models,"
                        + "(SELECT COALESCE(sum(tool_calls),0) FROM business.turn_policies) AS tools,"
                        + "(SELECT count(*) FROM business.runtime_workers WHERE state='active' AND expires_at>clock_timestamp()) AS active,"
                        + "(SELECT count(*) FROM business.runtime_workers WHERE state='active' AND expires_at<=clock_timestamp()) AS expired",
                (row, index) -> {
                    long models = row.getLong("models"), tools = row.getLong("tools");
                    boolean configured = modelRate.signum() != 0 || toolRate.signum() != 0;
                    return new RuntimeUsage(
                            models,
                            tools,
                            row.getLong("active"),
                            row.getLong("expired"),
                            configured
                                    ? modelRate
                                            .multiply(BigDecimal.valueOf(models))
                                            .add(toolRate.multiply(BigDecimal.valueOf(tools)))
                                    : null,
                            configured
                                    ? "configured-cost-per-committed-budget-call-not-provider-billing"
                                    : "unconfigured-not-provider-billing");
                });
    }
}
