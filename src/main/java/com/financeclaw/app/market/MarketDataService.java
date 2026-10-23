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

/**
 * 受治理的行情查询：先校验工具范围和调用预算，再读取固定发布版本绑定的行情快照。 主 Agent、研究子 Agent 及组合流程都在此预留一次根工具额度；调用此方法的外层工具不得重复扣额。
 * 确定性步骤的回放应从已保存回执恢复，只有首次实际读取才调用本服务。
 */
@Service
public class MarketDataService {
    private final ExecutionPolicyService policies;
    private final MarketCatalog catalog;

    public MarketDataService(ExecutionPolicyService policies, MarketCatalog catalog) {
        this.policies = policies;
        this.catalog = catalog;
    }

    public Map<String, Object> read(Claim claim, String symbol) {
        try {
            policies.consumeTool(claim, "market:read");
        } catch (Blocked blocked) {
            if (blocked.getMessage().equals("tool_denied")) {
                throw new Blocked("market_denied");
            }
            throw blocked;
        }
        if (symbol == null || !symbol.matches("[A-Za-z0-9._-]{1,16}")) {
            throw new Blocked("invalid_symbol");
        }
        var found =
                PayloadJson.object(catalog.snapshots(claim.turn().releaseHash()))
                        .get(symbol.toUpperCase(Locale.ROOT));
        if (!(found instanceof Map<?, ?> row)) {
            throw new Blocked("market_snapshot_unavailable");
        }
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
