package com.financeclaw.tools;

import com.financeclaw.agent.*;
import com.financeclaw.application.*;
import com.financeclaw.domain.Governance.Blocked;
import com.financeclaw.domain.Task.Claim;
import io.agentscope.core.message.*;
import io.agentscope.core.model.*;
import io.agentscope.core.tool.ToolValidator;
import java.math.*;
import java.time.*;
import java.util.*;
import reactor.core.publisher.Flux;

/**
 * Fixed portfolio_review@1.1.0 steps. Native ReAct executes only this deterministic tool schedule.
 */
public final class PortfolioReview {
  public static final String COMMAND = "/workflow portfolio_review ";
  public static final Map<String, Object> SCHEMA =
      Map.of(
          "type",
          "object",
          "properties",
          Map.of(
              "portfolio_name", Map.of("type", "string", "minLength", 1, "maxLength", 120),
              "max_snapshot_age_hours", Map.of("type", "integer", "minimum", 1, "maximum", 168),
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
                              MarketData.SYMBOL,
                              "quantity",
                              Map.of("type", List.of("number", "string")),
                              "cost_basis",
                              Map.of("type", List.of("number", "string"))),
                          "required",
                          List.of("symbol", "quantity", "cost_basis"),
                          "additionalProperties",
                          false))),
          "required",
          List.of("portfolio_name", "positions"),
          "additionalProperties",
          false);
  public static final Map<String, Object> PUBLISH =
      GovernedTools.schema("draft_hash", Map.of("type", "string", "pattern", "^[a-f0-9]{64}$"));
  private final TaskService tasks;
  private final GovernanceService governance;
  private final Claim claim;
  private final CapabilitySteps steps;

  public PortfolioReview(TaskService tasks, GovernanceService governance, Claim claim) {
    this.tasks = tasks;
    this.governance = governance;
    this.claim = claim;
    steps = new CapabilitySteps(tasks, claim, "workflow:portfolio_review:1.1.0");
  }

  public static Map<String, Object> normalize(Map<String, Object> input) {
    if (ToolValidator.validateInput(ResumeInputs.canonical(input), SCHEMA) != null)
      throw new Blocked("invalid_portfolio");
    List<Map<String, Object>> positions = new ArrayList<>();
    Set<String> seen = new HashSet<>();
    for (Object raw : (List<?>) input.get("positions")) {
      Map<?, ?> row = (Map<?, ?>) raw;
      String symbol = ((String) row.get("symbol")).toUpperCase(Locale.ROOT);
      if (!seen.add(symbol)) throw new Blocked("duplicate_portfolio_symbol");
      positions.add(
          Map.of(
              "symbol",
              symbol,
              "quantity",
              decimal(row.get("quantity"), false).toPlainString(),
              "cost_basis",
              decimal(row.get("cost_basis"), true).toPlainString()));
    }
    return Map.of(
        "portfolio_name",
        input.get("portfolio_name"),
        "positions",
        positions,
        "max_snapshot_age_hours",
        input.getOrDefault("max_snapshot_age_hours", 48));
  }

  private static BigDecimal decimal(Object value, boolean zero) {
    try {
      var d = new BigDecimal(value.toString()).stripTrailingZeros();
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

  @SuppressWarnings("unchecked")
  public Map<String, Object> prepare(Map<String, Object> input) {
    var normalized = normalize(input);
    steps.run("normalize_input", normalized, () -> normalized);
    String clock =
        (String) steps.run("request_clock", Map.of(), () -> tasks.identity(claim).startedAt());
    List<Map<String, Object>> snapshots = new ArrayList<>();
    for (var position : (List<Map<String, Object>>) normalized.get("positions")) {
      String symbol = (String) position.get("symbol");
      snapshots.add(
          (Map<String, Object>)
              steps.run(
                  "snapshot:" + symbol,
                  position,
                  () -> {
                    var row = new TreeMap<>(new MarketData(tasks, claim).read(symbol));
                    row.put("quantity", position.get("quantity"));
                    return row;
                  }));
    }
    String oldest =
        (String)
            steps.run(
                "validate_freshness",
                snapshots,
                () -> {
                  Instant now = OffsetDateTime.parse(clock.replace(' ', 'T')).toInstant();
                  Instant first =
                      snapshots.stream()
                          .map(s -> OffsetDateTime.parse((String) s.get("as_of")).toInstant())
                          .min(Comparator.naturalOrder())
                          .orElseThrow();
                  for (var s : snapshots) {
                    Instant time = OffsetDateTime.parse((String) s.get("as_of")).toInstant();
                    if (time.isAfter(now.plusSeconds(300))
                        || time.isBefore(
                            now.minusSeconds(
                                ((Number) normalized.get("max_snapshot_age_hours")).longValue()
                                    * 3600))) throw new Blocked("stale_market_snapshot");
                  }
                  if (snapshots.stream().map(s -> s.get("currency")).distinct().count() != 1)
                    throw new Blocked("mixed_portfolio_currency");
                  return first.toString();
                });
    Map<String, Object> analysis =
        (Map<String, Object>) steps.run("analyze_exposure", snapshots, () -> analyze(snapshots));
    var draft = new TreeMap<String, Object>(analysis);
    draft.put("portfolio_name", normalized.get("portfolio_name"));
    draft.put("arguments_hash", GovernanceService.digest(ResumeInputs.canonical(normalized)));
    draft.put("snapshot_as_of", oldest);
    draft.put("source_refs", snapshots);
    draft.put("workflow_id", "portfolio_review");
    draft.put("workflow_version", "1.1.0");
    return (Map<String, Object>)
        steps.run(
            "publication_approval",
            draft,
            () ->
                Map.of(
                    "draft_hash",
                    GovernanceService.digest(ResumeInputs.canonical(draft)),
                    "draft",
                    draft));
  }

  public static Map<String, Object> analyze(List<Map<String, Object>> snapshots) {
    var values =
        snapshots.stream()
            .map(
                s ->
                    new BigDecimal(s.get("price").toString())
                        .multiply(new BigDecimal(s.get("quantity").toString())))
            .toList();
    BigDecimal total = values.stream().reduce(BigDecimal.ZERO, BigDecimal::add),
        largest = values.stream().max(Comparator.naturalOrder()).orElseThrow();
    String risk =
        largest.compareTo(total.multiply(new BigDecimal("0.50"))) >= 0
            ? "high"
            : largest.compareTo(total.multiply(new BigDecimal("0.30"))) >= 0 ? "moderate" : "low";
    return Map.of(
        "total_market_value",
        total.setScale(2, RoundingMode.HALF_EVEN).toPlainString(),
        "largest_position_weight",
        largest.divide(total, 4, RoundingMode.HALF_EVEN).toPlainString(),
        "risk_band",
        risk);
  }

  public Object publish(String hash) {
    var old =
        tasks
            .governance()
            .step(claim.turn().id(), "workflow:portfolio_review:1.1.0", "publication_approval");
    if (old == null) throw new Blocked("workflow_not_prepared");
    var prepared = GovernanceService.object(old.result());
    if (!Objects.equals(hash, prepared.get("draft_hash")))
      throw new Blocked("workflow_draft_mismatch");
    return steps.run(
        "publish_report",
        Map.of("draft_hash", hash),
        () -> {
          var artifact = governance.archive(claim, prepared.get("draft"), "workflow:run");
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

  public Model driver() {
    Map<String, Object> input =
        normalize(GovernanceService.object(claim.turn().message().substring(COMMAND.length())));
    return new Model() {
      public String getModelName() {
        return "portfolio-fixed-schedule";
      }

      public Flux<ChatResponse> stream(
          List<Msg> messages, List<ToolSchema> tools, GenerateOptions options) {
        List<ToolResultBlock> results =
            messages.stream()
                .flatMap(m -> m.getContentBlocks(ToolResultBlock.class).stream())
                .filter(r -> !r.isSuspended())
                .toList();
        ContentBlock output;
        if (results.stream()
            .anyMatch(
                r ->
                    r.getState() == ToolResultState.ERROR
                        || r.getState() == ToolResultState.DENIED))
          output = TextBlock.builder().text("组合复盘未发布：步骤失败或审批被拒绝。").build();
        else if (results.stream().anyMatch(r -> r.getName().equals("publish_portfolio_report")))
          output =
              TextBlock.builder()
                  .text(
                      results.getLast().getOutput().stream()
                          .filter(b -> b instanceof TextBlock)
                          .map(b -> ((TextBlock) b).getText())
                          .findFirst()
                          .orElseThrow())
                  .build();
        else if (results.stream().anyMatch(r -> r.getName().equals("portfolio_review"))) {
          var prepared =
              tasks
                  .governance()
                  .step(
                      claim.turn().id(), "workflow:portfolio_review:1.1.0", "publication_approval");
          output =
              call(
                  "publish",
                  "publish_portfolio_report",
                  Map.of(
                      "draft_hash", GovernanceService.object(prepared.result()).get("draft_hash")));
        } else output = call("prepare", "portfolio_review", input);
        return Flux.just(ChatResponse.builder().id("workflow").content(List.of(output)).build());
      }
    };
  }

  public static ToolUseBlock call(String id, String name, Map<String, Object> input) {
    return ToolUseBlock.builder()
        .id(id)
        .name(name)
        .input(input)
        .content(ResumeInputs.canonical(input))
        .build();
  }
}
