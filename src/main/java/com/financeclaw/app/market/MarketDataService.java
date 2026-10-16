package com.financeclaw.app.market;

import com.financeclaw.app.execution.ExecutionPolicyService;
import com.financeclaw.app.market.port.MarketCatalog;
import com.financeclaw.domain.Governance.Blocked;
import com.financeclaw.domain.Task.Claim;
import com.financeclaw.domain.market.MarketSnapshot;
import com.financeclaw.support.*;
import java.math.BigDecimal;
import java.util.*;
import org.springframework.stereotype.Service;

@Service
public class MarketDataService {
  private final ExecutionPolicyService policies;
  private final MarketCatalog catalog;

  public MarketDataService(ExecutionPolicyService policies, MarketCatalog catalog) {
    this.policies = policies;
    this.catalog = catalog;
  }

  public Map<String, Object> read(Claim claim, String symbol) {
    policies.checkDispatch(claim);
    if (!policies.scopes(claim).contains("market:read")) throw new Blocked("market_denied");
    policies.consume(claim, false);
    if (symbol == null || !symbol.matches("[A-Za-z0-9._-]{1,16}"))
      throw new Blocked("invalid_symbol");
    var found =
        PayloadJson.object(catalog.snapshots(claim.turn().releaseHash()))
            .get(symbol.toUpperCase(Locale.ROOT));
    if (!(found instanceof Map<?, ?> row)) throw new Blocked("market_snapshot_unavailable");
    var snapshot = snapshot(symbol.toUpperCase(Locale.ROOT), row);
    return Map.of(
        "symbol",
        snapshot.symbol(),
        "price",
        snapshot.price().toPlainString(),
        "currency",
        snapshot.currency(),
        "provider",
        snapshot.provider(),
        "as_of",
        snapshot.asOf(),
        "input_hash",
        PayloadDigest.sha256(PayloadJson.canonical(Map.of("symbol", snapshot.symbol()))),
        "tool_version",
        "1.0.0");
  }

  public static MarketSnapshot snapshot(String symbol, Map<?, ?> row) {
    try {
      return new MarketSnapshot(
          symbol,
          new BigDecimal((String) row.get("price")),
          (String) row.get("currency"),
          (String) row.get("provider"),
          (String) row.get("as_of"));
    } catch (RuntimeException e) {
      throw new Blocked("invalid_market_snapshot");
    }
  }
}
