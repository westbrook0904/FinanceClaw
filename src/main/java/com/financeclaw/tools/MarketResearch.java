package com.financeclaw.tools;

import com.financeclaw.agent.*;
import com.financeclaw.application.*;
import com.financeclaw.domain.ExecutionScope;
import com.financeclaw.domain.Governance.*;
import com.financeclaw.domain.Task.Claim;
import com.financeclaw.infrastructure.state.*;
import io.agentscope.core.message.*;
import io.agentscope.core.model.*;
import io.agentscope.core.permission.*;
import io.agentscope.core.state.*;
import io.agentscope.core.tool.*;
import java.time.Duration;
import java.util.*;
import java.util.concurrent.atomic.AtomicInteger;
import reactor.core.publisher.*;
import reactor.core.scheduler.Schedulers;

/**
 * Fixed child catalog, root-owned budgets and identity, native ReAct execution and cancellation.
 */
public final class MarketResearch {
  public static final Map<String, Object> FIELDS =
      Map.of(
          "symbols",
              Map.of(
                  "type", "array", "maxItems", 10, "uniqueItems", true, "items", MarketData.SYMBOL),
          "analysis_period", Map.of("type", "string", "minLength", 1, "maxLength", 128));
  public static final Map<String, Object> ANSWER =
      Map.of(
          "type",
          "object",
          "properties",
          FIELDS,
          "required",
          List.of("symbols", "analysis_period"),
          "additionalProperties",
          false);
  public static final Map<String, Object> SCHEMA;

  static {
    var p = new TreeMap<>(FIELDS);
    p.put("task", Map.of("type", "string", "minLength", 1, "maxLength", 4000));
    SCHEMA =
        Map.of(
            "type",
            "object",
            "properties",
            p,
            "required",
            List.of("task"),
            "additionalProperties",
            false);
  }

  private final TaskService tasks;
  private final Claim claim;
  private final RuntimeMapper states;
  private final Model model;

  public MarketResearch(TaskService tasks, Claim claim, RuntimeMapper states, Model model) {
    this.tasks = tasks;
    this.claim = claim;
    this.states = states;
    this.model = model;
  }

  public static boolean missing(Map<String, Object> input) {
    return !(input.get("symbols") instanceof List<?> s)
        || s.isEmpty()
        || !input.containsKey("analysis_period");
  }

  public ExecutionScope scope(String call) {
    var root = claim.scope();
    return new ExecutionScope(
        root.tenant(),
        root.subject(),
        ExecutionScope.hash(List.of(root.key(), "market_research", call)),
        "market_research",
        root.release());
  }

  public Mono<ToolResultBlock> run(ToolCallParam param) {
    if (missing(param.getInput()))
      return Mono.just(ToolResultBlock.suspended(param.getToolUseBlock()));
    return Mono.defer(
            () -> {
              tasks.checkDispatch(claim);
              var allowed =
                  GovernanceService.scopes(tasks.governance().policy(claim.turn().id()).scopes());
              if (!allowed.containsAll(List.of("market:read", "research:run")))
                throw new Blocked("research_denied");
              List<String> symbols =
                  ((List<?>) param.getInput().get("symbols"))
                      .stream().map(s -> s.toString().toUpperCase(Locale.ROOT)).distinct().toList();
              String call = param.getToolUseBlock().getId();
              var old = tasks.governance().step(claim.turn().id(), "child:" + call, "result");
              String inputHash = GovernanceService.digest(ResumeInputs.canonical(param.getInput()));
              if (old != null) {
                if (!old.inputHash().equals(inputHash)) throw new Blocked("child_input_conflict");
                return Mono.just(ToolResultBlock.text(old.result()));
              }
              var scope = scope(call);
              var store =
                  new MyBatisAgentStateStore(states, scope) {
                    @Override
                    public long saveIfVersion(String u, String s, String k, State state, long v) {
                      return tasks.fenced(claim, () -> super.saveIfVersion(u, s, k, state, v));
                    }
                  };
              Map<String, Map<String, Object>> evidence =
                  new java.util.concurrent.ConcurrentHashMap<>();
              Toolkit toolkit = new Toolkit();
              AtomicInteger toolCalls = new AtomicInteger();
              toolkit.registerAgentTool(
                  new ToolBase(
                      ToolBase.builder()
                          .name("market_snapshot")
                          .description(
                              "Read release-pinned market evidence; retain provider and as_of.")
                          .inputSchema(MarketData.SCHEMA)) {
                    public Mono<ToolResultBlock> callAsync(ToolCallParam p) {
                      return Mono.fromCallable(
                              () -> {
                                if (toolCalls.incrementAndGet() > 10)
                                  throw new Blocked("child_tool_budget_exceeded");
                                String symbol =
                                    Objects.toString(p.getInput().get("symbol"), "")
                                        .toUpperCase(Locale.ROOT);
                                if (!symbols.contains(symbol))
                                  throw new Blocked("child_scope_expansion");
                                var snapshot = new MarketData(tasks, claim).read(symbol);
                                evidence.put(symbol, snapshot);
                                var result =
                                    new CapabilitySteps(tasks, claim, "child:" + call)
                                        .run(
                                            "tool:" + p.getToolUseBlock().getId(),
                                            p.getInput(),
                                            () -> snapshot);
                                return ToolResultBlock.text(ResumeInputs.canonical(result));
                              })
                          .subscribeOn(Schedulers.boundedElastic());
                    }
                  });
              AtomicInteger calls = new AtomicInteger();
              Model delegate = model == null ? offline(symbols) : model;
              Model bounded =
                  new Model() {
                    public String getModelName() {
                      return delegate.getModelName();
                    }

                    public Flux<ChatResponse> stream(
                        List<Msg> messages, List<ToolSchema> schemas, GenerateOptions options) {
                      return Flux.defer(
                          () -> {
                            if (calls.incrementAndGet() > 4)
                              throw new Blocked("child_model_budget_exceeded");
                            tasks.consume(claim, true);
                            return delegate.stream(messages, schemas, options);
                          });
                    }
                  };
              var permission =
                  PermissionContextState.builder()
                      .mode(PermissionMode.DEFAULT)
                      .addAllowRule(
                          "market_snapshot",
                          new PermissionRule(
                              "market_snapshot", null, PermissionBehavior.ALLOW, "child-narrowed"))
                      .build();
              return Mono.using(
                  () -> IsolatedAgentFactory.create(bounded, toolkit, store, permission),
                  agent -> {
                    var identity = tasks.identity(claim);
                    var ctx =
                        io.agentscope.core.agent.RuntimeContext.builder(
                                IsolatedAgentFactory.context(scope))
                            .put(
                                ExecutionIdentity.class,
                                new ExecutionIdentity(
                                    identity.tenant(),
                                    identity.subject(),
                                    identity.conversationId(),
                                    identity.turnId(),
                                    identity.attemptId(),
                                    identity.releaseHash(),
                                    identity.grantRevision(),
                                    List.of("market:read"),
                                    identity.startedAt()))
                            .build();
                    return agent
                        .call(
                            "Research only the supplied symbols and period. Use market_snapshot for every symbol; retain limitations.\n"
                                + ResumeInputs.canonical(param.getInput()),
                            ctx)
                        .timeout(Duration.ofSeconds(15))
                        .doOnCancel(() -> agent.interrupt(ctx))
                        .map(
                            answer -> {
                              tasks.checkDispatch(claim);
                              var state =
                                  store.getVersioned(
                                      scope.userId(),
                                      scope.sessionId(),
                                      "agent_state",
                                      AgentState.class);
                              if (!state.isPresent()
                                  || !TaskAgentRuntime.classify(state.value(), state.version())
                                      .status()
                                      .equals("completed")
                                  || evidence.size() != symbols.size()
                                  || answer.getTextContent().isBlank()
                                  || answer.getTextContent().length() > 8000)
                                throw new Blocked("child_incomplete");
                              Object result =
                                  new CapabilitySteps(tasks, claim, "child:" + call)
                                      .run(
                                          "result",
                                          param.getInput(),
                                          () ->
                                              Map.of(
                                                  "outcome",
                                                  "success",
                                                  "summary",
                                                  answer.getTextContent(),
                                                  "evidence",
                                                  symbols.stream().map(evidence::get).toList(),
                                                  "analysis_period",
                                                  param.getInput().get("analysis_period"),
                                                  "limitations",
                                                  List.of("仅使用发布时冻结的行情快照；不代表实时行情。")));
                              return ToolResultBlock.text(ResumeInputs.canonical(result));
                            });
                  },
                  agent -> agent.close());
            })
        .onErrorMap(e -> e instanceof Blocked ? e : new Blocked("child_failed"));
  }

  private static Model offline(List<String> symbols) {
    return new Model() {
      public String getModelName() {
        return "market-research-offline-replay";
      }

      public Flux<ChatResponse> stream(
          List<Msg> messages, List<ToolSchema> tools, GenerateOptions options) {
        boolean read =
            messages.stream().anyMatch(m -> !m.getContentBlocks(ToolResultBlock.class).isEmpty());
        List<ContentBlock> content = new ArrayList<>();
        if (read)
          content.add(TextBlock.builder().text("已核对指定证券的冻结行情；来源和时间见 evidence，不提供实时行情推断。").build());
        else
          for (int i = 0; i < symbols.size(); i++)
            content.add(
                PortfolioReview.call(
                    "call-" + (i + 1), "market_snapshot", Map.of("symbol", symbols.get(i))));
        return Flux.just(ChatResponse.builder().id("research").content(content).build());
      }
    };
  }
}
