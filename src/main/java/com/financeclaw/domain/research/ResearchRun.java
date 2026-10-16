package com.financeclaw.domain.research;

import com.financeclaw.domain.ExecutionScope;
import com.financeclaw.domain.Governance.*;
import java.util.*;
import java.util.concurrent.ConcurrentHashMap;
import java.util.concurrent.atomic.AtomicInteger;

/** One child invocation: root identity, narrowed symbols, bounded calls and complete evidence. */
public final class ResearchRun {
  private final ExecutionScope scope;
  private final ExecutionIdentity identity;
  private final List<String> symbols;
  private final AtomicInteger modelCalls = new AtomicInteger();
  private final AtomicInteger toolCalls = new AtomicInteger();
  private final Map<String, Map<String, Object>> evidence = new ConcurrentHashMap<>();

  public ResearchRun(
      ExecutionScope root, String call, ExecutionIdentity rootIdentity, List<?> symbols) {
    this.scope = scope(root, call);
    this.identity =
        new ExecutionIdentity(
            rootIdentity.tenant(),
            rootIdentity.subject(),
            rootIdentity.conversationId(),
            rootIdentity.turnId(),
            rootIdentity.attemptId(),
            rootIdentity.releaseHash(),
            rootIdentity.grantRevision(),
            List.of("market:read"),
            rootIdentity.startedAt());
    this.symbols =
        symbols.stream().map(s -> s.toString().toUpperCase(Locale.ROOT)).distinct().toList();
  }

  public static boolean missing(Map<String, Object> input) {
    return !(input.get("symbols") instanceof List<?> s)
        || s.isEmpty()
        || !input.containsKey("analysis_period");
  }

  public static ExecutionScope scope(ExecutionScope root, String call) {
    return new ExecutionScope(
        root.tenant(),
        root.subject(),
        ExecutionScope.hash(List.of(root.key(), "market_research", call)),
        "market_research",
        root.release());
  }

  public ExecutionScope scope() {
    return scope;
  }

  public ExecutionIdentity identity() {
    return identity;
  }

  public List<String> symbols() {
    return symbols;
  }

  public void reserveModelCall() {
    if (modelCalls.incrementAndGet() > 4) throw new Blocked("child_model_budget_exceeded");
  }

  public String reserveSnapshot(Object value) {
    if (toolCalls.incrementAndGet() > 10) throw new Blocked("child_tool_budget_exceeded");
    String symbol = Objects.toString(value, "").toUpperCase(Locale.ROOT);
    if (!symbols.contains(symbol)) throw new Blocked("child_scope_expansion");
    return symbol;
  }

  public void recordEvidence(String symbol, Map<String, Object> snapshot) {
    if (!symbols.contains(symbol)) throw new Blocked("child_scope_expansion");
    evidence.put(symbol, Map.copyOf(snapshot));
  }

  public List<Map<String, Object>> completedEvidence(boolean completedState, String summary) {
    if (!completedState
        || evidence.size() != symbols.size()
        || summary.isBlank()
        || summary.length() > 8000) throw new Blocked("child_incomplete");
    return symbols.stream().map(evidence::get).toList();
  }
}
