package com.financeclaw.app.portfolio;

import com.financeclaw.app.execution.*;
import com.financeclaw.app.market.MarketDataService;
import com.financeclaw.app.portfolio.port.PortfolioPublication;
import com.financeclaw.domain.Governance.Blocked;
import com.financeclaw.domain.Task.Claim;
import com.financeclaw.domain.portfolio.Portfolio;
import com.financeclaw.support.*;
import java.math.BigDecimal;
import java.time.OffsetDateTime;
import java.util.*;
import org.springframework.stereotype.Service;

/** Fixed local steps, with the original keys and inputs for durable replay compatibility. */
@Service
public class PortfolioReviewService {
  public static final String COMMAND = "/workflow portfolio_review ";
  public static final String NAMESPACE = "workflow:portfolio_review:1.1.0";
  private final CapabilityStepService steps;
  private final ExecutionPolicyService policies;
  private final MarketDataService market;
  private final PortfolioPublication publication;

  public PortfolioReviewService(
      CapabilityStepService steps,
      ExecutionPolicyService policies,
      MarketDataService market,
      PortfolioPublication publication) {
    this.steps = steps;
    this.policies = policies;
    this.market = market;
    this.publication = publication;
  }

  @SuppressWarnings("unchecked")
  public Map<String, Object> prepare(Claim claim, PortfolioRequest request) {
    var normalized = request.payload();
    steps.run(claim, NAMESPACE, "normalize_input", normalized, () -> normalized);
    String clock =
        (String)
            steps.run(
                claim,
                NAMESPACE,
                "request_clock",
                Map.of(),
                () -> policies.identity(claim).startedAt());
    List<Map<String, Object>> snapshots = new ArrayList<>();
    for (var position : (List<Map<String, Object>>) normalized.get("positions")) {
      String symbol = (String) position.get("symbol");
      snapshots.add(
          (Map<String, Object>)
              steps.run(
                  claim,
                  NAMESPACE,
                  "snapshot:" + symbol,
                  position,
                  () -> {
                    var row = new TreeMap<>(market.read(claim, symbol));
                    row.put("quantity", position.get("quantity"));
                    return row;
                  }));
    }
    var holdings =
        snapshots.stream()
            .map(
                s ->
                    new Portfolio.Holding(
                        MarketDataService.snapshot((String) s.get("symbol"), s),
                        new BigDecimal(s.get("quantity").toString())))
            .toList();
    String oldest =
        (String)
            steps.run(
                claim,
                NAMESPACE,
                "validate_freshness",
                snapshots,
                () ->
                    request
                        .portfolio()
                        .oldestSnapshot(
                            holdings, OffsetDateTime.parse(clock.replace(' ', 'T')).toInstant())
                        .toString());
    Map<String, Object> analysis =
        (Map<String, Object>)
            steps.run(
                claim,
                NAMESPACE,
                "analyze_exposure",
                snapshots,
                () -> exposure(Portfolio.Exposure.analyze(holdings)));
    var draft = new TreeMap<String, Object>(analysis);
    draft.put("portfolio_name", normalized.get("portfolio_name"));
    draft.put("arguments_hash", PayloadDigest.sha256(PayloadJson.canonical(normalized)));
    draft.put("snapshot_as_of", oldest);
    draft.put("source_refs", snapshots);
    draft.put("workflow_id", "portfolio_review");
    draft.put("workflow_version", "1.1.0");
    return (Map<String, Object>)
        steps.run(
            claim,
            NAMESPACE,
            "publication_approval",
            draft,
            () ->
                Map.of(
                    "draft_hash",
                    PayloadDigest.sha256(PayloadJson.canonical(draft)),
                    "draft",
                    draft));
  }

  public static Map<String, Object> exposure(Portfolio.Exposure e) {
    return Map.of(
        "total_market_value",
        e.totalMarketValue().toPlainString(),
        "largest_position_weight",
        e.largestPositionWeight().toPlainString(),
        "risk_band",
        e.riskBand());
  }

  private Map<String, Object> prepared(Claim claim) {
    var old = steps.find(claim, NAMESPACE, "publication_approval");
    if (old == null) throw new Blocked("workflow_not_prepared");
    return PayloadJson.object(old.result());
  }

  public String preparedHash(Claim claim) {
    return (String) prepared(claim).get("draft_hash");
  }

  public Object publish(Claim claim, String hash) {
    var prepared = prepared(claim);
    if (!Objects.equals(hash, prepared.get("draft_hash")))
      throw new Blocked("workflow_draft_mismatch");
    return steps.run(
        claim,
        NAMESPACE,
        "publish_report",
        Map.of("draft_hash", hash),
        () -> {
          var artifact = publication.archive(claim, prepared.get("draft"));
          return Map.of(
              "workflow_id",
              "portfolio_review",
              "workflow_version",
              "1.1.0",
              "status",
              "completed",
              "artifact_id",
              artifact.id(),
              "hash",
              artifact.hash(),
              "draft_hash",
              hash);
        });
  }
}
