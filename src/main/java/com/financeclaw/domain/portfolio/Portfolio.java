package com.financeclaw.domain.portfolio;

import com.financeclaw.domain.Governance.Blocked;
import com.financeclaw.domain.market.MarketSnapshot;
import java.math.*;
import java.time.Instant;
import java.util.*;

/** Portfolio rules have no container, persistence, JSON or AgentScope dependency. */
public record Portfolio(String name, List<Position> positions, int maxSnapshotAgeHours) {
  public Portfolio {
    if (name == null
        || name.isEmpty()
        || name.codePointCount(0, name.length()) > 120
        || positions == null
        || positions.isEmpty()
        || positions.size() > 20
        || maxSnapshotAgeHours < 1
        || maxSnapshotAgeHours > 168) throw new Blocked("invalid_portfolio");
    positions = List.copyOf(positions);
    var seen = new HashSet<String>();
    for (var position : positions)
      if (!seen.add(position.symbol())) throw new Blocked("duplicate_portfolio_symbol");
  }

  public record Position(String symbol, BigDecimal quantity, BigDecimal costBasis) {
    public Position {
      if (symbol == null || !symbol.matches("[A-Za-z0-9._-]{1,16}"))
        throw new Blocked("invalid_portfolio");
      symbol = symbol.toUpperCase(Locale.ROOT);
      quantity = decimal(quantity, false);
      costBasis = decimal(costBasis, true);
    }

    public static Position parse(String symbol, Object quantity, Object costBasis) {
      try {
        return new Position(
            symbol, new BigDecimal(quantity.toString()), new BigDecimal(costBasis.toString()));
      } catch (Blocked e) {
        throw e;
      } catch (RuntimeException e) {
        throw new Blocked("invalid_portfolio_decimal");
      }
    }

    private static BigDecimal decimal(BigDecimal value, boolean zero) {
      try {
        var d = value.stripTrailingZeros();
        if (d.signum() < 0
            || (!zero && d.signum() == 0)
            || d.scale() > 8
            || (long) d.precision() + Math.max(0L, -(long) d.scale()) > 24)
          throw new IllegalArgumentException();
        return d;
      } catch (RuntimeException e) {
        throw new Blocked("invalid_portfolio_decimal");
      }
    }
  }

  public record Holding(MarketSnapshot snapshot, BigDecimal quantity) {
    public BigDecimal marketValue() {
      return snapshot.price().multiply(quantity);
    }
  }

  public record Exposure(
      BigDecimal totalMarketValue, BigDecimal largestPositionWeight, String riskBand) {
    public static Exposure analyze(List<Holding> holdings) {
      var values = holdings.stream().map(Holding::marketValue).toList();
      BigDecimal total = values.stream().reduce(BigDecimal.ZERO, BigDecimal::add),
          largest = values.stream().max(Comparator.naturalOrder()).orElseThrow();
      String risk =
          largest.compareTo(total.multiply(new BigDecimal("0.50"))) >= 0
              ? "high"
              : largest.compareTo(total.multiply(new BigDecimal("0.30"))) >= 0 ? "moderate" : "low";
      return new Exposure(
          total.setScale(2, RoundingMode.HALF_EVEN),
          largest.divide(total, 4, RoundingMode.HALF_EVEN),
          risk);
    }
  }

  public Instant oldestSnapshot(List<Holding> holdings, Instant clock) {
    Instant first =
        holdings.stream()
            .map(h -> h.snapshot().time())
            .min(Comparator.naturalOrder())
            .orElseThrow();
    for (var holding : holdings) {
      Instant time = holding.snapshot().time();
      if (time.isAfter(clock.plusSeconds(300))
          || time.isBefore(clock.minusSeconds(maxSnapshotAgeHours * 3600L)))
        throw new Blocked("stale_market_snapshot");
    }
    if (holdings.stream().map(h -> h.snapshot().currency()).distinct().count() != 1)
      throw new Blocked("mixed_portfolio_currency");
    return first;
  }
}
