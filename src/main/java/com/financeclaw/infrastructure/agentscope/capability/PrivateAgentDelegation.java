package com.financeclaw.infrastructure.agentscope.capability;

import com.financeclaw.app.execution.ChildInvocationService;
import com.financeclaw.app.execution.ChildInvocationService.Invocation;
import com.financeclaw.app.market.MarketDataService;
import com.financeclaw.app.release.port.Credentials;
import com.financeclaw.domain.Governance.Blocked;
import com.financeclaw.domain.Task.Claim;
import com.financeclaw.domain.agent.*;
import com.financeclaw.domain.release.ModelProfile;
import com.financeclaw.infrastructure.agentscope.AgentStateInspector;
import com.financeclaw.infrastructure.agentscope.IsolatedAgentFactory;
import com.financeclaw.infrastructure.agentscope.context.ContextBudgets;
import com.financeclaw.infrastructure.agentscope.context.ToolResultOffloader;
import com.financeclaw.infrastructure.agentscope.model.ModelFactory;
import com.financeclaw.infrastructure.state.RuntimeMapper;
import com.financeclaw.support.ArtifactPayload;
import com.financeclaw.support.PayloadJson;
import com.financeclaw.support.PrivateAgentContracts;
import io.agentscope.core.agent.Agent;
import io.agentscope.core.agent.RuntimeContext;
import io.agentscope.core.event.AgentEvent;
import io.agentscope.core.message.*;
import io.agentscope.core.middleware.ActingInput;
import io.agentscope.core.middleware.MiddlewareBase;
import io.agentscope.core.model.*;
import io.agentscope.core.permission.PermissionBehavior;
import io.agentscope.core.permission.PermissionContextState;
import io.agentscope.core.permission.PermissionMode;
import io.agentscope.core.permission.PermissionRule;
import io.agentscope.core.state.AgentState;
import io.agentscope.core.tool.ToolBase;
import io.agentscope.core.tool.ToolCallParam;
import io.agentscope.core.tool.ToolValidator;
import io.agentscope.core.tool.Toolkit;
import io.agentscope.core.util.JsonUtils;
import java.time.Duration;
import java.util.*;
import java.util.concurrent.atomic.AtomicBoolean;
import java.util.concurrent.atomic.AtomicReference;
import java.util.function.Function;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.core.env.Environment;
import org.springframework.stereotype.Component;
import org.springframework.transaction.support.TransactionSynchronizationManager;
import reactor.core.publisher.Flux;
import reactor.core.publisher.Mono;
import reactor.core.scheduler.Schedulers;

/** 将发布时冻结的叶子目标装配为独立原生 ReActAgent。只传显式 message 和目标提示词， 不读取父画像、会话历史或工具结果；父 Turn 仍独占调度、取消和持久计数。 */
@Component
public class PrivateAgentDelegation {
    private final ChildInvocationService calls;
    private final RuntimeMapper states;
    private final Credentials credentials;
    private final MarketDataService market;
    private ContextBudgets budgets = ContextBudgets.defaults();
    private Duration timeout = Duration.ofSeconds(30);

    @Autowired
    public PrivateAgentDelegation(
            ChildInvocationService calls,
            RuntimeMapper states,
            Credentials credentials,
            MarketDataService market) {
        this.calls = calls;
        this.states = states;
        this.credentials = credentials;
        this.market = market;
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
                            var toolkit = toolkit(target, invocation, release, fatal);
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
                                                                            target,
                                                                            fatal),
                                                                    models.fallback() == null
                                                                            ? null
                                                                            : bounded(
                                                                                    models
                                                                                            .fallback(),
                                                                                    release.definition()
                                                                                            .fallback(),
                                                                                    invocation,
                                                                                    target,
                                                                                    fatal),
                                                                    toolkit,
                                                                    store,
                                                                    permissions(target),
                                                                    builder ->
                                                                            builder.name(
                                                                                            target
                                                                                                    .agentId())
                                                                                    .sysPrompt(
                                                                                            release
                                                                                                    .systemPrompt())
                                                                                    .middleware(
                                                                                            guard(
                                                                                                    target,
                                                                                                    fatal)),
                                                                    target.profile()
                                                                            == PrivateAgentTarget
                                                                                    .Profile.EMPTY),
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
            PrivateAgentTarget target,
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
                                    if (!allowedSchemas(target, tools))
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
                                    // 工具参数、结果及 callId 同样占窗口，不能只累计 getTextContent。
                                    int work =
                                            ContextBudgets.bytes(
                                                    JsonUtils.getJsonCodec()
                                                            .toJson(
                                                                    messages.stream()
                                                                            .filter(
                                                                                    m ->
                                                                                            m
                                                                                                            .getRole()
                                                                                                    != MsgRole
                                                                                                            .SYSTEM)
                                                                            .toList()));
                                    if (work > available)
                                        throw remember(fatal, "context_budget_exceeded");
                                    calls.reserveModel(invocation);
                                    String responseId = UUID.randomUUID().toString();
                                    var marketHeader = new AtomicBoolean();
                                    return delegate.stream(messages, tools, options)
                                            .map(
                                                    response -> {
                                                        if (response.getContent().stream()
                                                                .filter(
                                                                        ToolUseBlock.class
                                                                                ::isInstance)
                                                                .map(ToolUseBlock.class::cast)
                                                                .anyMatch(
                                                                        call ->
                                                                                !allowedChunk(
                                                                                        target,
                                                                                        call,
                                                                                        marketHeader))) {
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

    /** v2 只执行固定行情能力；发布中的 reader 编译依赖不会注册到子 Toolkit。 */
    private Toolkit toolkit(
            PrivateAgentTarget target,
            Invocation invocation,
            ResolvedAgentRelease release,
            AtomicReference<Blocked> fatal) {
        var toolkit = new Toolkit();
        if (target.profile() == PrivateAgentTarget.Profile.EMPTY) return toolkit;
        if (market == null) throw new Blocked("agent_delegation_unavailable");
        toolkit.registerAgentTool(
                new ToolBase(
                        ToolBase.builder()
                                .name("market_snapshot")
                                .description("Governed market_snapshot")
                                .inputSchema(MarketToolSchemas.SNAPSHOT)) {
                    @Override
                    public Mono<ToolResultBlock> callAsync(ToolCallParam param) {
                        return Mono.fromCallable(
                                        () -> {
                                            requireHealthy(fatal);
                                            if (ToolValidator.validateInput(
                                                            PayloadJson.canonical(param.getInput()),
                                                            MarketToolSchemas.SNAPSHOT)
                                                    != null) {
                                                throw remember(fatal, "invalid_tool_input");
                                            }
                                            // 真实父 Claim 继续负责授权与根额度；正文取 begin 已校验的目标冻结发布。
                                            var result =
                                                    ToolResultBlock.text(
                                                            PayloadJson.canonical(
                                                                    calls.privateSnapshot(
                                                                            invocation,
                                                                            param.getToolUseBlock()
                                                                                    .getId(),
                                                                            param.getInput(),
                                                                            () ->
                                                                                    market
                                                                                            .readFrozen(
                                                                                                    invocation
                                                                                                            .parent(),
                                                                                                    target,
                                                                                                    release,
                                                                                                    (String)
                                                                                                            param.getInput()
                                                                                                                    .get(
                                                                                                                            "symbol")))));
                                            // 按父归档相同的完整 SDK 编码验算，包含最终补齐的 callId/name 和转义成本。
                                            // 固定快照低于最小归档阈值；若未来字段意外膨胀，阻断而非丢正文或给无 reader 的子引用。
                                            var wrapped =
                                                    new ToolResultBlock(
                                                            param.getToolUseBlock().getId(),
                                                            "market_snapshot",
                                                            result.getOutput(),
                                                            result.getMetadata(),
                                                            result.getState());
                                            if (ArtifactPayload.encode(
                                                                    PayloadJson.parse(
                                                                            JsonUtils.getJsonCodec()
                                                                                    .toJson(
                                                                                            wrapped)))
                                                            .bytes()
                                                    >= ToolResultOffloader.MIN_THRESHOLD_BYTES) {
                                                throw remember(
                                                        fatal, "child_tool_result_budget_exceeded");
                                            }
                                            return result;
                                        })
                                .onErrorMap(
                                        error ->
                                                error instanceof Blocked
                                                        ? error
                                                        : new Blocked("private_agent_failed"))
                                .doOnError(
                                        error -> {
                                            if (error instanceof Blocked blocked)
                                                fatal.compareAndSet(null, blocked);
                                        })
                                .subscribeOn(Schedulers.boundedElastic());
                    }
                });
        return toolkit;
    }

    private static PermissionContextState permissions(PrivateAgentTarget target) {
        var builder = PermissionContextState.builder().mode(PermissionMode.DEFAULT);
        if (target.profile() == PrivateAgentTarget.Profile.MARKET_SNAPSHOT) {
            builder.addAllowRule(
                    "market_snapshot",
                    new PermissionRule(
                            "market_snapshot",
                            null,
                            PermissionBehavior.ALLOW,
                            "frozen-private-market-profile"));
        }
        return builder.build();
    }

    /** SDK 2.0.3 用 __fragment__ 表示无名称的参数续片；它不是可执行的工具名。 */
    private static boolean allowedChunk(
            PrivateAgentTarget target, ToolUseBlock call, AtomicBoolean marketHeader) {
        if (target.profile() != PrivateAgentTarget.Profile.MARKET_SNAPSHOT) return false;
        if ("market_snapshot".equals(call.getName())) {
            marketHeader.set(true);
            return true;
        }
        return marketHeader.get()
                && "__fragment__".equals(call.getName())
                && (call.getId() == null || call.getId().isEmpty());
    }

    /** 完成参数合并后再次核验实际派发，既不把续片当工具，也不依赖 SDK 的错误观察阻断执行。 */
    private static MiddlewareBase guard(PrivateAgentTarget target, AtomicReference<Blocked> fatal) {
        return new MiddlewareBase() {
            @Override
            public Flux<AgentEvent> onActing(
                    Agent agent,
                    RuntimeContext context,
                    ActingInput input,
                    Function<ActingInput, Flux<AgentEvent>> next) {
                return Flux.defer(
                        () -> {
                            requireHealthy(fatal);
                            for (var call : input.toolCalls()) {
                                if (target.profile() != PrivateAgentTarget.Profile.MARKET_SNAPSHOT
                                        || !"market_snapshot".equals(call.getName())) {
                                    throw remember(fatal, "child_tools_denied");
                                }
                            }
                            return next.apply(input);
                        });
            }
        };
    }

    private static boolean allowedSchemas(PrivateAgentTarget target, List<ToolSchema> tools) {
        if (target.profile() == PrivateAgentTarget.Profile.EMPTY) return tools.isEmpty();
        return tools.size() == 1
                && "market_snapshot".equals(tools.getFirst().getName())
                && PayloadJson.canonical(tools.getFirst().getParameters())
                        .equals(PayloadJson.canonical(MarketToolSchemas.SNAPSHOT));
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
