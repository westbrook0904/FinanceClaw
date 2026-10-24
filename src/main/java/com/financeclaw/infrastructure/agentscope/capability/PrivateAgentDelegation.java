package com.financeclaw.infrastructure.agentscope.capability;

import com.financeclaw.app.execution.ChildInvocationService;
import com.financeclaw.app.execution.ChildInvocationService.Invocation;
import com.financeclaw.app.release.port.Credentials;
import com.financeclaw.domain.Governance.Blocked;
import com.financeclaw.domain.Task.Claim;
import com.financeclaw.domain.agent.*;
import com.financeclaw.domain.release.ModelProfile;
import com.financeclaw.infrastructure.agentscope.AgentStateInspector;
import com.financeclaw.infrastructure.agentscope.IsolatedAgentFactory;
import com.financeclaw.infrastructure.agentscope.context.ContextBudgets;
import com.financeclaw.infrastructure.agentscope.model.ModelFactory;
import com.financeclaw.infrastructure.state.RuntimeMapper;
import com.financeclaw.support.PayloadJson;
import com.financeclaw.support.PrivateAgentContracts;
import io.agentscope.core.message.*;
import io.agentscope.core.model.*;
import io.agentscope.core.permission.PermissionContextState;
import io.agentscope.core.permission.PermissionMode;
import io.agentscope.core.state.AgentState;
import io.agentscope.core.tool.ToolCallParam;
import io.agentscope.core.tool.ToolValidator;
import io.agentscope.core.tool.Toolkit;
import java.time.Duration;
import java.util.*;
import java.util.concurrent.atomic.AtomicReference;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.core.env.Environment;
import org.springframework.stereotype.Component;
import org.springframework.transaction.support.TransactionSynchronizationManager;
import reactor.core.publisher.Flux;
import reactor.core.publisher.Mono;

/** 将发布时冻结的叶子目标装配为独立原生 ReActAgent。只传显式 message 和目标提示词， 不读取父画像、会话历史或工具结果；父 Turn 仍独占调度、取消和持久计数。 */
@Component
public class PrivateAgentDelegation {
    private final ChildInvocationService calls;
    private final RuntimeMapper states;
    private final Credentials credentials;
    private ContextBudgets budgets = ContextBudgets.defaults();
    private Duration timeout = Duration.ofSeconds(30);

    public PrivateAgentDelegation(
            ChildInvocationService calls, RuntimeMapper states, Credentials credentials) {
        this.calls = calls;
        this.states = states;
        this.credentials = credentials;
    }

    /** 与主运行时使用同一分区预算和超时配置；不会因此导入父上下文。 */
    @Autowired
    void configureContext(Environment environment) {
        budgets = ContextBudgets.from(environment);
        timeout =
                Duration.ofMillis(
                        environment.getProperty(
                                "financeclaw.worker.model-timeout-ms", Long.class, 30000L));
    }

    public Mono<ToolResultBlock> run(Claim parent, CapabilityBinding binding, ToolCallParam param) {
        return Mono.defer(
                        () -> {
                            if (TransactionSynchronizationManager.isActualTransactionActive()) {
                                throw new IllegalStateException(
                                        "Private Agent must run outside a database transaction");
                            }
                            var target = PrivateAgentContracts.from(binding);
                            if (ToolValidator.validateInput(
                                            PayloadJson.canonical(param.getInput()),
                                            PrivateAgentTarget.INPUT_SCHEMA)
                                    != null) {
                                throw new Blocked("invalid_tool_input");
                            }
                            var started =
                                    calls.beginPrivate(
                                            parent,
                                            param.getToolUseBlock().getId(),
                                            param.getInput(),
                                            target);
                            var invocation = started.invocation();
                            var release = started.release();
                            LocalCapabilityCatalog.requireCompatible(release.plan());
                            if (invocation.cachedResult() != null) {
                                return Mono.just(ToolResultBlock.text(invocation.cachedResult()));
                            }
                            String message = (String) param.getInput().get("message");
                            var profile = release.definition().primary();
                            budgets.checkAdmission(profile, "", message);
                            var scope = target.scope(parent.scope(), invocation.callId());
                            var store =
                                    new PrivateAgentStateStore(states, scope, calls, invocation);
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
                                    // 原生最终检查点可能先于业务 result 回执落库。收尾不能再次扣模型额度。
                                    return Mono.just(result(invocation, completed.result()));
                                }
                            }
                            var fatal = new AtomicReference<Blocked>();
                            return Mono.using(
                                    () -> models(release),
                                    models ->
                                            Mono.using(
                                                    () ->
                                                            IsolatedAgentFactory.create(
                                                                    bounded(
                                                                            models.primary(),
                                                                            profile,
                                                                            invocation,
                                                                            fatal),
                                                                    models.fallback() == null
                                                                            ? null
                                                                            : bounded(
                                                                                    models
                                                                                            .fallback(),
                                                                                    release.definition()
                                                                                            .fallback(),
                                                                                    invocation,
                                                                                    fatal),
                                                                    new Toolkit(),
                                                                    store,
                                                                    PermissionContextState.builder()
                                                                            .mode(
                                                                                    PermissionMode
                                                                                            .DEFAULT)
                                                                            .build(),
                                                                    builder ->
                                                                            builder.name(
                                                                                            target
                                                                                                    .agentId())
                                                                                    .sysPrompt(
                                                                                            release
                                                                                                    .systemPrompt()),
                                                                    true),
                                                    agent -> {
                                                        var context =
                                                                IsolatedAgentFactory.context(scope);
                                                        return agent.call(message, context)
                                                                .timeout(timeout)
                                                                .doOnCancel(
                                                                        () ->
                                                                                agent.interrupt(
                                                                                        context))
                                                                .map(
                                                                        answer -> {
                                                                            requireHealthy(fatal);
                                                                            var state =
                                                                                    store
                                                                                            .getVersioned(
                                                                                                    scope
                                                                                                            .userId(),
                                                                                                    scope
                                                                                                            .sessionId(),
                                                                                                    "agent_state",
                                                                                                    AgentState
                                                                                                            .class);
                                                                            if (!state.isPresent())
                                                                                throw new Blocked(
                                                                                        "child_incomplete");
                                                                            var completed =
                                                                                    AgentStateInspector
                                                                                            .classify(
                                                                                                    state
                                                                                                            .value(),
                                                                                                    state
                                                                                                            .version());
                                                                            if (!completed
                                                                                    .status()
                                                                                    .equals(
                                                                                            "completed"))
                                                                                throw new Blocked(
                                                                                        "child_incomplete");
                                                                            return result(
                                                                                    invocation,
                                                                                    completed
                                                                                            .result());
                                                                        });
                                                    },
                                                    agent -> agent.close()),
                                    Models::close);
                        })
                .onErrorMap(
                        error ->
                                error instanceof Blocked
                                        ? error
                                        : new Blocked("private_agent_failed"));
    }

    private ToolResultBlock result(Invocation invocation, String answer) {
        if (answer == null || answer.isBlank()) throw new Blocked("child_incomplete");
        return ToolResultBlock.text(
                PayloadJson.canonical(calls.complete(invocation, () -> Map.of("answer", answer))));
    }

    /** 不逐 token 查数据库：只在模型派发和检查点／结果边界核验父栅栏。 */
    private Model bounded(
            Model delegate,
            ModelProfile profile,
            Invocation invocation,
            AtomicReference<Blocked> fatal) {
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
                                    requireHealthy(fatal);
                                    if (!tools.isEmpty())
                                        throw remember(fatal, "child_tools_denied");
                                    int available =
                                            budgets.effectiveWorkBytes(
                                                    profile,
                                                    messages.stream()
                                                            .filter(
                                                                    m ->
                                                                            m.getRole()
                                                                                    == MsgRole
                                                                                            .SYSTEM)
                                                            .toList(),
                                                    "",
                                                    tools);
                                    int work =
                                            messages.stream()
                                                    .filter(m -> m.getRole() != MsgRole.SYSTEM)
                                                    .mapToInt(
                                                            m ->
                                                                    ContextBudgets.bytes(
                                                                                    m
                                                                                            .getTextContent())
                                                                            + 128)
                                                    .sum();
                                    if (work > available)
                                        throw remember(fatal, "context_budget_exceeded");
                                    calls.reserveModel(invocation);
                                    String responseId = UUID.randomUUID().toString();
                                    return delegate.stream(messages, tools, options)
                                            .map(
                                                    response -> {
                                                        if (response.getContent().stream()
                                                                .anyMatch(
                                                                        ToolUseBlock.class
                                                                                ::isInstance)) {
                                                            // 即使 SDK 将模型异常转换为观察或尝试
                                                            // fallback，也不能继续伪装成合法叶子答复。
                                                            throw remember(
                                                                    fatal, "child_tools_denied");
                                                        }
                                                        return response.withId(responseId);
                                                    });
                                })
                        .doOnError(
                                error -> {
                                    if (error instanceof Blocked blocked)
                                        fatal.compareAndSet(null, blocked);
                                });
            }
        };
    }

    private static void requireHealthy(AtomicReference<Blocked> fatal) {
        if (fatal.get() != null) throw fatal.get();
    }

    private static Blocked remember(AtomicReference<Blocked> fatal, String code) {
        var blocked = new Blocked(code);
        fatal.compareAndSet(null, blocked);
        return blocked;
    }

    private Models models(ResolvedAgentRelease release) {
        var primary = createModel(release.definition().primary());
        try {
            return new Models(
                    primary,
                    release.definition().fallback() == null
                            ? null
                            : createModel(release.definition().fallback()));
        } catch (RuntimeException failure) {
            primary.close();
            throw failure;
        }
    }

    /** 离线实现同样只读取传入的子消息，不能复用回显父 Turn 的主 Agent 模型。 */
    protected ModelFactory.Handle createModel(ModelProfile profile) {
        if (!profile.name().equals("offline"))
            return ModelFactory.create(profile, credentials.modelApiKey());
        return new ModelFactory.Handle() {
            @Override
            public void close() {}

            @Override
            public String getModelName() {
                return "private-agent-offline-v1";
            }

            @Override
            public Flux<ChatResponse> stream(
                    List<Msg> messages, List<ToolSchema> tools, GenerateOptions options) {
                String input =
                        messages.stream()
                                .filter(m -> m.getRole() == MsgRole.USER)
                                .reduce((previous, current) -> current)
                                .map(Msg::getTextContent)
                                .orElse("");
                return Flux.just(
                        ChatResponse.builder()
                                .id(UUID.randomUUID().toString())
                                .content(
                                        List.of(TextBlock.builder().text("已收到委派：" + input).build()))
                                .build());
            }
        };
    }

    private record Models(ModelFactory.Handle primary, ModelFactory.Handle fallback) {
        void close() {
            try {
                primary.close();
            } finally {
                if (fallback != null) fallback.close();
            }
        }
    }
}
