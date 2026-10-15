package com.financeclaw.tools;

import com.financeclaw.agent.*;
import com.financeclaw.application.*;
import com.financeclaw.domain.Governance.Blocked;
import com.financeclaw.domain.Task.Claim;
import java.math.BigDecimal;
import java.time.OffsetDateTime;
import java.util.*;

/**
 * Release-pinned, timestamped snapshots supplied by the operator; never manufacture current prices.
 */
public final class MarketData {
  public static final Map<String, Object> SYMBOL =
      Map.of("type", "string", "minLength", 1, "maxLength", 16, "pattern", "^[A-Za-z0-9._-]+$");
  public static final Map<String, Object> SCHEMA = GovernedTools.schema("symbol", SYMBOL);
  private final TaskService tasks;
  private final Claim claim;

  public MarketData(TaskService tasks, Claim claim) {
    this.tasks = tasks;
    this.claim = claim;
  }

  public Map<String, Object> read(String symbol) {
    tasks.checkDispatch(claim);
    if (!GovernanceService.scopes(tasks.governance().policy(claim.turn().id()).scopes())
        .contains("market:read")) throw new Blocked("market_denied");
    tasks.consume(claim, false);
    if (symbol == null || !symbol.matches("[A-Za-z0-9._-]{1,16}"))
      throw new Blocked("invalid_symbol");
    var data =
        GovernanceService.object(
            tasks.releases().require(claim.turn().releaseHash()).business().marketJson());
    var found = data.get(symbol.toUpperCase(Locale.ROOT));
    if (!(found instanceof Map<?, ?> row)) throw new Blocked("market_snapshot_unavailable");
    try {
      BigDecimal price = new BigDecimal((String) row.get("price"));
      String provider = (String) row.get("provider"),
          currency = (String) row.get("currency"),
          time = (String) row.get("as_of");
      if (price.signum() <= 0
          || (long) price.precision() + Math.max(0L, -(long) price.scale()) > 24
          || price.scale() > 8
          || provider == null
          || provider.isBlank()
          || provider.length() > 128
          || currency == null
          || !currency.matches("[A-Z]{3}")) throw new IllegalArgumentException();
      OffsetDateTime.parse(time);
      return Map.of(
          "symbol",
          symbol.toUpperCase(Locale.ROOT),
          "price",
          price.toPlainString(),
          "currency",
          currency,
          "provider",
          provider,
          "as_of",
          time,
          "input_hash",
          GovernanceService.digest(
              ResumeInputs.canonical(Map.of("symbol", symbol.toUpperCase(Locale.ROOT)))),
          "tool_version",
          "1.0.0");
    } catch (RuntimeException e) {
      throw new Blocked("invalid_market_snapshot");
    }
  }
}
