package com.financeclaw.app.portfolio;

import com.financeclaw.domain.Governance.Blocked;
import com.financeclaw.domain.portfolio.Portfolio;
import java.util.*;

/**
 * Typed rules plus the existing persisted argument representation, including numeric JSON types.
 */
public record PortfolioRequest(Portfolio portfolio, Map<String, Object> payload) {
  public PortfolioRequest {
    payload = Map.copyOf(payload);
  }

  public static PortfolioRequest from(Map<String, Object> input) {
    List<Portfolio.Position> positions = new ArrayList<>();
    Set<String> seen = new HashSet<>();
    for (Object raw : (List<?>) input.get("positions")) {
      var row = (Map<?, ?>) raw;
      String symbol = ((String) row.get("symbol")).toUpperCase(Locale.ROOT);
      if (!seen.add(symbol)) throw new Blocked("duplicate_portfolio_symbol");
      positions.add(Portfolio.Position.parse(symbol, row.get("quantity"), row.get("cost_basis")));
    }
    Object age = input.getOrDefault("max_snapshot_age_hours", 48);
    var portfolio =
        new Portfolio((String) input.get("portfolio_name"), positions, ((Number) age).intValue());
    return new PortfolioRequest(
        portfolio,
        Map.of(
            "portfolio_name",
            portfolio.name(),
            "positions",
            positions.stream()
                .map(
                    p ->
                        Map.<String, Object>of(
                            "symbol",
                            p.symbol(),
                            "quantity",
                            p.quantity().toPlainString(),
                            "cost_basis",
                            p.costBasis().toPlainString()))
                .toList(),
            "max_snapshot_age_hours",
            age));
  }
}
