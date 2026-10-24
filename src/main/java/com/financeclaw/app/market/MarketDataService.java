package com.financeclaw.app.market;

import com.financeclaw.app.execution.ExecutionPolicyService;
import com.financeclaw.app.market.port.MarketCatalog;
import com.financeclaw.domain.Governance.Blocked;
import com.financeclaw.domain.Task.Claim;
import com.financeclaw.domain.Task.Owner;
import com.financeclaw.domain.agent.PrivateAgentTarget;
import com.financeclaw.domain.agent.ResolvedAgentRelease;
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
        consume(claim);
        requireSymbol(symbol);
        return readSnapshot(catalog.snapshots(claim.turn().releaseHash()), symbol, null);
    }

    /**
     * 私有市场叶子的数据源只能是 begin 已解析的目标发布，而不是根 Turn 或当前部署指针。 继续借用真实父 Claim 扣根额度；子额度与幂等步骤由
     * ChildInvocationService 的同一事务管理。 target/frozen 都来自服务端绑定，不向模型开放 release 或 owner
     * 参数，也不为每次读行情重复查询清单。
     */
    public Map<String, Object> readFrozen(
            Claim parent, PrivateAgentTarget target, ResolvedAgentRelease frozen, String symbol) {
        if (target == null || target.profile() != PrivateAgentTarget.Profile.MARKET_SNAPSHOT) {
            throw new Blocked("agent_leaf_required");
        }
        var owner = new Owner(parent.turn().tenant(), parent.turn().subject());
        PrivateAgentTarget.requireLeaf(owner, frozen, target.profile());
        frozen.requireIdentity(owner, target.agentId());
        consume(parent);
        return readSnapshot(
                frozen.definition().business().marketJson(), symbol, target.releaseHash());
    }

    private void consume(Claim claim) {
        try {
            policies.consumeTool(claim, "market:read");
        } catch (Blocked blocked) {
            if (blocked.getMessage().equals("tool_denied")) {
                throw new Blocked("market_denied");
            }
            throw blocked;
        }
    }

    private Map<String, Object> readSnapshot(String snapshots, String symbol, String sourceHash) {
        requireSymbol(symbol);
        var found = PayloadJson.object(snapshots).get(symbol.toUpperCase(Locale.ROOT));
        if (!(found instanceof Map<?, ?> row)) {
            throw new Blocked("market_snapshot_unavailable");
        }
        var snapshot = snapshot(symbol.toUpperCase(Locale.ROOT), row);
        var result =
                new LinkedHashMap<String, Object>(
                        Map.of(
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
                                PayloadDigest.sha256(
                                        PayloadJson.canonical(Map.of("symbol", snapshot.symbol()))),
                                "tool_version",
                                "1.0.0"));
        // 旧主/研究路径保持原结果和输入摘要；新 profile 显式携带冻结来源并将其纳入摘要。
        if (sourceHash != null) {
            result.put("source_release_hash", sourceHash);
            result.put(
                    "input_hash",
                    PayloadDigest.sha256(
                            PayloadJson.canonical(
                                    Map.of(
                                            "symbol",
                                            snapshot.symbol(),
                                            "source_release_hash",
                                            sourceHash))));
        }
        return result;
    }

    private static void requireSymbol(String symbol) {
        if (symbol == null || !symbol.matches("[A-Za-z0-9._-]{1,16}")) {
            throw new Blocked("invalid_symbol");
        }
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
