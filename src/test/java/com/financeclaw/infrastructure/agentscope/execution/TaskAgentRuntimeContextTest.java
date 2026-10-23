package com.financeclaw.infrastructure.agentscope.execution;

import static org.assertj.core.api.Assertions.*;
import static org.mockito.ArgumentMatchers.*;
import static org.mockito.Mockito.*;

import com.financeclaw.app.artifact.ArtifactService;
import com.financeclaw.app.execution.ExecutionLeaseService;
import com.financeclaw.app.execution.ExecutionPolicyService;
import com.financeclaw.app.execution.port.ExecutionContextGateway;
import com.financeclaw.app.release.ReleaseService;
import com.financeclaw.app.release.port.Credentials;
import com.financeclaw.app.report.ReportService;
import com.financeclaw.app.task.TaskEventHub;
import com.financeclaw.domain.Governance.*;
import com.financeclaw.domain.Task.*;
import com.financeclaw.domain.release.*;
import com.financeclaw.infrastructure.agentscope.capability.BusinessCapabilities;
import com.financeclaw.infrastructure.agentscope.capability.ToolContribution;
import com.financeclaw.infrastructure.state.RuntimeMapper;
import com.financeclaw.infrastructure.state.RuntimeMapper.StateKey;
import com.financeclaw.infrastructure.state.RuntimeMapper.StateRow;
import io.agentscope.core.message.*;
import io.agentscope.core.model.*;
import java.util.List;
import java.util.Map;
import java.util.concurrent.ConcurrentHashMap;
import java.util.concurrent.CopyOnWriteArrayList;
import java.util.function.Supplier;
import java.util.stream.Collectors;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;
import reactor.core.publisher.Flux;
import reactor.core.publisher.Mono;

class TaskAgentRuntimeContextTest {
    private final ExecutionPolicyService policy = mock(ExecutionPolicyService.class);
    private final ExecutionLeaseService leases = mock(ExecutionLeaseService.class);
    private final ReleaseService releases = mock(ReleaseService.class);
    private final ExecutionContextGateway interactions = mock(ExecutionContextGateway.class);
    private final RuntimeMapper states = mock(RuntimeMapper.class);
    private final BusinessCapabilities capabilities = mock(BusinessCapabilities.class);
    private final Map<StateKey, StateRow> snapshots = new ConcurrentHashMap<>();
    private final List<List<Msg>> modelRequests = new CopyOnWriteArrayList<>();
    private final Claim claim = claim(null);

    @BeforeEach
    void setup() {
        when(releases.require("release")).thenReturn(release(ReleaseDefinition.OFFLINE));
        when(policy.memoryContext(any())).thenReturn("");
        when(policy.scopes(any())).thenReturn(List.of());
        when(policy.history(any())).thenReturn(List.of());
        var events = new TaskEventHub();
        when(policy.beginProgress(any())).thenAnswer(i -> events.begin(i.getArgument(0)));
        when(policy.identity(any()))
                .thenAnswer(
                        i -> {
                            Claim c = i.getArgument(0);
                            return new ExecutionIdentity(
                                    c.turn().tenant(),
                                    c.turn().subject(),
                                    c.turn().conversationId(),
                                    c.turn().id(),
                                    c.command().attemptId(),
                                    "release",
                                    1,
                                    List.of(),
                                    "2026-10-23T00:00:00Z");
                        });
        when(leases.fenced(any(), any())).thenAnswer(i -> ((Supplier<?>) i.getArgument(1)).get());
        when(states.getState(any())).thenAnswer(i -> snapshots.get(i.getArgument(0)));
        when(states.insertStateIfAbsent(any(), anyString()))
                .thenAnswer(
                        i ->
                                snapshots.putIfAbsent(
                                                        i.getArgument(0),
                                                        new StateRow(i.getArgument(1), 1, false))
                                                == null
                                        ? 1
                                        : 0);
        when(states.updateStateByExpectedVersion(any(), anyString(), anyLong()))
                .thenAnswer(
                        i -> {
                            StateKey key = i.getArgument(0);
                            StateRow old = snapshots.get(key);
                            long expected = i.getArgument(2);
                            return old != null
                                            && old.version() == expected
                                            && snapshots.replace(
                                                    key,
                                                    old,
                                                    new StateRow(
                                                            i.getArgument(1), expected + 1, false))
                                    ? 1
                                    : 0;
                        });
    }

    @Test
    void profileAndEvidenceAreLoadedOnceAcrossNativeModelRoundsAndNeverStoredAsHistory() {
        String userContext =
                "\nUser-maintained profile:\n# PROFILE_PRIVATE_MARKER\n偏好中文\n"
                        + "Evidence: FACT_PRIVATE_MARKER source=prior-turn revision=1";
        when(policy.memoryContext(claim)).thenReturn(userContext);
        doAnswer(
                        i -> {
                            ToolContribution.Registrar registrar = i.getArgument(3);
                            registrar.tool(
                                    "context_probe",
                                    GovernedTools.EMPTY,
                                    null,
                                    p -> Mono.just(ToolResultBlock.text("probe completed")));
                            return null;
                        })
                .when(capabilities)
                .contribute(eq(claim), isNull(), eq(false), any());

        var result = runtime(true).execute(claim);

        assertThat(result.status()).isEqualTo("completed");
        assertThat(modelRequests).hasSize(2);
        for (var request : modelRequests) {
            String system = systemText(request);
            assertThat(system).contains(userContext);
            assertThat(system.indexOf("PROFILE_PRIVATE_MARKER"))
                    .isEqualTo(system.lastIndexOf("PROFILE_PRIVATE_MARKER"));
        }
        verify(policy, times(1)).memoryContext(claim);
        verify(policy, times(2)).consume(claim, true);
        verify(policy).consumeTool(claim, null);
        verify(policy).checkPolicy(claim);
        assertThat(snapshots).isNotEmpty();
        assertThat(snapshots.values())
                .allSatisfy(
                        state ->
                                assertThat(state.payload())
                                        .doesNotContain(
                                                "PROFILE_PRIVATE_MARKER", "FACT_PRIVATE_MARKER")
                                        .contains("current request"));
    }

    @Test
    void historyUsesOnlyCapacityRemainingAfterTheCompleteUtf8Profile() {
        when(releases.require("release"))
                .thenReturn(release(new ModelProfile("offline", "", 8192, 16384, 128, 0, false)));
        String userContext = "中文".repeat(1000); // 6000 bytes, not 2000 characters.
        when(policy.memoryContext(claim)).thenReturn(userContext);
        when(policy.history(claim))
                .thenReturn(
                        List.of(
                                history(
                                        "recent",
                                        "RECENT_" + "a".repeat(80),
                                        "RECENT_REPLY_" + "b".repeat(80)),
                                history(
                                        "older",
                                        "OLDER_" + "c".repeat(300),
                                        "OLDER_REPLY_" + "d".repeat(300))));

        runtime(false).execute(claim);

        assertThat(modelRequests).hasSize(1);
        var messages = modelRequests.getFirst();
        assertThat(systemText(messages)).contains(userContext);
        String conversation =
                messages.stream()
                        .filter(m -> m.getRole() != MsgRole.SYSTEM)
                        .map(Msg::getTextContent)
                        .collect(Collectors.joining("\n"));
        assertThat(conversation)
                .contains("RECENT_", "RECENT_REPLY_", "current request")
                .doesNotContain("OLDER_", "OLDER_REPLY_");
    }

    @Test
    void oversizedProfileFailsBeforeNativeStateToolsOrModelDispatch() {
        when(releases.require("release"))
                .thenReturn(release(new ModelProfile("offline", "", 1024, 2048, 128, 0, false)));
        when(policy.memoryContext(claim)).thenReturn("中".repeat(350));

        assertThatThrownBy(() -> runtime(false).execute(claim))
                .isInstanceOf(Blocked.class)
                .hasMessage("context_budget_exceeded");

        assertThat(modelRequests).isEmpty();
        verifyNoInteractions(states, capabilities, interactions);
        verify(policy, never()).history(any());
        verify(policy, never()).consume(any(), anyBoolean());
    }

    @Test
    void changedProfileRejectsResumeBeforeReadingOrCopyingItsOldCheckpoint() {
        Claim resume = claim("old-interaction");
        when(policy.memoryContext(resume)).thenReturn("profile loaded before the concurrent edit");
        doNothing().doThrow(new Blocked("profile_changed")).when(policy).checkDispatch(resume);

        assertThatThrownBy(() -> runtime(false).execute(resume))
                .isInstanceOf(Blocked.class)
                .hasMessage("profile_changed");

        var order = inOrder(policy);
        order.verify(policy).beginProgress(resume);
        order.verify(policy).checkDispatch(resume);
        order.verify(policy).memoryContext(resume);
        order.verify(policy).checkDispatch(resume);
        verifyNoInteractions(states, interactions);
        assertThat(modelRequests).isEmpty();
    }

    private TaskAgentRuntime runtime(boolean twoRounds) {
        return new TaskAgentRuntime(
                policy,
                leases,
                releases,
                mock(Credentials.class),
                interactions,
                mock(ArtifactService.class),
                mock(ReportService.class),
                states,
                capabilities,
                5000) {
            @Override
            protected Model model(Claim current) {
                return new Model() {
                    public String getModelName() {
                        return "context-capture";
                    }

                    public Flux<ChatResponse> stream(
                            List<Msg> messages, List<ToolSchema> tools, GenerateOptions options) {
                        modelRequests.add(List.copyOf(messages));
                        ContentBlock response =
                                twoRounds && modelRequests.size() == 1
                                        ? ToolUseBlock.builder()
                                                .id("probe-1")
                                                .name("context_probe")
                                                .input(Map.of())
                                                .content("{}")
                                                .build()
                                        : TextBlock.builder().text("completed").build();
                        return Flux.just(
                                ChatResponse.builder()
                                        .id("answer-" + modelRequests.size())
                                        .content(List.of(response))
                                        .build());
                    }
                };
            }
        };
    }

    private static String systemText(List<Msg> messages) {
        return messages.stream()
                .filter(m -> m.getRole() == MsgRole.SYSTEM)
                .map(Msg::getTextContent)
                .collect(Collectors.joining("\n"));
    }

    private static ReleaseDefinition release(ModelProfile model) {
        return new ReleaseDefinition(
                "context-test",
                "finance_agent",
                List.of(),
                model,
                null,
                8,
                8,
                new BusinessFeatures(List.of(), "", "{}", null));
    }

    private static Claim claim(String resume) {
        return new Claim(
                new Command("command", "turn", "running", "attempt", 1, true, resume),
                new Turn(
                        "turn",
                        "conversation",
                        "tenant",
                        "alice",
                        "key",
                        "hash",
                        "current request",
                        "release",
                        "running",
                        2,
                        null,
                        null));
    }

    private static Turn history(String id, String message, String result) {
        return new Turn(
                id,
                "conversation",
                "tenant",
                "alice",
                "key-" + id,
                "hash",
                message,
                "release",
                "completed",
                3,
                result,
                null);
    }
}
