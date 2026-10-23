package com.financeclaw.infrastructure.agentscope.context;

import static org.assertj.core.api.Assertions.*;
import static org.mockito.Mockito.*;

import com.financeclaw.app.artifact.ArtifactService;
import com.financeclaw.domain.Governance.Artifact;
import com.financeclaw.domain.Governance.Blocked;
import com.financeclaw.domain.Task.*;
import com.financeclaw.domain.release.ModelProfile;
import com.financeclaw.support.ArtifactPayload;
import io.agentscope.core.ReActAgent;
import io.agentscope.core.agent.Agent;
import io.agentscope.core.agent.RuntimeContext;
import io.agentscope.core.hook.*;
import io.agentscope.core.message.*;
import io.agentscope.core.model.*;
import io.agentscope.core.permission.*;
import io.agentscope.core.tool.*;
import io.agentscope.core.util.JsonUtils;
import java.time.Duration;
import java.util.List;
import java.util.Map;
import java.util.concurrent.CopyOnWriteArrayList;
import java.util.concurrent.atomic.AtomicInteger;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;
import org.mockito.ArgumentCaptor;
import reactor.core.publisher.Flux;
import reactor.core.publisher.Mono;

/** 使用发布版原生 Hook 和工具循环，防止只替换结果字段却让 SDK 继续观察原始结果消息。 */
class WorkingContextMiddlewareTest {
    private static final String TOOL = "large_probe";
    private static final String RAW = "RAW_LARGE_TOOL_PAYLOAD";
    private static final String RAW_METADATA = "RAW_TOOL_METADATA";
    private static final Claim CLAIM =
            new Claim(
                    new Command("command", "turn", "running", "attempt", 1, true, null),
                    new Turn(
                            "turn",
                            "conversation",
                            "tenant",
                            "subject",
                            "key",
                            "hash",
                            "input",
                            "release",
                            "running",
                            1,
                            null,
                            null));
    private ArtifactService artifacts;
    private WorkingContextMiddleware middleware;

    @BeforeEach
    void setup() {
        artifacts = mock(ArtifactService.class);
        when(artifacts.archiveToolResult(eq(CLAIM), any(), eq("skill:use")))
                .thenAnswer(
                        invocation -> {
                            var payload = ArtifactPayload.encode(invocation.getArgument(1));
                            return new Artifact(
                                    "artifact",
                                    "turn",
                                    "conversation",
                                    "tenant",
                                    "subject",
                                    "release",
                                    payload.hash(),
                                    payload.json(),
                                    payload.bytes());
                        });
        var session = mock(WorkingContextSession.class);
        when(session.inputId()).thenReturn("turn-input-turn");
        middleware =
                new WorkingContextMiddleware(
                        CLAIM,
                        new ModelProfile("offline", "", 65536, 65536, 1024, 0, false),
                        ContextBudgets.defaults(),
                        "",
                        session,
                        new ToolResultOffloader(artifacts, 8192),
                        name -> "skill:use",
                        request -> fail("Small references must not require context compaction"));
    }

    @Test
    void nativePostActingRemovesRawContentBeforeObservingAndReasoningDoesNotArchiveAgain() {
        var postActing = new AtomicInteger();
        var executions = new AtomicInteger();
        var requests = new CopyOnWriteArrayList<List<Msg>>();
        var toolkit = new Toolkit();
        toolkit.registerAgentTool(
                new ToolBase(
                        ToolBase.builder()
                                .name(TOOL)
                                .description("native hook regression probe")
                                .inputSchema(Map.of("type", "object", "properties", Map.of()))) {
                    @Override
                    public Mono<ToolResultBlock> callAsync(ToolCallParam param) {
                        executions.incrementAndGet();
                        return Mono.just(raw());
                    }
                });
        Model model =
                new Model() {
                    @Override
                    public String getModelName() {
                        return "native-offload-regression";
                    }

                    @Override
                    public Flux<ChatResponse> stream(
                            List<Msg> messages, List<ToolSchema> tools, GenerateOptions options) {
                        requests.add(List.copyOf(messages));
                        ContentBlock content =
                                requests.size() == 1
                                        ? call()
                                        : TextBlock.builder().text("done").build();
                        return Flux.just(
                                ChatResponse.builder()
                                        .id("response-" + requests.size())
                                        .content(List.of(content))
                                        .build());
                    }
                };
        // 包装同一个原生 Hook：这里的断言发生于 SDK firePostActing 返回之前，早于结果消息加入 context。
        Hook nativeHook =
                new Hook() {
                    @Override
                    public <T extends HookEvent> Mono<T> onEvent(T event) {
                        return middleware
                                .onEvent(event)
                                .doOnNext(
                                        next -> {
                                            if (next instanceof PostActingEvent acting) {
                                                postActing.incrementAndGet();
                                                assertThat(acting.getToolResultMsg()).isNotNull();
                                                assertThat(
                                                                acting.getToolResultMsg()
                                                                        .getFirstContentBlock())
                                                        .isSameAs(acting.getToolResult());
                                                assertThat(
                                                                JsonUtils.getJsonCodec()
                                                                        .toJson(
                                                                                acting
                                                                                        .getToolResultMsg()))
                                                        .doesNotContain(RAW, RAW_METADATA)
                                                        .contains("artifact_id", "artifact");
                                                assertThat(acting.getToolResult().getId())
                                                        .isEqualTo("call");
                                                assertThat(acting.getToolResult().getName())
                                                        .isEqualTo(TOOL);
                                                assertThat(acting.getToolResult().getState())
                                                        .isEqualTo(ToolResultState.SUCCESS);
                                            }
                                        });
                    }
                };
        var rule = new PermissionRule(TOOL, null, PermissionBehavior.ALLOW, "native-test");
        var permissions =
                PermissionContextState.builder()
                        .mode(PermissionMode.DEFAULT)
                        .addAllowRule(TOOL, rule)
                        .build();
        try (var agent =
                ReActAgent.builder()
                        .name("native-offload-test")
                        .sysPrompt("policy")
                        .model(model)
                        .toolkit(toolkit)
                        .permissionContext(permissions)
                        .middleware(middleware)
                        .hook(nativeHook)
                        .enableMetaTool(false)
                        .enableTaskList(false)
                        .maxIters(3)
                        .maxRetries(1)
                        .build()) {
            var result =
                    agent.call(
                                    List.of(
                                            Msg.builder()
                                                    .id("turn-input-turn")
                                                    .name("user")
                                                    .role(MsgRole.USER)
                                                    .textContent("input")
                                                    .build()),
                                    RuntimeContext.builder()
                                            .userId("tenant-subject")
                                            .sessionId("native-offload")
                                            .build())
                            .block(Duration.ofSeconds(5));
            assertThat(result.getTextContent()).isEqualTo("done");
        }
        assertThat(executions).hasValue(1);
        assertThat(postActing).hasValue(1);
        assertThat(requests).hasSize(2);
        assertThat(JsonUtils.getJsonCodec().toJson(requests.get(1)))
                .doesNotContain(RAW, RAW_METADATA)
                .contains("artifact_id", "artifact");
        var payload = ArgumentCaptor.forClass(Object.class);
        verify(artifacts, times(1))
                .archiveToolResult(eq(CLAIM), payload.capture(), eq("skill:use"));
        assertThat(JsonUtils.getJsonCodec().toJson(payload.getValue())).contains(RAW, RAW_METADATA);
    }

    @Test
    void hookKeepsOriginalResultAndMessageWhenArchivingFails() {
        var raw = raw().withIdAndName("call", TOOL);
        var message = ToolResultMessageBuilder.buildToolResultMsg(raw, call(), "agent");
        var acting = new PostActingEvent(mock(Agent.class), new Toolkit(), call(), raw);
        acting.setToolResultMsg(message);
        var failure = new Blocked("artifact_archive_failed");
        when(artifacts.archiveToolResult(eq(CLAIM), any(), eq("skill:use"))).thenThrow(failure);

        assertThatThrownBy(() -> middleware.onEvent(acting).block(Duration.ofSeconds(5)))
                .isSameAs(failure);
        assertThat(acting.getToolResult()).isSameAs(raw);
        assertThat(acting.getToolResultMsg()).isSameAs(message);
        assertThat(JsonUtils.getJsonCodec().toJson(message)).contains(RAW);
    }

    @Test
    void hookCreatesAReferenceMessageWhenNativeEventHasNoMessageYet() {
        var acting =
                new PostActingEvent(
                        mock(Agent.class),
                        new Toolkit(),
                        call(),
                        raw().withIdAndName("call", TOOL));
        assertThat(middleware.onEvent(acting).block(Duration.ofSeconds(5))).isSameAs(acting);
        assertThat(acting.getToolResultMsg().getRole()).isEqualTo(MsgRole.TOOL);
        assertThat(acting.getToolResultMsg().getFirstContentBlock())
                .isSameAs(acting.getToolResult());
        assertThat(JsonUtils.getJsonCodec().toJson(acting.getToolResultMsg()))
                .doesNotContain(RAW, RAW_METADATA)
                .contains("artifact_id");
        verify(artifacts).archiveToolResult(eq(CLAIM), any(), eq("skill:use"));
    }

    private static ToolUseBlock call() {
        return ToolUseBlock.builder().id("call").name(TOOL).input(Map.of()).content("{}").build();
    }

    private static ToolResultBlock raw() {
        return new ToolResultBlock(
                "call",
                TOOL,
                List.of(TextBlock.builder().text(RAW.repeat(1000)).build()),
                Map.of("diagnostic", RAW_METADATA.repeat(1000)),
                ToolResultState.SUCCESS);
    }
}
