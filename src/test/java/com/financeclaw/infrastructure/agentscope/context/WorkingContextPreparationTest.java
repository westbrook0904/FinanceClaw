package com.financeclaw.infrastructure.agentscope.context;

import static org.assertj.core.api.Assertions.*;
import static org.mockito.Mockito.*;

import com.financeclaw.app.artifact.ArtifactService;
import com.financeclaw.domain.Task.*;
import com.financeclaw.domain.release.ReleaseDefinition;
import com.financeclaw.infrastructure.agentscope.context.WorkingContextCompactor.SummaryRequest;
import com.financeclaw.infrastructure.state.RuntimeMapper;
import io.agentscope.core.agent.Agent;
import io.agentscope.core.agent.RuntimeContext;
import io.agentscope.core.message.*;
import io.agentscope.core.middleware.*;
import io.agentscope.core.model.Model;
import io.agentscope.core.state.AgentState;
import java.time.Duration;
import java.util.*;
import java.util.concurrent.CopyOnWriteArrayList;
import org.junit.jupiter.api.Test;
import reactor.core.publisher.Flux;

/** 双重模型前入口只能替换同一份投影，不能重复摘要或让系统长期记忆流入摘要请求。 */
class WorkingContextPreparationTest {
    @Test
    void reasoningAndModelHooksShareOneProjectionAndExcludeLongMemoryFromSummarizer() {
        String privateMemory = "PROFILE_PRIVATE_MARKER\nFACT_PRIVATE_MARKER";
        var claim = claim();
        var mapper = mock(RuntimeMapper.class);
        var session = new WorkingContextSession(mapper, claim, 1);
        var system =
                Msg.builder().role(MsgRole.SYSTEM).textContent("policy\n" + privateMemory).build();
        var oldInput = user("old-user", "old question".repeat(300));
        var oldAnswer = assistant("old answer".repeat(300));
        var current = user(session.inputId(), "current user request");
        var recent = assistant("most recent completed observation");
        var state =
                AgentState.builder().context(List.of(oldInput, oldAnswer, current, recent)).build();
        var context = RuntimeContext.builder().userId("owner").sessionId("scope").build();
        context.setAgentState(state);
        List<SummaryRequest> summaries = new CopyOnWriteArrayList<>();
        var middleware =
                new WorkingContextMiddleware(
                        claim,
                        ReleaseDefinition.OFFLINE,
                        new ContextBudgets(8192, 16384, 9000, 8192, 16384, 8192, 1500, 50, 1),
                        privateMemory,
                        session,
                        new ToolResultOffloader(mock(ArtifactService.class), 8192),
                        name -> null,
                        request -> {
                            summaries.add(request);
                            return new WorkContext(
                                    List.of("old goal"),
                                    List.of(),
                                    List.of("old evidence"),
                                    List.of("continue"));
                        });
        List<List<Msg>> forwarded = new CopyOnWriteArrayList<>();
        var initial = new ArrayList<Msg>();
        initial.add(system);
        initial.addAll(state.getContext());
        var agent = mock(Agent.class);
        middleware
                .onReasoning(
                        agent,
                        context,
                        new ReasoningInput(initial, List.of(), null),
                        input -> {
                            forwarded.add(input.messages());
                            return Flux.empty();
                        })
                .blockLast(Duration.ofSeconds(5));
        middleware
                .onModelCall(
                        agent,
                        context,
                        new ModelCallInput(
                                forwarded.getFirst(), List.of(), null, mock(Model.class)),
                        input -> {
                            forwarded.add(input.messages());
                            return Flux.empty();
                        })
                .blockLast(Duration.ofSeconds(5));

        assertThat(summaries).hasSize(1);
        assertThat(summaries.getFirst().completedMessages()).containsExactly(oldInput, oldAnswer);
        assertThat(WorkingContextSession.encode(summaries.getFirst().completedMessages()))
                .doesNotContain("PROFILE_PRIVATE_MARKER", "FACT_PRIVATE_MARKER");
        assertThat(forwarded).hasSize(2);
        assertThat(forwarded.getFirst()).containsExactlyElementsOf(forwarded.getLast());
        assertThat(forwarded.getLast().getFirst()).isSameAs(system);
        assertThat(state.getContext())
                .contains(current, recent)
                .doesNotContain(oldInput, oldAnswer, system);
        assertThat(state.getContext().stream().filter(WorkingContextCompactor::isSummary))
                .hasSize(1);
        assertThat(WorkingContextSession.encode(state.getContext())).doesNotContain(privateMemory);
        verifyNoInteractions(mapper);
    }

    private static Msg user(String id, String text) {
        return Msg.builder().id(id).name("user").role(MsgRole.USER).textContent(text).build();
    }

    private static Msg assistant(String text) {
        return Msg.builder().name("assistant").role(MsgRole.ASSISTANT).textContent(text).build();
    }

    private static Claim claim() {
        return new Claim(
                new Command("command", "turn", "running", "attempt", 1, true, null),
                new Turn(
                        "turn",
                        "conversation",
                        "tenant",
                        "alice",
                        "key",
                        "hash",
                        "current user request",
                        "release",
                        "running",
                        2,
                        null,
                        null));
    }
}
