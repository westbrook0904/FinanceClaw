package com.financeclaw.infrastructure.agentscope.capability;

import com.financeclaw.app.portfolio.PortfolioRequest;
import com.financeclaw.domain.Governance.Blocked;
import com.financeclaw.support.*;
import io.agentscope.core.tool.ToolValidator;
import java.util.*;

/**
 * 定义组合复盘与发布摘要的工具 schema，并将校验后的输入交给领域请求解析。 SDK schema 检查限制字段形状，PortfolioRequest
 * 再处理持仓数值等业务规则；格式不合法时以固定错误阻断。此类不持有 owner、租约或数据库连接。
 */
public final class PortfolioInputs {
    private PortfolioInputs() {}

    public static final Map<String, Object> SCHEMA =
            Map.of(
                    "type",
                    "object",
                    "properties",
                    Map.of(
                            "portfolio_name",
                                    Map.of("type", "string", "minLength", 1, "maxLength", 120),
                            "max_snapshot_age_hours",
                                    Map.of("type", "integer", "minimum", 1, "maximum", 168),
                            "positions",
                                    Map.of(
                                            "type",
                                            "array",
                                            "minItems",
                                            1,
                                            "maxItems",
                                            20,
                                            "items",
                                            Map.of(
                                                    "type",
                                                    "object",
                                                    "properties",
                                                    Map.of(
                                                            "symbol",
                                                            MarketToolSchemas.SYMBOL,
                                                            "quantity",
                                                            Map.of(
                                                                    "type",
                                                                    List.of("number", "string")),
                                                            "cost_basis",
                                                            Map.of(
                                                                    "type",
                                                                    List.of("number", "string"))),
                                                    "required",
                                                    List.of("symbol", "quantity", "cost_basis"),
                                                    "additionalProperties",
                                                    false))),
                    "required",
                    List.of("portfolio_name", "positions"),
                    "additionalProperties",
                    false);
    public static final Map<String, Object> PUBLISH =
            ToolSchemas.required(
                    "draft_hash", Map.of("type", "string", "pattern", "^[a-f0-9]{64}$"));

    public static PortfolioRequest parse(Map<String, Object> input) {
        if (ToolValidator.validateInput(PayloadJson.canonical(input), SCHEMA) != null) {
            throw new Blocked("invalid_portfolio");
        }
        return PortfolioRequest.from(input);
    }
}
