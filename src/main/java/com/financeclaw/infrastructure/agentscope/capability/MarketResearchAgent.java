package com.financeclaw.infrastructure.agentscope.capability;

import com.financeclaw.app.research.MarketResearchService;
import com.financeclaw.domain.Governance.*;
import com.financeclaw.domain.Task.Claim;
import com.financeclaw.domain.research.ResearchRun;
import com.financeclaw.infrastructure.agentscope.*;
import com.financeclaw.infrastructure.state.*;
import com.financeclaw.support.PayloadJson;
import io.agentscope.core.message.*;
import io.agentscope.core.model.*;
import io.agentscope.core.permission.*;
import io.agentscope.core.state.*;
import io.agentscope.core.tool.*;
import java.time.Duration;
import java.util.*;
import org.springframework.transaction.support.TransactionSynchronizationManager;
import reactor.core.publisher.*;
import reactor.core.scheduler.Schedulers;

/**
 * SDK state CAS, model streaming and cancellation; business rules belong to the research use case.
 */
public final class MarketResearchAgent {
    private final MarketResearchService research;
    private final RuntimeMapper states;
    private final Claim claim;
    private final Model model;

    public MarketResearchAgent(
            MarketResearchService research, RuntimeMapper states, Claim claim, Model model) {
        this.research = research;
        this.states = states;
        this.claim = claim;
        this.model = model;
    }

    public Mono<ToolResultBlock> run(ToolCallParam param) {
        if (ResearchRun.missing(param.getInput())) {
            return Mono.just(ToolResultBlock.suspended(param.getToolUseBlock()));
        }
        return Mono.defer(
                        () -> {
                            if (TransactionSynchronizationManager.isActualTransactionActive()) {
                                throw new IllegalStateException(
                                        "Native research must run outside a database transaction");
                            }
                            var execution =
                                    research.begin(
                                            claim,
                                            param.getToolUseBlock().getId(),
                                            param.getInput());
                            if (execution.cachedResult() != null) {
                                return Mono.just(ToolResultBlock.text(execution.cachedResult()));
                            }
                            var scope = execution.run().scope();
                            var store =
                                    new ChildResearchStateStore(states, scope, research, execution);
                            var saved =
                                    store.getVersioned(
                                            scope.userId(),
                                            scope.sessionId(),
                                            "agent_state",
                                            AgentState.class);
                            if (saved.isPresent()) {
                                var completed =
                                        AgentStateInspector.classify(
                                                saved.value(), saved.version());
                                if (completed.status().equals("completed")) {
                                    // SDK 最终检查点先于业务回执提交时，可据原始证据安全收尾。
                                    // 不再发起第一个新模型请求，避免恢复过程突破已用尽的持久额度。
                                    return Mono.just(
                                            ToolResultBlock.text(
                                                    PayloadJson.canonical(
                                                            research.complete(
                                                                    execution,
                                                                    true,
                                                                    completed.result()))));
                                }
                            }
                            Toolkit toolkit = new Toolkit();
                            toolkit.registerAgentTool(
                                    new ToolBase(
                                            ToolBase.builder()
                                                    .name("market_snapshot")
                                                    .description(
                                                            "Read release-pinned market evidence;"
                                                                    + " retain provider and as_of.")
                                                    .inputSchema(MarketToolSchemas.SNAPSHOT)) {
                                        @Override
                                        public Mono<ToolResultBlock> callAsync(ToolCallParam p) {
                                            return Mono.fromCallable(
                                                            () ->
                                                                    ToolResultBlock.text(
                                                                            PayloadJson.canonical(
                                                                                    research
                                                                                            .snapshot(
                                                                                                    execution,
                                                                                                    p.getToolUseBlock()
                                                                                                            .getId(),
                                                                                                    p
                                                                                                            .getInput()))))
                                                    .subscribeOn(Schedulers.boundedElastic());
                                        }
                                    });
                            Model delegate =
                                    model == null ? offline(execution.run().symbols()) : model;
                            Model bounded =
                                    new Model() {
                                        @Override
                                        public String getModelName() {
                                            return delegate.getModelName();
                                        }

                                        @Override
                                        public Flux<ChatResponse> stream(
                                                List<Msg> messages,
                                                List<ToolSchema> schemas,
                                                GenerateOptions options) {
                                            return Flux.defer(
                                                    () -> {
                                                        research.modelCall(execution);
                                                        return delegate.stream(
                                                                messages, schemas, options);
                                                    });
                                        }
                                    };
                            var permission =
                                    PermissionContextState.builder()
                                            .mode(PermissionMode.DEFAULT)
                                            .addAllowRule(
                                                    "market_snapshot",
                                                    new PermissionRule(
                                                            "market_snapshot",
                                                            null,
                                                            PermissionBehavior.ALLOW,
                                                            "child-narrowed"))
                                            .build();
                            return Mono.using(
                                    () ->
                                            IsolatedAgentFactory.create(
                                                    bounded, toolkit, store, permission),
                                    agent -> {
                                        var ctx =
                                                io.agentscope.core.agent.RuntimeContext.builder(
                                                                IsolatedAgentFactory.context(scope))
                                                        .put(
                                                                ExecutionIdentity.class,
                                                                execution.run().identity())
                                                        .build();
                                        return agent.call(
                                                        "Research only the supplied symbols and"
                                                                + " period. Use market_snapshot for"
                                                                + " every symbol; retain limitations.\n"
                                                                + PayloadJson.canonical(
                                                                        param.getInput()),
                                                        ctx)
                                                .timeout(Duration.ofSeconds(15))
                                                .doOnCancel(() -> agent.interrupt(ctx))
                                                .map(
                                                        answer -> {
                                                            var state =
                                                                    store.getVersioned(
                                                                            scope.userId(),
                                                                            scope.sessionId(),
                                                                            "agent_state",
                                                                            AgentState.class);
                                                            boolean completed =
                                                                    state.isPresent()
                                                                            && AgentStateInspector
                                                                                    .classify(
                                                                                            state
                                                                                                    .value(),
                                                                                            state
                                                                                                    .version())
                                                                                    .status()
                                                                                    .equals(
                                                                                            "completed");
                                                            return ToolResultBlock.text(
                                                                    PayloadJson.canonical(
                                                                            research.complete(
                                                                                    execution,
                                                                                    completed,
                                                                                    answer
                                                                                            .getTextContent())));
                                                        });
                                    },
                                    agent -> agent.close());
                        })
                .onErrorMap(e -> e instanceof Blocked ? e : new Blocked("child_failed"));
    }

    private static Model offline(List<String> symbols) {
        return new Model() {
            @Override
            public String getModelName() {
                return "market-research-offline-replay";
            }

            @Override
            public Flux<ChatResponse> stream(
                    List<Msg> messages, List<ToolSchema> tools, GenerateOptions options) {
                boolean read =
                        messages.stream()
                                .anyMatch(
                                        m -> !m.getContentBlocks(ToolResultBlock.class).isEmpty());
                List<ContentBlock> content = new ArrayList<>();
                if (read) {
                    content.add(
                            TextBlock.builder()
                                    .text("已核对指定证券的冻结行情；来源和时间见 evidence，不提供实时行情推断。")
                                    .build());
                } else {
                    for (int i = 0; i < symbols.size(); i++) {
                        content.add(
                                NativeToolCalls.call(
                                        "call-" + (i + 1),
                                        "market_snapshot",
                                        Map.of("symbol", symbols.get(i))));
                    }
                }
                return Flux.just(ChatResponse.builder().id("research").content(content).build());
            }
        };
    }
}
