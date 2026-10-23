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
import com.financeclaw.infrastructure.agentscope.context.WorkingContextCompactor;
import com.financeclaw.infrastructure.agentscope.context.WorkingContextSession;
import com.financeclaw.infrastructure.state.RuntimeMapper;
import com.financeclaw.infrastructure.state.RuntimeMapper.StateKey;
import com.financeclaw.infrastructure.state.RuntimeMapper.StateRow;
import com.financeclaw.infrastructure.state.RuntimeMapper.WorkRow;
import com.financeclaw.support.ArtifactPayload;
import com.financeclaw.support.PayloadJson;
import io.agentscope.core.message.*;
import io.agentscope.core.model.*;
import java.util.*;
import java.util.concurrent.ConcurrentHashMap;
import java.util.concurrent.CopyOnWriteArrayList;
import java.util.concurrent.atomic.AtomicInteger;
import java.util.concurrent.atomic.AtomicLong;
import java.util.function.Function;
import java.util.function.Supplier;
import java.util.stream.Collectors;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;
import org.springframework.mock.env.MockEnvironment;
import reactor.core.publisher.Flux;
import reactor.core.publisher.Mono;

/** 真实 SDK 循环与恢复，持久层用带版本的状态/工作上下文契约替身；不调用外部模型。 */
class TaskAgentRuntimeContextTest {
    private final ExecutionPolicyService policy = mock(ExecutionPolicyService.class);
    private final ExecutionLeaseService leases = mock(ExecutionLeaseService.class);
    private final ReleaseService releases = mock(ReleaseService.class);
    private final ExecutionContextGateway interactions = mock(ExecutionContextGateway.class);
    private final RuntimeMapper states = mock(RuntimeMapper.class);
    private final BusinessCapabilities capabilities = mock(BusinessCapabilities.class);
    private final ArtifactService artifacts = mock(ArtifactService.class);
    private final Map<StateKey, StateRow> snapshots = new ConcurrentHashMap<>();
    private final Map<String, WorkWrite> work = new ConcurrentHashMap<>();
    private final Set<String> completed = ConcurrentHashMap.newKeySet();
    private final List<Request> modelRequests = new CopyOnWriteArrayList<>();
    private final List<Object> archived = new CopyOnWriteArrayList<>();
    private final AtomicLong sequence = new AtomicLong();
    private final ThreadLocal<Integer> fenceDepth = ThreadLocal.withInitial(() -> 0);
    private final Claim claim = claim("turn", "attempt", null, "current request");

    private record Request(Claim claim, List<Msg> messages, List<ToolSchema> tools) {}

    private record WorkWrite(Claim claim, long grant, long version, WorkRow row, long sequence) {}

    @BeforeEach
    void setup() {
        when(releases.require("release")).thenReturn(release(ReleaseDefinition.OFFLINE));
        when(policy.memoryContext(any())).thenReturn("");
        when(policy.scopes(any())).thenReturn(List.of());
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
        when(leases.fenced(any(), any()))
                .thenAnswer(
                        i -> {
                            fenceDepth.set(fenceDepth.get() + 1);
                            try {
                                return ((Supplier<?>) i.getArgument(1)).get();
                            } finally {
                                fenceDepth.set(fenceDepth.get() - 1);
                            }
                        });
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
        when(states.getLatestCompletedWorkContext(any(), anyLong()))
                .thenAnswer(
                        i -> {
                            Claim current = i.getArgument(0);
                            long grant = i.getArgument(1);
                            return work.values().stream()
                                    .filter(row -> completed.contains(row.claim().turn().id()))
                                    .filter(row -> row.grant() == grant)
                                    .filter(row -> sameConversation(row.claim(), current))
                                    .max(Comparator.comparingLong(WorkWrite::sequence))
                                    .map(WorkWrite::row)
                                    .orElse(null);
                        });
        when(states.getTurnWorkContext(any(), anyString(), anyLong()))
                .thenAnswer(
                        i -> {
                            Turn turn = i.getArgument(0);
                            var row = work.get(turn.id());
                            String attempt = i.getArgument(1);
                            long version = i.getArgument(2);
                            return row == null
                                            || !row.claim().command().attemptId().equals(attempt)
                                            || row.version() != version
                                    ? null
                                    : row.row();
                        });
        doAnswer(
                        i -> {
                            Claim c = i.getArgument(0);
                            long grant = i.getArgument(1), version = i.getArgument(2);
                            var scope = c.scope();
                            var saved =
                                    snapshots.get(
                                            new StateKey(
                                                    scope.namespace(),
                                                    scope.userId(),
                                                    scope.sessionId(),
                                                    "agent_state"));
                            assertThat(fenceDepth.get()).as("工作上下文与 SDK CAS 共用执行栅栏").isPositive();
                            assertThat(saved).isNotNull();
                            assertThat(saved.version()).isEqualTo(version);
                            var row = new WorkRow(i.getArgument(3), i.getArgument(4));
                            work.put(
                                    c.turn().id(),
                                    new WorkWrite(
                                            c, grant, version, row, sequence.incrementAndGet()));
                            return null;
                        })
                .when(states)
                .saveTurnWorkContext(any(), anyLong(), anyLong(), anyString(), anyString());
        when(artifacts.archiveToolResult(any(), any(), nullable(String.class)))
                .thenAnswer(
                        i -> {
                            Claim c = i.getArgument(0);
                            Object payload = i.getArgument(1);
                            archived.add(payload);
                            var encoded = ArtifactPayload.encode(payload);
                            return new Artifact(
                                    "file-" + archived.size(),
                                    c.turn().id(),
                                    c.turn().conversationId(),
                                    c.turn().tenant(),
                                    c.turn().subject(),
                                    "release",
                                    encoded.hash(),
                                    encoded.json(),
                                    encoded.bytes());
                        });
    }

    @Test
    void profileAndEvidenceAreLoadedOnceAcrossNativeRoundsAndExcludedFromEverySavedWorkPayload() {
        String userContext =
                "\nUser profile: PROFILE_PRIVATE_MARKER\n偏好中文\n"
                        + "Evidence: FACT_PRIVATE_MARKER source=prior-turn revision=1";
        when(policy.memoryContext(claim)).thenReturn(userContext);
        registerProbe(input -> "probe completed");
        var result =
                runtime(
                                (c, round, messages) ->
                                        round == 0
                                                ? toolCall(
                                                        "probe-1",
                                                        "context_probe",
                                                        Map.of("query", "inspect this"))
                                                : text("completed"))
                        .execute(claim);
        assertThat(result.status()).isEqualTo("completed");
        assertThat(modelRequests).hasSize(2);
        for (var request : modelRequests) {
            String system = systemText(request.messages());
            assertThat(system).contains(userContext);
            assertThat(system.indexOf("PROFILE_PRIVATE_MARKER"))
                    .isEqualTo(system.lastIndexOf("PROFILE_PRIVATE_MARKER"));
        }
        verify(policy, times(1)).memoryContext(claim);
        verify(policy, times(2)).consume(claim, true);
        verify(policy).consumeTool(claim, null);
        assertThat(work).containsKey(claim.turn().id());
        var saved = work.get(claim.turn().id());
        assertThat(saved.version()).isEqualTo(result.stateVersion());
        assertThat(saved.row().payload() + saved.row().transcript())
                .doesNotContain("PROFILE_PRIVATE_MARKER", "FACT_PRIVATE_MARKER")
                .contains("current request", "inspect this", "probe completed");
        assertThat(snapshots.values())
                .allSatisfy(
                        row ->
                                assertThat(row.payload())
                                        .doesNotContain(
                                                "PROFILE_PRIVATE_MARKER", "FACT_PRIVATE_MARKER"));
    }

    @Test
    void
            nextTurnInheritsToolArgumentsResultsAndLatestCompletedProjectionWithoutReplayingOldActions() {
        var first = claim("first", "attempt-1", null, "first request");
        var second = claim("second", "attempt-2", null, "second request");
        var third = claim("third", "attempt-3", null, "third request");
        registerProbe(input -> "EVIDENCE_" + input.get("query"));
        var execution =
                runtime(
                        (c, round, messages) ->
                                c.turn().id().equals("first") && round == 0
                                        ? toolCall(
                                                "research-first",
                                                "context_probe",
                                                Map.of("query", "symbol=AAPL;period=2026"))
                                        : text("DONE_" + c.turn().id()));
        assertThat(execution.execute(first).status()).isEqualTo("completed");
        completed.add("first");
        assertThat(execution.execute(second).status()).isEqualTo("completed");
        completed.add("second");
        assertThat(execution.execute(third).status()).isEqualTo("completed");
        assertThat(serialized(requestFor(second).messages()))
                .contains(
                        "symbol=AAPL;period=2026",
                        "EVIDENCE_",
                        "research-first",
                        "first request",
                        "DONE_first",
                        "second request");
        assertThat(serialized(requestFor(third).messages()))
                .contains("DONE_second", "second request", "third request");
        verify(policy, times(1)).consumeTool(first, null);
        verify(policy, never()).consumeTool(second, null);
        verify(policy, never()).consumeTool(third, null);
        assertThat(work.get("second").row().transcript())
                .contains("second request", "DONE_second")
                .doesNotContain("research-first", "symbol=AAPL", "first request");
        verify(states).getLatestCompletedWorkContext(second, 1);
        verify(states).getLatestCompletedWorkContext(third, 1);
    }

    @Test
    void successfulButNotBusinessCompletedProjectionIsNotInherited() {
        var first = claim("first", "attempt-1", null, "not sealed");
        var next = claim("next", "attempt-2", null, "new request");
        var execution = runtime((c, round, messages) -> text("DONE_" + c.turn().id()));
        execution.execute(first);
        execution.execute(next);
        assertThat(serialized(requestFor(next).messages()))
                .contains("new request")
                .doesNotContain("not sealed", "DONE_first");
    }

    @Test
    void repeatedProviderResponseAndToolCallIdsStillExecuteEachNewTurnExactlyOnce() {
        var first = claim("first", "attempt-1", null, "first operation");
        var second = claim("second", "attempt-2", null, "second operation");
        var executions = new AtomicInteger();
        registerProbe(input -> "EXECUTION_" + executions.incrementAndGet());
        // 有意复用供应商的 response id(answer-0/answer-1) 及 callId，历史不能冒充本轮结果。
        var execution =
                runtime(
                        (c, round, messages) ->
                                round == 0
                                        ? toolCall(
                                                "provider-call",
                                                "context_probe",
                                                Map.of("query", c.turn().message()))
                                        : text("DONE_" + c.turn().id()));

        assertThat(execution.execute(first).status()).isEqualTo("completed");
        completed.add("first");
        assertThat(execution.execute(second).status()).isEqualTo("completed");

        assertThat(executions).hasValue(2);
        assertThat(modelRequests.stream().filter(request -> request.claim().equals(second)))
                .hasSize(2);
        verify(policy).consumeTool(first, null);
        verify(policy).consumeTool(second, null);
        assertThat(serialized(requestFor(second).messages()))
                .contains("EXECUTION_1", "first operation");
        assertThat(work.get("second").row().transcript())
                .contains("provider-call", "second operation", "EXECUTION_2", "DONE_second")
                .doesNotContain("EXECUTION_1", "first operation");
    }

    @Test
    void midTurnCompactionKeepsUserAnchorAndEveryOriginalToolRoundInTranscript() {
        registerProbe(input -> "RAW_ROUND_" + input.get("query") + "x".repeat(1600));
        var execution =
                runtime(
                        (c, round, messages) ->
                                round < 4
                                        ? toolCall(
                                                "call-" + round,
                                                "context_probe",
                                                Map.of("query", "round-" + round))
                                        : text("completed"));
        execution.configureContext(smallWorkBudget());
        var outcome = execution.execute(claim);
        assertThat(outcome.status()).isEqualTo("completed");
        assertThat(modelRequests).hasSize(5);
        assertThat(
                        modelRequests.stream()
                                .flatMap(request -> request.messages().stream())
                                .anyMatch(WorkingContextCompactor::isSummary))
                .isTrue();
        for (var request : modelRequests) {
            assertThat(
                            request.messages().stream()
                                    .filter(message -> message.getId().equals("turn-input-turn")))
                    .hasSize(1);
            assertThat(
                            request.messages().stream()
                                    .filter(WorkingContextCompactor::isSummary)
                                    .count())
                    .isLessThanOrEqualTo(1);
        }
        var row = work.get("turn").row();
        assertThat(row.transcript())
                .contains(
                        "call-0",
                        "call-1",
                        "call-2",
                        "call-3",
                        "RAW_ROUND_round-0",
                        "RAW_ROUND_round-1",
                        "RAW_ROUND_round-2",
                        "RAW_ROUND_round-3");
        assertThat(row.transcript()).doesNotContain(WorkingContextCompactor.SUMMARY_NAME);
        assertThat(WorkingContextCompactor.bytes(WorkingContextSession.decode(row.payload())))
                .isLessThanOrEqualTo(9000);
        assertThat(
                        WorkingContextSession.decode(row.payload()).stream()
                                .filter(WorkingContextCompactor::isSummary))
                .hasSize(1);
        verify(policy, times(5)).consume(claim, true);
        verify(policy, times(4)).consumeTool(claim, null);
        assertThat(archived).isEmpty();
    }

    @Test
    void largeToolResultIsArchivedBeforeNextLlmAndOnlyItsReferenceIsPersistedAsWork() {
        String raw = "RAW_PRIVATE_RESULT_" + "中".repeat(5000);
        registerProbe(input -> raw);
        var outcome =
                runtime(
                                (c, round, messages) ->
                                        round == 0
                                                ? toolCall(
                                                        "large-result",
                                                        "context_probe",
                                                        Map.of("query", "large file"))
                                                : text("completed"))
                        .execute(claim);
        assertThat(outcome.status()).isEqualTo("completed");
        assertThat(modelRequests).hasSize(2);
        assertThat(serialized(modelRequests.getLast().messages()))
                .contains("artifact_id", "file-1", "hash", "read_artifact_range", "max_bytes")
                .doesNotContain("RAW_PRIVATE_RESULT_", "中".repeat(100));
        assertThat(archived).hasSize(1);
        assertThat(PayloadJson.canonical(archived.getFirst())).contains(raw);
        assertThat(work.get("turn").row().payload() + work.get("turn").row().transcript())
                .contains("large-result", "large file", "file-1")
                .doesNotContain("RAW_PRIVATE_RESULT_");
        verify(artifacts).archiveToolResult(eq(claim), any(), isNull());
    }

    @Test
    void boundedResultStaysInlineAndDoesNotCreateAnArtifact() {
        registerProbe(input -> "bounded result");
        runtime(
                        (c, round, messages) ->
                                round == 0
                                        ? toolCall(
                                                "small-result",
                                                "context_probe",
                                                Map.of("query", "small file"))
                                        : text("completed"))
                .execute(claim);
        assertThat(serialized(modelRequests.getLast().messages())).contains("bounded result");
        assertThat(archived).isEmpty();
        verify(artifacts, never()).archiveToolResult(any(), any(), nullable(String.class));
    }

    @Test
    void schemaBudgetRejectsDispatchBeforeModelCounterAndProvider() {
        var execution = runtime((c, round, messages) -> text("must not run"));
        execution.configureContext(
                new MockEnvironment().withProperty("financeclaw.context.tool-schema-bytes", "1"));
        assertThatThrownBy(() -> execution.execute(claim))
                .isInstanceOf(Blocked.class)
                .hasMessage("tool_schema_budget_exceeded");
        assertThat(modelRequests).isEmpty();
        verify(policy, never()).consume(any(), anyBoolean());
    }

    @Test
    void currentInputAndLongMemoryHaveSeparateAdmissionLimits() {
        var oversized = claim("large", "attempt-large", null, "x".repeat(1025));
        var execution = runtime((c, round, messages) -> text("must not run"));
        execution.configureContext(
                new MockEnvironment()
                        .withProperty("financeclaw.context.current-input-bytes", "1024"));
        assertThatThrownBy(() -> execution.execute(oversized))
                .isInstanceOf(Blocked.class)
                .hasMessage("current_input_budget_exceeded");
        when(policy.memoryContext(claim)).thenReturn("中".repeat(6000));
        assertThatThrownBy(() -> execution.execute(claim))
                .isInstanceOf(Blocked.class)
                .hasMessage("long_memory_budget_exceeded");
        verifyNoInteractions(states, capabilities, interactions);
        assertThat(modelRequests).isEmpty();
    }

    @Test
    void oversizedCombinedProfileFailsBeforeNativeStateToolsOrModelDispatch() {
        when(releases.require("release"))
                .thenReturn(release(new ModelProfile("offline", "", 1024, 2048, 128, 0, false)));
        when(policy.memoryContext(claim)).thenReturn("中".repeat(350));
        assertThatThrownBy(
                        () -> runtime((c, round, messages) -> text("must not run")).execute(claim))
                .isInstanceOf(Blocked.class)
                .hasMessage("context_budget_exceeded");
        assertThat(modelRequests).isEmpty();
        verifyNoInteractions(states, capabilities, interactions);
        verify(policy, never()).consume(any(), anyBoolean());
    }

    @Test
    void resumeKeepsOriginalUserAnchorAndAddsAnsweredToolToTheSameTurnTranscript() {
        String frozen = "\nFROZEN_TURN_MEMORY";
        when(policy.memoryContext(any())).thenReturn(frozen);
        var first = claim("waiting", "attempt-old", null, "original request");
        var waiting =
                runtime((c, round, messages) -> toolCall("ask-city", "request_city", Map.of()))
                        .execute(first);
        assertThat(waiting.status()).isEqualTo("waiting");
        var pending = waiting.pending().getFirst();
        var answered =
                new Interaction(
                        "answered-city",
                        first.turn().id(),
                        first.command().id(),
                        first.command().attemptId(),
                        waiting.stateVersion(),
                        1,
                        "release",
                        "input",
                        pending.callId(),
                        pending.tool(),
                        pending.actionHash(),
                        pending.schema(),
                        pending.question(),
                        2,
                        "answered",
                        true,
                        "response-hash",
                        PayloadJson.canonical(Map.of("answer", Map.of("city", "宁波"))));
        when(interactions.interaction("answered-city")).thenReturn(answered);
        when(interactions.interactions(first.turn().id())).thenReturn(List.of(answered));
        var resumed = claim("waiting", "attempt-resume", "answered-city", "original request");
        var result = runtime((c, round, messages) -> text("resumed answer")).execute(resumed);
        assertThat(result.status()).isEqualTo("completed");
        var request = requestFor(resumed).messages();
        assertThat(request.stream().filter(message -> message.getId().equals("turn-input-waiting")))
                .hasSize(1);
        assertThat(systemText(request)).contains(frozen);
        assertThat(serialized(request)).contains("ask-city", "宁波", "original request");
        assertThat(work.get("waiting").row().transcript())
                .contains("ask-city", "宁波", "original request", "resumed answer")
                .doesNotContain(frozen);
        assertThat(work.get("waiting").claim().command().attemptId()).isEqualTo("attempt-resume");
        verify(states, times(1)).getLatestCompletedWorkContext(first, 1);
        verify(states, never()).getLatestCompletedWorkContext(resumed, 1);
        verify(states)
                .getTurnWorkContext(
                        first.turn(), first.command().attemptId(), waiting.stateVersion());
    }

    @Test
    void authorizationChangeRejectsResumeBeforeReadingOrCopyingItsCheckpoint() {
        Claim resume = claim("turn", "attempt", "old-interaction", "current request");
        when(policy.memoryContext(resume)).thenReturn("frozen context");
        doNothing()
                .doThrow(new Blocked("authorization_changed"))
                .when(policy)
                .checkDispatch(resume);
        assertThatThrownBy(
                        () -> runtime((c, round, messages) -> text("must not run")).execute(resume))
                .isInstanceOf(Blocked.class)
                .hasMessage("authorization_changed");
        var order = inOrder(policy);
        order.verify(policy).beginProgress(resume);
        order.verify(policy).checkDispatch(resume);
        order.verify(policy).memoryContext(resume);
        order.verify(policy).checkDispatch(resume);
        verifyNoInteractions(states, interactions);
        assertThat(modelRequests).isEmpty();
    }

    @Test
    void privacyChangeBeforeSdkSavePreventsBothCheckpointAndWorkContextWrites() {
        var forgotten = new java.util.concurrent.atomic.AtomicBoolean();
        when(policy.checkPolicy(claim))
                .thenAnswer(
                        i -> {
                            if (forgotten.get()) throw new Blocked("memory_privacy_changed");
                            return null;
                        });
        var execution =
                runtime(
                        (c, round, messages) -> {
                            forgotten.set(true);
                            return text("late answer must never be persisted");
                        });

        assertThatThrownBy(() -> execution.execute(claim))
                .isInstanceOf(Blocked.class)
                .hasMessage("memory_privacy_changed");
        assertThat(modelRequests).hasSize(1);
        assertThat(snapshots).isEmpty();
        assertThat(work).isEmpty();
        verify(states, never()).insertStateIfAbsent(any(), anyString());
        verify(states, never()).updateStateByExpectedVersion(any(), anyString(), anyLong());
        verify(states, never())
                .saveTurnWorkContext(any(), anyLong(), anyLong(), anyString(), anyString());
    }

    private void registerProbe(Function<Map<String, Object>, String> result) {
        doAnswer(
                        i -> {
                            ToolContribution.Registrar registrar = i.getArgument(3);
                            registrar.tool(
                                    "context_probe",
                                    Map.of(
                                            "type",
                                            "object",
                                            "properties",
                                            Map.of("query", Map.of("type", "string")),
                                            "required",
                                            List.of("query")),
                                    null,
                                    p ->
                                            Mono.just(
                                                    ToolResultBlock.text(
                                                            result.apply(p.getInput()))));
                            return null;
                        })
                .when(capabilities)
                .contribute(any(), isNull(), eq(false), any());
    }

    @FunctionalInterface
    private interface Script {
        ContentBlock answer(Claim claim, int round, List<Msg> messages);
    }

    private TaskAgentRuntime runtime(Script script) {
        return new TaskAgentRuntime(
                policy,
                leases,
                releases,
                mock(Credentials.class),
                interactions,
                artifacts,
                mock(ReportService.class),
                states,
                capabilities,
                5000) {
            @Override
            protected Model model(Claim current) {
                var rounds = new AtomicInteger();
                return new Model() {
                    public String getModelName() {
                        return "context-capture";
                    }

                    public Flux<ChatResponse> stream(
                            List<Msg> messages, List<ToolSchema> tools, GenerateOptions options) {
                        modelRequests.add(
                                new Request(current, List.copyOf(messages), List.copyOf(tools)));
                        int round = rounds.getAndIncrement();
                        return Flux.just(
                                ChatResponse.builder()
                                        // 固定Driver和部分模型可在同一Turn的不同调用间复用ID。
                                        .id("provider-reused-response-id")
                                        .content(List.of(script.answer(current, round, messages)))
                                        .build());
                    }
                };
            }
        };
    }

    private Request requestFor(Claim target) {
        return modelRequests.stream()
                .filter(request -> request.claim().equals(target))
                .findFirst()
                .orElseThrow();
    }

    private static MockEnvironment smallWorkBudget() {
        return new MockEnvironment()
                .withProperty("financeclaw.context.work-context-bytes", "9000")
                .withProperty("financeclaw.context.summary-bytes", "1500")
                .withProperty("financeclaw.context.compact-at-percent", "50");
    }

    private static boolean sameConversation(Claim first, Claim second) {
        return first.turn().conversationId().equals(second.turn().conversationId())
                && first.turn().tenant().equals(second.turn().tenant())
                && first.turn().subject().equals(second.turn().subject())
                && first.turn().releaseHash().equals(second.turn().releaseHash());
    }

    private static String serialized(List<Msg> messages) {
        return WorkingContextSession.encode(messages);
    }

    private static String systemText(List<Msg> messages) {
        return messages.stream()
                .filter(m -> m.getRole() == MsgRole.SYSTEM)
                .map(Msg::getTextContent)
                .collect(Collectors.joining("\n"));
    }

    private static TextBlock text(String value) {
        return TextBlock.builder().text(value).build();
    }

    private static ToolUseBlock toolCall(String id, String name, Map<String, Object> input) {
        return ToolUseBlock.builder()
                .id(id)
                .name(name)
                .input(input)
                .content(PayloadJson.canonical(input))
                .build();
    }

    private static ReleaseDefinition release(ModelProfile model) {
        return new ReleaseDefinition(
                "context-test",
                "finance_agent",
                List.of(),
                model,
                null,
                12,
                12,
                new BusinessFeatures(List.of(), "", "{}", null));
    }

    private static Claim claim(String turn, String attempt, String resume, String message) {
        return new Claim(
                new Command("command-" + attempt, turn, "running", attempt, 1, true, resume),
                new Turn(
                        turn,
                        "conversation",
                        "tenant",
                        "alice",
                        "key-" + turn,
                        "hash",
                        message,
                        "release",
                        "running",
                        2,
                        null,
                        null));
    }
}
