package com.financeclaw.architecture;

import static org.assertj.core.api.Assertions.*;
import static org.mockito.ArgumentMatchers.*;
import static org.mockito.Mockito.*;

import com.financeclaw.infrastructure.agentscope.IsolatedAgentFactory;
import io.agentscope.core.ReActAgent;
import io.agentscope.core.agent.RuntimeContext;
import io.agentscope.core.agent.StreamOptions;
import io.agentscope.core.message.*;
import io.agentscope.core.permission.*;
import io.agentscope.core.state.*;
import io.agentscope.core.tool.*;
import io.agentscope.core.tool.subagent.*;
import java.time.Duration;
import java.util.*;
import java.util.concurrent.atomic.AtomicInteger;
import org.junit.jupiter.api.Test;
import reactor.core.publisher.Mono;

/**
 * Fixed-SDK entry-point probes, not an alternative production research executor.
 *
 * <p>The actual SubAgentTool executes here. Agent/storage doubles expose which identity, lifecycle
 * and error contracts it supplies. This does not prove ReAct state recovery, PostgreSQL fencing,
 * shared business budgets, or a durable completion receipt.
 */
class SubAgentToolCompatibilityTest {
  private static final Duration WAIT = Duration.ofSeconds(3);
  private static final String MODEL_SESSION = "model-selected-session";
  private static final RuntimeContext SERVER_CONTEXT =
      RuntimeContext.builder().userId("server-user").sessionId("server-session").build();

  @Test
  void constructionAllocatesAnAgentAndTheNativeToolCannotPassOurPermissionGate() {
    var allocationCount = new AtomicInteger();
    var sample = mock(ReActAgent.class);
    var executing = respondingAgent();
    var tool =
        new SubAgentTool(
            () -> allocationCount.incrementAndGet() == 1 ? sample : executing, config(null));

    assertThat(allocationCount).hasValue(1);
    var toolkit = new Toolkit();
    toolkit.registerAgentTool(tool);
    var permission =
        PermissionContextState.builder()
            .mode(PermissionMode.DEFAULT)
            .addAllowRule(
                "probe_child",
                new PermissionRule("probe_child", null, PermissionBehavior.ALLOW, "probe"))
            .build();
    assertThatThrownBy(
            () ->
                IsolatedAgentFactory.create(
                    null, toolkit, new InMemoryAgentStateStore(), permission))
        .isInstanceOf(IllegalArgumentException.class)
        .hasMessage("Tool must extend ToolBase for permission evaluation: probe_child");

    assertThat(text(tool.callAsync(param(null)).block(WAIT)))
        .isEqualTo("session_id: " + MODEL_SESSION + "\n\nchild answer");
    assertThat(allocationCount).hasValue(2);
    verifyNoInteractions(sample);
    verify(executing).call(anyList(), same(SERVER_CONTEXT));
    verify(executing, never()).close();
  }

  @Test
  void modelSessionSelectsAnonymousUnversionedStorageDespiteTheServerRuntimeIdentity() {
    var agent = respondingAgent();
    var savedState = AgentState.builder().build();
    when(agent.getAgentState()).thenReturn(savedState);
    var store = mock(AgentStateStore.class);
    when(store.get(null, MODEL_SESSION, "agent_state", AgentState.class))
        .thenReturn(Optional.empty());
    var tool = new SubAgentTool(() -> agent, config(store));

    assertThat(tool.getParameters().get("properties"))
        .asInstanceOf(org.assertj.core.api.InstanceOfAssertFactories.MAP)
        .containsKeys("session_id", "message");
    var result = tool.callAsync(param(null)).block(WAIT);

    assertThat(text(result)).isEqualTo("session_id: " + MODEL_SESSION + "\n\nchild answer");
    verify(agent).call(anyList(), same(SERVER_CONTEXT));
    verify(store).get(null, MODEL_SESSION, "agent_state", AgentState.class);
    verify(store).save(null, MODEL_SESSION, "agent_state", savedState);
    verifyNoMoreInteractions(store);
    assertThat(SERVER_CONTEXT.getUserId()).isEqualTo("server-user");
    assertThat(SERVER_CONTEXT.getSessionId()).isEqualTo("server-session");
  }

  @Test
  void rejectedStateReadAndWriteDoNotStopExecutionOrInvalidateItsNormalResult() {
    var agent = respondingAgent();
    var savedState = AgentState.builder().build();
    when(agent.getAgentState()).thenReturn(savedState);
    var store = mock(AgentStateStore.class);
    when(store.get(null, MODEL_SESSION, "agent_state", AgentState.class))
        .thenThrow(new SecurityException("probe: bound scope rejected"));
    doThrow(new UnsupportedOperationException("probe: unversioned write rejected"))
        .when(store)
        .save(null, MODEL_SESSION, "agent_state", savedState);
    var tool = new SubAgentTool(() -> agent, config(store));

    var result = tool.callAsync(param(null)).block(WAIT);

    assertThat(text(result)).isEqualTo("session_id: " + MODEL_SESSION + "\n\nchild answer");
    assertThat(result.getState()).isNotEqualTo(ToolResultState.ERROR);
    verify(agent).call(anyList(), same(SERVER_CONTEXT));
    verify(store).get(null, MODEL_SESSION, "agent_state", AgentState.class);
    verify(store).save(null, MODEL_SESSION, "agent_state", savedState);
  }

  @Test
  void executionFailureReturnsInternalErrorTextAndStillInvokesTheUnversionedSave() {
    var agent = mock(ReActAgent.class);
    when(agent.call(anyList(), same(SERVER_CONTEXT)))
        .thenReturn(Mono.error(new IllegalStateException("probe-private-diagnostic")));
    var savedState = AgentState.builder().build();
    when(agent.getAgentState()).thenReturn(savedState);
    var store = mock(AgentStateStore.class);
    when(store.get(null, MODEL_SESSION, "agent_state", AgentState.class))
        .thenReturn(Optional.empty());
    var tool = new SubAgentTool(() -> agent, config(store));

    var result = tool.callAsync(param(null)).block(WAIT);

    assertThat(result.getState()).isEqualTo(ToolResultState.ERROR);
    assertThat(text(result)).contains("probe-private-diagnostic");
    verify(store).save(null, MODEL_SESSION, "agent_state", savedState);
  }

  @Test
  void disablingEventForwardingUsesCallWithoutEmittingButStillReturnsTheSession() {
    assertThat(SubAgentConfig.defaults().isForwardEvents()).isTrue();
    var agent = respondingAgent();
    var emitted = new ArrayList<ToolResultBlock>();
    var tool = new SubAgentTool(() -> agent, config(null));

    var result = tool.callAsync(param(emitted::add)).block(WAIT);

    assertThat(emitted).isEmpty();
    assertThat(text(result)).isEqualTo("session_id: " + MODEL_SESSION + "\n\nchild answer");
    verify(agent).call(anyList(), same(SERVER_CONTEXT));
    verify(agent, never()).stream(anyList(), any(StreamOptions.class), any(RuntimeContext.class));
  }

  @Test
  void disposingTheSubscriptionCancelsTheChildAndDoesNotSaveAResult() {
    var agent = mock(ReActAgent.class);
    var subscriptions = new AtomicInteger();
    var cancellations = new AtomicInteger();
    when(agent.call(anyList(), same(SERVER_CONTEXT)))
        .thenReturn(
            Mono.<Msg>never()
                .doOnSubscribe(ignored -> subscriptions.incrementAndGet())
                .doOnCancel(cancellations::incrementAndGet));
    var store = mock(AgentStateStore.class);
    when(store.get(null, MODEL_SESSION, "agent_state", AgentState.class))
        .thenReturn(Optional.empty());
    var tool = new SubAgentTool(() -> agent, config(store));
    var outputs = new ArrayList<ToolResultBlock>();
    var errors = new ArrayList<Throwable>();

    var subscription = tool.callAsync(param(null)).subscribe(outputs::add, errors::add);
    assertThat(subscriptions).hasValue(1);
    subscription.dispose();

    assertThat(cancellations).hasValue(1);
    assertThat(outputs).isEmpty();
    assertThat(errors).isEmpty();
    verify(agent).interrupt(SERVER_CONTEXT);
    verify(agent, never()).close();
    verify(store).get(null, MODEL_SESSION, "agent_state", AgentState.class);
    verifyNoMoreInteractions(store);
  }

  private static ReActAgent respondingAgent() {
    var agent = mock(ReActAgent.class);
    when(agent.call(anyList(), same(SERVER_CONTEXT)))
        .thenReturn(
            Mono.just(Msg.builder().role(MsgRole.ASSISTANT).textContent("child answer").build()));
    return agent;
  }

  private static SubAgentConfig config(AgentStateStore store) {
    return SubAgentConfig.builder()
        .toolName("probe_child")
        .description("Test-only SDK compatibility probe")
        .forwardEvents(false)
        .stateStore(store)
        .build();
  }

  private static ToolCallParam param(ToolEmitter emitter) {
    return ToolCallParam.builder()
        .input(Map.of("session_id", MODEL_SESSION, "message", "Research the authorized symbols"))
        .runtimeContext(SERVER_CONTEXT)
        .emitter(emitter)
        .build();
  }

  private static String text(ToolResultBlock result) {
    assertThat(result).isNotNull();
    assertThat(result.getOutput()).hasSize(1);
    assertThat(result.getOutput().getFirst()).isInstanceOf(TextBlock.class);
    return ((TextBlock) result.getOutput().getFirst()).getText();
  }
}
