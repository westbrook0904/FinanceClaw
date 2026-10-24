package com.financeclaw.infrastructure.agentscope.capability;

import static org.assertj.core.api.Assertions.*;
import static org.mockito.ArgumentMatchers.*;
import static org.mockito.Mockito.*;

import com.financeclaw.app.execution.ChildInvocationService;
import com.financeclaw.app.execution.ChildInvocationService.Invocation;
import com.financeclaw.app.execution.ChildInvocationService.PrivateInvocation;
import com.financeclaw.app.market.MarketDataService;
import com.financeclaw.app.release.port.Credentials;
import com.financeclaw.domain.Governance.Blocked;
import com.financeclaw.domain.Task.*;
import com.financeclaw.domain.agent.*;
import com.financeclaw.domain.release.*;
import com.financeclaw.infrastructure.agentscope.NativeToolCalls;
import com.financeclaw.infrastructure.agentscope.RuntimeArtifacts;
import com.financeclaw.infrastructure.agentscope.model.ModelFactory;
import com.financeclaw.infrastructure.state.RuntimeMapper;
import com.financeclaw.infrastructure.state.RuntimeMapper.StateKey;
import com.financeclaw.infrastructure.state.RuntimeMapper.StateRow;
import com.financeclaw.support.ArtifactPayload;
import com.financeclaw.support.PayloadJson;
import com.financeclaw.support.PrivateAgentContracts;
import io.agentscope.core.message.*;
import io.agentscope.core.model.*;
import io.agentscope.core.tool.ToolCallParam;
import java.util.*;
import java.util.concurrent.ConcurrentHashMap;
import java.util.concurrent.CopyOnWriteArrayList;
import java.util.concurrent.atomic.AtomicInteger;
import java.util.function.Function;
import java.util.function.Supplier;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.params.ParameterizedTest;
import org.junit.jupiter.params.provider.ValueSource;
import org.springframework.mock.env.MockEnvironment;
import reactor.core.publisher.Flux;

/** 真实原生 SDK 循环；替换计数边界和带版本持久端口以观测隔离、拒绝工具及收尾行为。 */
class PrivateAgentDelegationTest {
    private final ChildInvocationService calls = mock(ChildInvocationService.class);
    private final RuntimeMapper states = mock(RuntimeMapper.class);
    private final Credentials credentials = mock(Credentials.class);
    private final MarketDataService market = mock(MarketDataService.class);
    private final Map<StateKey, StateRow> rows = new ConcurrentHashMap<>();
    private final List<Request> requests = new CopyOnWriteArrayList<>();
    private final AtomicInteger opened = new AtomicInteger();
    private final AtomicInteger closed = new AtomicInteger();
    private PrivateAgentTarget target =
            new PrivateAgentTarget("1eaa8d13-26db-41f2-8bbb-3576dc75dfe1", "a".repeat(64));
    private final Claim parent =
            new Claim(
                    new Command(
                            "root-command", "root-turn", "running", "root-attempt", 1, true, null),
                    new Turn(
                            "root-turn",
                            "parent-conversation",
                            "tenant",
                            "alice",
                            "key",
                            "hash",
                            "PARENT_QUESTION_PRIVATE_MEMORY_TOOL_RESULT",
                            "root-release",
                            "running",
                            2,
                            null,
                            null,
                            "root-agent"));
    private ModelProfile fallback;
    private String cached;
    private Function<ModelProfile, Flux<ChatResponse>> response = ignored -> text("leaf answer");

    private record Request(ModelProfile profile, List<Msg> messages, List<ToolSchema> tools) {}

    @BeforeEach
    void setup() {
        when(calls.privateSnapshot(any(), anyString(), anyMap(), any()))
                .thenAnswer(i -> ((Supplier<?>) i.getArgument(3)).get());
        when(market.readFrozen(eq(parent), any(), any(), eq("AAPL")))
                .thenReturn(
                        Map.of(
                                "symbol",
                                "AAPL",
                                "provider",
                                "FROZEN_LEAF_MARKET",
                                "price",
                                "123.45"));
        when(calls.checkpoint(any(), any()))
                .thenAnswer(i -> ((Supplier<?>) i.getArgument(1)).get());
        when(calls.complete(any(), any())).thenAnswer(i -> ((Supplier<?>) i.getArgument(1)).get());
        when(calls.beginPrivate(eq(parent), eq("call"), anyMap(), argThat(t -> target.equals(t))))
                .thenAnswer(
                        i -> {
                            var definition =
                                    new ReleaseDefinition(
                                            "leaf-test",
                                            "finance_agent",
                                            target.profile() == PrivateAgentTarget.Profile.EMPTY
                                                    ? List.of()
                                                    : List.of("market:read"),
                                            ReleaseDefinition.OFFLINE,
                                            fallback,
                                            12,
                                            12,
                                            new BusinessFeatures(List.of(), "", "{}", null));
                            var plan =
                                    new AgentExecutionPlan(
                                            "primary",
                                            RuntimeArtifacts.hash(),
                                            target.profile() == PrivateAgentTarget.Profile.EMPTY
                                                    ? List.of()
                                                    : List.of("function:market_snapshot"),
                                            target.profile() == PrivateAgentTarget.Profile.EMPTY
                                                    ? List.of()
                                                    : LocalCapabilityCatalog.bindings(definition)
                                                            .stream()
                                                            .filter(
                                                                    b ->
                                                                            Set.of(
                                                                                            "function:market_snapshot",
                                                                                            "function:read_artifact_range")
                                                                                    .contains(
                                                                                            b.id()))
                                                            .toList(),
                                            definition);
                            var release =
                                    new ResolvedAgentRelease(
                                            target.agentId(),
                                            "FROZEN_LEAF_SYSTEM_PROMPT",
                                            definition,
                                            "tenant",
                                            "alice",
                                            plan);
                            return new PrivateInvocation(
                                    new Invocation(
                                            parent,
                                            "call",
                                            "input-hash",
                                            cached,
                                            List.of(),
                                            "private_agent",
                                            target.agentId(),
                                            target.releaseHash(),
                                            4,
                                            target.profile() == PrivateAgentTarget.Profile.EMPTY
                                                    ? 0
                                                    : 1,
                                            target.profile()),
                                    release);
                        });
        when(states.getState(any())).thenAnswer(i -> rows.get(i.getArgument(0)));
        when(states.insertStateIfAbsent(any(), anyString()))
                .thenAnswer(
                        i ->
                                rows.putIfAbsent(
                                                        i.getArgument(0),
                                                        new StateRow(i.getArgument(1), 1, false))
                                                == null
                                        ? 1
                                        : 0);
        when(states.updateStateByExpectedVersion(any(), anyString(), anyLong()))
                .thenAnswer(
                        i -> {
                            StateKey key = i.getArgument(0);
                            StateRow old = rows.get(key);
                            long expected = i.getArgument(2);
                            return old != null
                                            && old.version() == expected
                                            && rows.replace(
                                                    key,
                                                    old,
                                                    new StateRow(
                                                            i.getArgument(1), expected + 1, false))
                                    ? 1
                                    : 0;
                        });
    }

    @Test
    void nativeRequestUsesOnlyFrozenTargetPromptAndExplicitMessageWithNoTools() {
        runtime()
                .run(parent, PrivateAgentContracts.binding(target), input("EXPLICIT_LEAF_MESSAGE"))
                .block();
        assertThat(requests).hasSize(1);
        String text =
                requests.getFirst().messages().stream()
                        .map(Msg::getTextContent)
                        .reduce("", String::concat);
        assertThat(text)
                .contains("FROZEN_LEAF_SYSTEM_PROMPT", "EXPLICIT_LEAF_MESSAGE")
                .doesNotContain(
                        "PARENT_QUESTION", "PRIVATE_MEMORY", "TOOL_RESULT", "parent-conversation");
        assertThat(requests.getFirst().tools()).isEmpty();
        assertThat(rows).hasSize(1);
        assertThat(rows.keySet().iterator().next().namespace())
                .isEqualTo(target.scope(parent.scope(), "call").namespace());
        verify(calls, times(1)).reserveModel(any());
        verify(calls, times(1)).complete(any(), any());
        assertThat(opened.get()).isEqualTo(1);
        assertThat(closed.get()).isEqualTo(1);
        verifyNoInteractions(credentials);
    }

    @Test
    void fabricatedNestedToolIsFatalAndCannotBeConvertedToSuccessfulLeafResult() {
        response =
                ignored ->
                        Flux.just(
                                ChatResponse.builder()
                                        .id("forged")
                                        .content(
                                                List.of(
                                                        NativeToolCalls.call(
                                                                "nested",
                                                                "market_snapshot",
                                                                Map.of("symbol", "AAPL"))))
                                        .build());
        assertThatThrownBy(
                        () ->
                                runtime()
                                        .run(
                                                parent,
                                                PrivateAgentContracts.binding(target),
                                                input("leaf request"))
                                        .block())
                .isInstanceOf(Blocked.class)
                .hasMessage("child_tools_denied");
        assertThat(requests).hasSize(1);
        assertThat(requests.getFirst().tools()).isEmpty();
        verify(calls, never()).complete(any(), any());
        assertThat(closed.get()).isEqualTo(opened.get());
    }

    @Test
    void finalSdkCheckpointCanCloseReceiptWithoutAnotherModelReservation() {
        var runtime = runtime();
        runtime.run(parent, PrivateAgentContracts.binding(target), input("leaf request")).block();
        // 模拟业务 result 回执尚未提交：begin 再次返回 pending，但 SDK 已有最终检查点。
        doThrow(new Blocked("child_model_budget_exceeded")).when(calls).reserveModel(any());
        runtime.run(parent, PrivateAgentContracts.binding(target), input("leaf request")).block();
        assertThat(requests).hasSize(1);
        verify(calls, times(1)).reserveModel(any());
        verify(calls, times(2)).complete(any(), any());
        assertThat(opened.get()).isEqualTo(1);
        assertThat(closed.get()).isEqualTo(1);
    }

    @Test
    void committedReceiptReplayStillBeginsWithGateButDoesNotReadSdkOrCreateModels() {
        cached = PayloadJson.canonical(Map.of("answer", "cached answer"));
        runtime().run(parent, PrivateAgentContracts.binding(target), input("leaf request")).block();
        verify(calls).beginPrivate(eq(parent), eq("call"), anyMap(), eq(target));
        verify(calls, never()).reserveModel(any());
        verifyNoInteractions(states);
        assertThat(opened.get()).isZero();
    }

    @Test
    void fallbackUsesItsOwnProfileAndReservesAnotherActualRequest() {
        fallback =
                new ModelProfile(
                        "fallback", "http://127.0.0.1:1", 65536, 131072, 2048, 1024, false);
        response =
                profile ->
                        profile.name().equals("offline")
                                ? Flux.error(
                                        new IllegalStateException("synthetic provider failure"))
                                : text("fallback answer");
        runtime().run(parent, PrivateAgentContracts.binding(target), input("leaf request")).block();
        assertThat(requests).hasSize(2);
        assertThat(requests)
                .extracting(r -> r.profile().name())
                .containsExactly("offline", "fallback");
        verify(calls, times(2)).reserveModel(any());
        verify(calls).complete(any(), any());
        assertThat(opened.get()).isEqualTo(2);
        assertThat(closed.get()).isEqualTo(2);
    }

    @Test
    void admissionRejectsOversizedExplicitMessageBeforeModelOrCheckpointAccess() {
        assertThatThrownBy(
                        () ->
                                runtime()
                                        .run(
                                                parent,
                                                PrivateAgentContracts.binding(target),
                                                input("a".repeat(9000)))
                                        .block())
                .isInstanceOf(Blocked.class)
                .hasMessage("current_input_budget_exceeded");
        verify(calls, never()).reserveModel(any());
        verifyNoInteractions(states);
        assertThat(opened.get()).isZero();
    }

    @Test
    void explicitMessageBudgetHonorsTheConfiguredContextPartition() {
        var runtime = runtime();
        runtime.configureContext(
                new MockEnvironment()
                        .withProperty("financeclaw.context.current-input-bytes", "10000"));
        runtime.run(parent, PrivateAgentContracts.binding(target), input("a".repeat(9000))).block();
        assertThat(requests).hasSize(1);
        verify(calls).reserveModel(any());
    }

    @Test
    void marketProfileUsesOnlyItsFrozenSnapshotAndDoesNotRegisterReaderDependency() {
        marketTarget();
        var iteration = new AtomicInteger();
        response =
                ignored ->
                        iteration.getAndIncrement() == 0
                                ? snapshotCall()
                                : text("retained market evidence");
        runtime().run(parent, PrivateAgentContracts.binding(target), input("leaf request")).block();
        assertThat(requests).hasSize(2);
        for (var request : requests) {
            assertThat(request.tools())
                    .extracting(ToolSchema::getName)
                    .containsExactly("market_snapshot");
        }
        assertThat(
                        requests.getLast().messages().stream()
                                .map(
                                        m ->
                                                io.agentscope.core.util.JsonUtils.getJsonCodec()
                                                        .toJson(m))
                                .reduce("", String::concat))
                .contains("FROZEN_LEAF_MARKET", "123.45")
                .doesNotContain("PARENT_QUESTION_PRIVATE_MEMORY_TOOL_RESULT");
        verify(market).readFrozen(eq(parent), any(), any(), eq("AAPL"));
        verify(market, never()).read(any(), anyString());
        verify(calls).privateSnapshot(any(), eq("snapshot-1"), eq(Map.of("symbol", "AAPL")), any());
        verify(calls, times(2)).reserveModel(any());
        verify(calls).complete(any(), any());
        assertThat(opened.get()).isEqualTo(closed.get());
    }

    @ParameterizedTest
    @ValueSource(strings = {"read_artifact_range", "__fragment__"})
    void marketProfileRejectsReaderAndOrphanArgumentFragmentsAsFatal(String forbidden) {
        marketTarget();
        response =
                ignored ->
                        Flux.just(
                                ChatResponse.builder()
                                        .id("forged")
                                        .content(
                                                List.of(
                                                        NativeToolCalls.call(
                                                                forbidden.equals("__fragment__")
                                                                        ? ""
                                                                        : "forged-reader",
                                                                forbidden,
                                                                Map.of("artifact_id", "guessed"))))
                                        .build());
        assertThatThrownBy(
                        () ->
                                runtime()
                                        .run(
                                                parent,
                                                PrivateAgentContracts.binding(target),
                                                input("leaf request"))
                                        .block())
                .hasMessage("child_tools_denied");
        verify(calls, never()).privateSnapshot(any(), anyString(), anyMap(), any());
        verify(calls, never()).complete(any(), any());
        verifyNoInteractions(market);
    }

    @Test
    void toolResultJsonCountsAgainstTheChildWorkContextBeforeTheNextModelRequest() {
        marketTarget();
        when(market.readFrozen(eq(parent), any(), any(), eq("AAPL")))
                .thenReturn(
                        Map.of(
                                "symbol",
                                "AAPL",
                                "provider",
                                "证".repeat(128),
                                "price",
                                "1234567890123456.12345678",
                                "as_of",
                                "+999999999-12-31T23:59:59.999999999+18:00",
                                "release_hash",
                                "f".repeat(64)));
        response = ignored -> snapshotCall();
        var runtime = runtime();
        runtime.configureContext(
                new MockEnvironment()
                        .withProperty("financeclaw.context.work-context-bytes", "1024")
                        .withProperty("financeclaw.context.summary-bytes", "256"));
        assertThatThrownBy(
                        () ->
                                runtime.run(
                                                parent,
                                                PrivateAgentContracts.binding(target),
                                                input("leaf request"))
                                        .block())
                .hasMessage("context_budget_exceeded");
        assertThat(requests).hasSize(1);
        verify(calls).reserveModel(any());
        verify(market).readFrozen(eq(parent), any(), any(), eq("AAPL"));
        verify(calls, never()).complete(any(), any());
    }

    @Test
    void maximalBoundedSnapshotWithEscapedProviderAndCallIdNeedsNoChildArchiveReader() {
        marketTarget();
        when(market.readFrozen(eq(parent), any(), any(), eq("AAPL")))
                .thenReturn(
                        Map.of(
                                "symbol",
                                "ABCDEFGHIJKLMNOP",
                                "provider",
                                "\u0001".repeat(128),
                                "price",
                                "1234567890123456.12345678",
                                "currency",
                                "USD",
                                "as_of",
                                "+999999999-12-31T23:59:59.999999999+18:00",
                                "source_release_hash",
                                "f".repeat(64),
                                "input_hash",
                                "a".repeat(64),
                                "tool_version",
                                "1.0.0"));
        var iteration = new AtomicInteger();
        response =
                ignored ->
                        iteration.getAndIncrement() == 0
                                ? Flux.just(
                                        ChatResponse.builder()
                                                .id("snapshot")
                                                .content(
                                                        List.of(
                                                                NativeToolCalls.call(
                                                                        "x".repeat(512),
                                                                        "market_snapshot",
                                                                        Map.of("symbol", "AAPL"))))
                                                .build())
                                : text("retained bounded evidence");
        runtime().run(parent, PrivateAgentContracts.binding(target), input("leaf request")).block();
        var results =
                requests.getLast().messages().stream()
                        .flatMap(m -> m.getContentBlocks(ToolResultBlock.class).stream())
                        .toList();
        assertThat(results).hasSize(1);
        assertThat(
                        ArtifactPayload.encode(
                                        PayloadJson.parse(
                                                io.agentscope.core.util.JsonUtils.getJsonCodec()
                                                        .toJson(results.getFirst())))
                                .bytes())
                .isLessThan(4096);
        verify(calls).complete(any(), any());
    }

    private void marketTarget() {
        target =
                new PrivateAgentTarget(
                        target.agentId(),
                        target.releaseHash(),
                        PrivateAgentTarget.Profile.MARKET_SNAPSHOT);
    }

    private static Flux<ChatResponse> snapshotCall() {
        return Flux.just(
                ChatResponse.builder()
                        .id("snapshot")
                        .content(
                                List.of(
                                        NativeToolCalls.call(
                                                "snapshot-1",
                                                "market_snapshot",
                                                Map.of("symbol", "AAPL"))))
                        .build());
    }

    private PrivateAgentDelegation runtime() {
        return new PrivateAgentDelegation(calls, states, credentials, market) {
            @Override
            protected ModelFactory.Handle createModel(ModelProfile profile) {
                opened.incrementAndGet();
                return new ModelFactory.Handle() {
                    @Override
                    public void close() {
                        closed.incrementAndGet();
                    }

                    @Override
                    public String getModelName() {
                        return profile.name();
                    }

                    @Override
                    public Flux<ChatResponse> stream(
                            List<Msg> messages, List<ToolSchema> tools, GenerateOptions options) {
                        requests.add(
                                new Request(profile, List.copyOf(messages), List.copyOf(tools)));
                        return response.apply(profile);
                    }
                };
            }
        };
    }

    private ToolCallParam input(String message) {
        var block =
                NativeToolCalls.call(
                        "call",
                        PrivateAgentContracts.binding(target).name(),
                        Map.of("message", message));
        return ToolCallParam.builder().toolUseBlock(block).input(block.getInput()).build();
    }

    private static Flux<ChatResponse> text(String text) {
        return Flux.just(
                ChatResponse.builder()
                        .id("answer")
                        .content(List.of(TextBlock.builder().text(text).build()))
                        .build());
    }
}
