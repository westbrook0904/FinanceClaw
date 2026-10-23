package com.financeclaw.m0;

import static org.assertj.core.api.Assertions.*;

import io.agentscope.core.ReActAgent;
import io.agentscope.core.agent.Agent;
import io.agentscope.core.agent.RuntimeContext;
import io.agentscope.core.event.AgentEvent;
import io.agentscope.core.message.*;
import io.agentscope.core.middleware.*;
import io.agentscope.core.tool.*;
import java.time.Duration;
import java.util.List;
import java.util.Map;
import java.util.concurrent.CopyOnWriteArrayList;
import java.util.function.Function;
import org.junit.jupiter.api.Test;
import reactor.core.publisher.Flux;
import reactor.core.publisher.Mono;

class SdkMiddlewareTest {
    @Test
    void releasedHooksWrapRealReasoningAndToolLoop() {
        List<String> hooks = new CopyOnWriteArrayList<>();
        var executions = new java.util.concurrent.atomic.AtomicInteger();
        MiddlewareBase middleware =
                new MiddlewareBase() {
                    public Flux<AgentEvent> onAgent(
                            Agent agent,
                            RuntimeContext context,
                            AgentInput input,
                            Function<AgentInput, Flux<AgentEvent>> next) {
                        hooks.add("agent");
                        return next.apply(input);
                    }

                    public Flux<AgentEvent> onReasoning(
                            Agent agent,
                            RuntimeContext context,
                            ReasoningInput input,
                            Function<ReasoningInput, Flux<AgentEvent>> next) {
                        hooks.add("reasoning");
                        return next.apply(input);
                    }

                    public Flux<AgentEvent> onModelCall(
                            Agent agent,
                            RuntimeContext context,
                            ModelCallInput input,
                            Function<ModelCallInput, Flux<AgentEvent>> next) {
                        hooks.add("model");
                        return next.apply(input);
                    }

                    public Flux<AgentEvent> onActing(
                            Agent agent,
                            RuntimeContext context,
                            ActingInput input,
                            Function<ActingInput, Flux<AgentEvent>> next) {
                        hooks.add("tool");
                        return next.apply(input);
                    }

                    public Mono<String> onSystemPrompt(
                            Agent agent, RuntimeContext context, String prompt) {
                        hooks.add("system");
                        return Mono.just(prompt);
                    }
                };
        Toolkit tools = new Toolkit();
        tools.registerAgentTool(
                new ToolBase(
                        ToolBase.builder()
                                .name("read_probe")
                                .description("read probe")
                                .inputSchema(
                                        Map.of(
                                                "type",
                                                "object",
                                                "properties",
                                                Map.of("operation", Map.of("type", "string"))))) {
                    public Mono<ToolResultBlock> callAsync(ToolCallParam param) {
                        executions.incrementAndGet();
                        assertThat(param.getInput()).containsEntry("operation", "report-1");
                        return Mono.just(ToolResultBlock.text("ok"));
                    }
                });
        try (var agent =
                ReActAgent.builder()
                        .name("probe")
                        .sysPrompt("policy")
                        .model(ProbeFixtures.scripted("read_probe", ""))
                        .toolkit(tools)
                        .permissionContext(ProbeFixtures.permissions("read_probe", false))
                        .middleware(middleware)
                        .maxRetries(1)
                        .build()) {
            var result =
                    agent.call(
                                    List.of(new UserMessage("user", "read")),
                                    RuntimeContext.builder()
                                            .userId("m0")
                                            .sessionId("hooks")
                                            .build())
                            .block(Duration.ofSeconds(5));
            assertThat(result.getTextContent()).isEqualTo("done");
            assertThat(executions).hasValue(1);
            assertThat(hooks)
                    .containsExactly(
                            "agent", "system", "reasoning", "model", "tool", "reasoning", "model");
        }
    }
}
