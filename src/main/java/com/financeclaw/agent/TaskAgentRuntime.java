package com.financeclaw.agent;

import com.financeclaw.application.GovernanceService;
import com.financeclaw.application.TaskService;
import com.financeclaw.domain.Governance.*;
import com.financeclaw.domain.Task.*;
import com.financeclaw.infrastructure.state.MyBatisAgentStateStore;
import com.financeclaw.infrastructure.state.RuntimeMapper;
import io.agentscope.core.ReActAgent;
import io.agentscope.core.event.*;
import io.agentscope.core.message.*;
import io.agentscope.core.model.*;
import io.agentscope.core.permission.*;
import io.agentscope.core.state.*;
import java.time.Duration;
import java.util.*;
import java.util.concurrent.ConcurrentHashMap;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.stereotype.Component;
import reactor.core.publisher.Flux;
import reactor.core.publisher.Sinks;

/** Native ReAct execution with release-bound tools, model adapters and isolated resume slots. */
@Component
public class TaskAgentRuntime {
  private final TaskService tasks;
  private final RuntimeMapper states;
  private final Duration timeout;
  private final GovernanceService governance;

  private record Active(ReActAgent agent, Sinks.Empty<Void> stop) {}

  private final Map<String, Active> active = new ConcurrentHashMap<>();

  public TaskAgentRuntime(
      TaskService tasks,
      GovernanceService governance,
      RuntimeMapper states,
      @Value("${financeclaw.worker.model-timeout-ms:30000}") long timeoutMillis) {
    this.governance = governance;
    this.tasks = tasks;
    this.states = states;
    this.timeout = Duration.ofMillis(timeoutMillis);
  }

  protected Model model(Claim claim) {
    return new Model() {
      @Override
      public String getModelName() {
        return "financeclaw-offline-v1";
      }

      @Override
      public Flux<ChatResponse> stream(
          List<Msg> messages, List<ToolSchema> tools, GenerateOptions options) {
        return Flux.defer(
            () -> {
              tasks.checkDispatch(claim);
              return Flux.just(
                  ChatResponse.builder()
                      .id(claim.command().attemptId())
                      .content(
                          List.of(
                              TextBlock.builder().text("已收到：" + claim.turn().message()).build()))
                      .build());
            });
      }
    };
  }

  public Outcome execute(Claim claim) {
    tasks.checkDispatch(claim);
    var release = tasks.releases().require(claim.turn().releaseHash());
    var scope = claim.scope();
    var store =
        new MyBatisAgentStateStore(states, scope) {
          @Override
          public long saveIfVersion(
              String user, String session, String key, State state, long expected) {
            return tasks.fenced(
                claim, () -> super.saveIfVersion(user, session, key, state, expected));
          }
        };
    var stop = Sinks.<Void>empty();
    try (var tools = new GovernedTools(tasks, governance, claim);
        var provider =
            release.primary().name().equals("offline")
                ? null
                : ModelFactory.create(release.primary(), tasks.releases().apiKey());
        var backup =
            release.fallback() == null
                ? null
                : ModelFactory.create(release.fallback(), tasks.releases().apiKey())) {
      Model delegate = release.primary().name().equals("offline") ? model(claim) : provider;
      tools.business(release.primary().name().equals("offline") ? null : provider, states);
      boolean workflow =
          claim.turn().message().startsWith(com.financeclaw.tools.PortfolioReview.COMMAND);
      if (workflow)
        delegate = new com.financeclaw.tools.PortfolioReview(tasks, governance, claim).driver();
      Model primary = guarded(claim, delegate, stop, !workflow);
      Model fallback =
          release.fallback() == null || workflow ? null : guarded(claim, backup, stop, true);
      try (var agent =
          IsolatedAgentFactory.create(
              primary, fallback, tools.toolkit, store, tools.permissions(), tools::configure)) {
        active.put(claim.command().attemptId(), new Active(agent, stop));
        List<Msg> inputs = inputs(claim, store, tools);
        agent
            .streamEvents(
                inputs,
                io.agentscope.core.agent.RuntimeContext.builder(IsolatedAgentFactory.context(scope))
                    .put(ExecutionIdentity.class, tasks.identity(claim))
                    .build())
            .publishOn(reactor.core.scheduler.Schedulers.boundedElastic(), 1)
            .doOnNext(
                event -> {
                  if (event instanceof TextBlockDeltaEvent text
                      && text.getDelta() != null
                      && !text.getDelta().isEmpty())
                    tasks.progress(claim, "text_delta", text.getDelta(), null);
                  else if (event instanceof ToolCallStartEvent tool
                      && agent.getToolkit().getToolNames().contains(tool.getToolCallName()))
                    tasks.progress(claim, "tool_started", null, tool.getToolCallName());
                  else if (event instanceof ToolResultEndEvent tool
                      && agent.getToolkit().getToolNames().contains(tool.getToolCallName()))
                    tasks.progress(
                        claim,
                        tool.getState() == ToolResultState.SUCCESS
                            ? "tool_completed"
                            : "tool_failed",
                        null,
                        tool.getToolCallName());
                })
            .blockLast(timeout);
        var saved =
            store.getVersioned(scope.userId(), scope.sessionId(), "agent_state", AgentState.class);
        // Policy failures inside SDK tools can become error results. Recheck before committing.
        tasks.fenced(
            claim,
            () -> {
              tasks.checkPolicy(claim);
              return null;
            });
        var outcome = classify(saved.value(), saved.version());
        return outcome.status().equals("waiting")
            ? new Outcome("waiting", null, saved.version(), tools.pending(saved.value()))
            : outcome;
      }
    } finally {
      active.remove(claim.command().attemptId());
    }
  }

  private Model guarded(Claim claim, Model delegate, Sinks.Empty<Void> stop, boolean countModel) {
    return new Model() {
      @Override
      public String getModelName() {
        return delegate.getModelName();
      }

      @Override
      public Flux<ChatResponse> stream(
          List<Msg> messages, List<ToolSchema> tools, GenerateOptions options) {
        return Flux.defer(
                () -> {
                  if (countModel) tasks.consume(claim, true);
                  return delegate.stream(messages, tools, options);
                })
            .takeUntilOther(stop.asMono());
      }
    };
  }

  private List<Msg> inputs(Claim claim, MyBatisAgentStateStore store, GovernedTools tools) {
    if (claim.command().resumeId() == null) {
      var profile = tasks.releases().require(claim.turn().releaseHash()).primary();
      int budget =
          Math.min(
              8192,
              Math.max(
                  0,
                  (Math.min(profile.inputLimit(), profile.window() - profile.outputReserve())
                          - profile.margin())
                      / 4));
      List<Msg> history = new ArrayList<>();
      int used = 0;
      for (var previous : tasks.history(claim)) {
        int bytes =
            ResumeInputs.canonical(List.of(previous.message(), previous.result()))
                .getBytes(java.nio.charset.StandardCharsets.UTF_8)
                .length;
        if (used + bytes > budget) break;
        history.addFirst(
            Msg.builder()
                .name("assistant")
                .role(MsgRole.ASSISTANT)
                .textContent(previous.result())
                .build());
        history.addFirst(
            Msg.builder().name("user").role(MsgRole.USER).textContent(previous.message()).build());
        used += bytes;
      }
      history.add(
          Msg.builder()
              .name("user")
              .role(MsgRole.USER)
              .textContent(claim.turn().message())
              .build());
      return history;
    }
    tasks.checkDispatch(claim);
    {
      var anchor = tasks.governance().interaction(claim.command().resumeId());
      if (anchor == null || !anchor.status().equals("answered") || !anchor.live())
        throw new Blocked("stale_interaction");
      var source =
          new Claim(
                  new Command(
                      anchor.commandId(),
                      anchor.turnId(),
                      "done",
                      anchor.attemptId(),
                      0,
                      false,
                      null),
                  claim.turn())
              .scope();
      var original = new MyBatisAgentStateStore(states, source);
      var saved =
          original.getVersioned(
              source.userId(), source.sessionId(), "agent_state", AgentState.class);
      if (!saved.isPresent() || saved.version() != anchor.stateVersion())
        throw new Blocked("checkpoint_mismatch");
      List<Msg> replies = new ArrayList<>();
      for (var i : tasks.governance().interactions(claim.turn().id()))
        if (i.attemptId().equals(anchor.attemptId())) {
          if (!i.status().equals("answered")
              || !i.live()
              || i.grantRevision() != anchor.grantRevision())
            throw new Blocked("stale_interaction");
          var answer = GovernanceService.object(i.response());
          replies.add(
              i.kind().equals("approval")
                  ? ResumeInputs.approval(
                      original,
                      source,
                      i.stateVersion(),
                      i.callId(),
                      i.actionHash(),
                      "approve".equals(answer.get("decision")))
                  : tools.resumeInput(original, source, i, answer.get("answer")));
        }
      var state = saved.value();
      var copied =
          AgentState.builder()
              .userId(claim.scope().userId())
              .sessionId(claim.scope().sessionId())
              .context(state.getContext())
              .summary(state.getSummary())
              .permissionContext(tools.permissions())
              .toolContext(state.getToolContext())
              .tasksContext(state.getTasksContext())
              .planModeContext(state.getPlanModeContext())
              .build();
      store.saveIfVersion(
          claim.scope().userId(), claim.scope().sessionId(), "agent_state", copied, 0);
      tasks.checkDispatch(claim);
      return replies;
    }
  }

  public void interrupt(Claim claim) {
    var agent = active.get(claim.command().attemptId());
    if (agent != null) {
      agent.agent().interrupt(IsolatedAgentFactory.context(claim.scope()));
      // SDK signals alone are only observed between model chunks; also cancel an idle stream.
      agent.stop().tryEmitEmpty();
    }
  }

  public static Outcome classify(AgentState state, long version) {
    if (state == null || state.isShutdownInterrupted()) return new Outcome("failed", null, version);
    Map<String, ToolUseBlock> calls = new HashMap<>();
    Map<String, ToolResultBlock> results = new HashMap<>();
    for (Msg msg : state.getContext()) {
      msg.getContentBlocks(ToolUseBlock.class).forEach(t -> calls.put(t.getId(), t));
      msg.getContentBlocks(ToolResultBlock.class).forEach(t -> results.put(t.getId(), t));
    }
    for (String id : calls.keySet()) {
      ToolResultBlock r = results.get(id);
      if (r == null
          || r.isSuspended()
          || r.getState() == ToolResultState.RUNNING
          || (calls.get(id).getState() == ToolCallState.ASKING
              && r.getState() == ToolResultState.SUCCESS))
        return new Outcome("waiting", null, version);
    }
    if (results.values().stream()
        .anyMatch(
            r ->
                r.getState() == ToolResultState.ERROR
                    || r.getState() == ToolResultState.INTERRUPTED
                    || r.getState() == ToolResultState.DENIED))
      return new Outcome("failed", null, version);
    var context = state.getContext();
    if (context.isEmpty()) return new Outcome("failed", null, version);
    Msg last = context.getLast();
    String text = last.getTextContent();
    if (last.getRole() != MsgRole.ASSISTANT || text == null || text.isBlank())
      return new Outcome("failed", null, version);
    return new Outcome("completed", text, version);
  }
}
