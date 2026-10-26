package com.financeclaw.infrastructure.agentscope.execution;

import com.financeclaw.app.agent.port.AgentAccessGateway;
import com.financeclaw.app.agent.port.CredentialAccessGateway;
import com.financeclaw.app.artifact.ArtifactService;
import com.financeclaw.app.execution.ExecutionLeaseService;
import com.financeclaw.app.execution.ExecutionPolicyService;
import com.financeclaw.app.execution.port.ExecutionContextGateway;
import com.financeclaw.app.portfolio.PortfolioReviewService;
import com.financeclaw.app.release.ReleaseService;
import com.financeclaw.app.release.port.Credentials;
import com.financeclaw.app.report.ReportService;
import com.financeclaw.domain.Governance.*;
import com.financeclaw.domain.Task.*;
import com.financeclaw.domain.agent.AgentDraft;
import com.financeclaw.infrastructure.agentscope.AgentStateInspector;
import com.financeclaw.infrastructure.agentscope.IsolatedAgentFactory;
import com.financeclaw.infrastructure.agentscope.ResumeInputs;
import com.financeclaw.infrastructure.agentscope.capability.BusinessCapabilities;
import com.financeclaw.infrastructure.agentscope.capability.LocalCapabilityCatalog;
import com.financeclaw.infrastructure.agentscope.context.*;
import com.financeclaw.infrastructure.agentscope.model.ModelFactory;
import com.financeclaw.infrastructure.state.MyBatisAgentStateStore;
import com.financeclaw.infrastructure.state.RuntimeMapper;
import com.financeclaw.support.PayloadJson;
import io.agentscope.core.ReActAgent;
import io.agentscope.core.event.*;
import io.agentscope.core.message.*;
import io.agentscope.core.model.*;
import io.agentscope.core.permission.*;
import io.agentscope.core.state.*;
import java.time.Duration;
import java.util.*;
import java.util.concurrent.ConcurrentHashMap;
import java.util.concurrent.atomic.AtomicBoolean;
import java.util.concurrent.atomic.AtomicReference;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.core.env.Environment;
import org.springframework.stereotype.Component;
import reactor.core.publisher.Flux;
import reactor.core.publisher.Sinks;

/**
 * 将持久业务执行绑定到 AgentScope ReAct 运行时。
 *
 * <p>每个 attempt 有独立 Agent、工具、状态 scope 和停止信号。长期记忆只在 Turn 受理时冻结， 恢复沿用同一正文；工作上下文跨 Turn
 * 延续，接近预算时压缩，过大工具结果先归档成文件引用。 SDK 检查点与会话投影共用执行栅栏事务，业务完成仍以封存回执为准。
 */
@Component
public class TaskAgentRuntime implements com.financeclaw.app.execution.port.AgentExecutionPort {
    private com.financeclaw.infrastructure.runtime.RuntimeObservations observations =
            com.financeclaw.infrastructure.runtime.RuntimeObservations.disabled();

    @Autowired(required = false)
    public void configureObservations(
            com.financeclaw.infrastructure.runtime.RuntimeObservations observations) {
        this.observations = observations;
    }

    private CredentialAccessGateway credentialAccess = CredentialAccessGateway.trustedFixture();

    @Autowired
    public void configureCredentialAccess(CredentialAccessGateway access) {
        this.credentialAccess = access;
    }

    private AgentAccessGateway agentAccess = AgentAccessGateway.ownedOnly();

    @Autowired
    public void configureAgentAccess(AgentAccessGateway access) {
        this.agentAccess = access;
    }

    private ContextBudgets budgets = ContextBudgets.defaults();
    private final ExecutionPolicyService policy;
    private final ExecutionLeaseService leases;
    private final ReleaseService releases;
    private final Credentials credentials;
    private final ExecutionContextGateway context;
    private final RuntimeMapper states;
    private final Duration timeout;
    private final ArtifactService artifacts;
    private final ReportService reports;
    private final BusinessCapabilities capabilities;

    private record Active(ReActAgent agent, Sinks.Empty<Void> stop, AtomicBoolean interrupted) {}

    private final Map<String, Active> active = new ConcurrentHashMap<>();

    @Autowired
    void configureContext(Environment environment) {
        budgets = ContextBudgets.from(environment);
    }

    public TaskAgentRuntime(
            ExecutionPolicyService policy,
            ExecutionLeaseService leases,
            ReleaseService releases,
            Credentials credentials,
            ExecutionContextGateway context,
            ArtifactService artifacts,
            ReportService reports,
            RuntimeMapper states,
            BusinessCapabilities capabilities,
            @Value("${financeclaw.worker.model-timeout-ms:30000}") long timeoutMillis) {
        this.capabilities = capabilities;
        this.artifacts = artifacts;
        this.reports = reports;
        this.policy = policy;
        this.leases = leases;
        this.releases = releases;
        this.credentials = credentials;
        this.context = context;
        this.states = states;
        this.timeout = Duration.ofMillis(timeoutMillis);
    }

    protected Model model(Claim claim) {
        return new Model() {
            @Override
            public String getModelName() {
                return "financeclaw-offline-v1";
            }

            @Override
            public Flux<ChatResponse> stream(
                    List<Msg> messages, List<ToolSchema> tools, GenerateOptions options) {
                return Flux.defer(
                        () -> {
                            policy.checkDispatch(claim);
                            return Flux.just(
                                    ChatResponse.builder()
                                            .id(claim.command().attemptId())
                                            .content(
                                                    List.of(
                                                            TextBlock.builder()
                                                                    .text(
                                                                            "已收到："
                                                                                    + claim.turn()
                                                                                            .message())
                                                                    .build()))
                                            .build());
                        });
            }
        };
    }

    /** 执行一个已领取 attempt，生命周期内注册进度通道；离开作用域即关闭本次流的生产资格。 */
    @Override
    public Outcome execute(Claim claim) {
        // 先注册 attempt 的进度通道，再核查策略；启动期间发生撤权也能令该通道失效。
        try (var progress = policy.beginProgress(claim)) {
            return executeRegistered(claim);
        }
    }

    private Outcome executeRegistered(Claim claim) {
        policy.checkDispatch(claim);
        var resolved = releases.resolve(claim.turn().releaseHash());
        var authorization =
                agentAccess.requireInvocation(
                        new Owner(claim.turn().tenant(), claim.turn().subject()),
                        claim.turn().agentId(),
                        claim.turn().releaseHash());
        resolved.requireIdentity(authorization.resourceOwner(), claim.turn().agentId());
        var release = resolved.definition();
        LocalCapabilityCatalog.requireCompatible(resolved.plan());
        // 本 attempt 只读既有 Turn 快照，不重新召回或替换长期记忆；恢复也使用同一正文。
        String userContext = policy.memoryContext(claim);
        var budgetProfile =
                release.fallback() != null
                                && ContextBudgets.capacity(release.fallback())
                                        < ContextBudgets.capacity(release.primary())
                        ? release.fallback()
                        : release.primary();
        budgets.checkAdmission(budgetProfile, userContext, claim.turn().message());
        var identity = policy.identity(claim);
        var workContext = new WorkingContextSession(states, claim, identity.grantRevision());
        var scope = claim.scope();
        // SDK 的最大轮数摘要可把模型门禁异常转换为普通 assistant 文本。
        // 保留本 attempt 实际发生的拒绝，既不以“额度刚好用满”推测失败，也不影响合法暂停。
        var rejected = new AtomicReference<RuntimeException>();
        var store =
                new MyBatisAgentStateStore(states, scope) {
                    @Override
                    public long saveIfVersion(
                            String user, String session, String key, State state, long expected) {
                        requireNoRejection(rejected);
                        return leases.fenced(
                                claim,
                                () -> {
                                    requireNoRejection(rejected);
                                    // 与遗忘/撤权持有相同 owner/grant 锁，禁止删除后旧回调重新写回上下文。
                                    policy.checkPolicy(claim);
                                    long version =
                                            super.saveIfVersion(
                                                    user, session, key, state, expected);
                                    if (version != UNVERSIONED) {
                                        workContext.persist((AgentState) state, version);
                                    }
                                    return version;
                                });
                    }
                };
        var stop = Sinks.<Void>empty();
        var interrupted = new AtomicBoolean();
        try (var tools =
                        new GovernedTools(
                                policy,
                                releases,
                                credentials,
                                artifacts,
                                reports,
                                claim,
                                resolved.plan(),
                                credentialAccess);
                var provider =
                        release.primary().name().equals("offline")
                                ? null
                                : ModelFactory.create(
                                        release.primary(), credentials.modelApiKey());
                var backup =
                        release.fallback() == null
                                ? null
                                : ModelFactory.create(
                                        release.fallback(), credentials.modelApiKey())) {
            tools.configureObservations(observations);
            Model delegate = release.primary().name().equals("offline") ? model(claim) : provider;
            tools.business(
                    release.primary().name().equals("offline")
                            ? null
                            : observedModel(claim, provider),
                    capabilities);
            boolean workflow =
                    resolved.agentId().equals(AgentDraft.BUILTIN_AGENT)
                            && claim.turn().message().startsWith(PortfolioReviewService.COMMAND);
            if (workflow) {
                delegate = capabilities.portfolioDriver(claim);
            }
            Model primary = guarded(claim, delegate, stop, !workflow, tools, rejected);
            Model fallback =
                    release.fallback() == null || workflow
                            ? null
                            : guarded(claim, backup, stop, true, tools, rejected);
            var contextMiddleware =
                    new WorkingContextMiddleware(
                            claim,
                            budgetProfile,
                            budgets,
                            userContext,
                            workContext,
                            new ToolResultOffloader(artifacts, budgets.toolResultBytes()),
                            tools::requiredScope,
                            new ModelWorkContextSummarizer(
                                    workflow && provider != null
                                            ? guarded(claim, provider, stop, true, tools, rejected)
                                            : primary,
                                    release.primary().outputReserve(),
                                    timeout,
                                    provider == null),
                            failure -> rejected.compareAndSet(null, failure));
            try (var agent =
                    IsolatedAgentFactory.create(
                            primary,
                            fallback,
                            tools.toolkit,
                            store,
                            tools.permissions(),
                            builder -> {
                                builder.name(resolved.agentId())
                                        .sysPrompt(resolved.systemPrompt())
                                        .maxIters(release.maxModelCalls());
                                tools.configure(builder);
                                builder.middleware(contextMiddleware).hook(contextMiddleware);
                                builder.middleware(
                                        new io.agentscope.core.middleware.MiddlewareBase() {
                                            @Override
                                            public reactor.core.publisher.Mono<String>
                                                    onSystemPrompt(
                                                            io.agentscope.core.agent.Agent agent,
                                                            io.agentscope.core.agent.RuntimeContext
                                                                    context,
                                                            String prompt) {
                                                return reactor.core.publisher.Mono.just(
                                                        prompt + userContext);
                                            }
                                        });
                            },
                            resolved.plan() != null && resolved.plan().capabilities().isEmpty())) {
                active.put(claim.command().attemptId(), new Active(agent, stop, interrupted));
                List<Msg> inputs = inputs(claim, store, tools, workContext);
                agent.streamEvents(
                                inputs,
                                io.agentscope.core.agent.RuntimeContext.builder(
                                                IsolatedAgentFactory.context(scope))
                                        .put(ExecutionIdentity.class, identity)
                                        .build())
                        .publishOn(reactor.core.scheduler.Schedulers.boundedElastic(), 1)
                        .doOnNext(
                                event -> {
                                    if (event instanceof TextBlockDeltaEvent text
                                            && text.getDelta() != null
                                            && !text.getDelta().isEmpty()) {
                                        policy.progress(claim, "text_delta", text.getDelta(), null);
                                    } else if (event instanceof ToolCallStartEvent tool
                                            && agent.getToolkit()
                                                    .getToolNames()
                                                    .contains(tool.getToolCallName())) {
                                        policy.progress(
                                                claim,
                                                "tool_started",
                                                null,
                                                tool.getToolCallName());
                                    } else if (event instanceof ToolResultEndEvent tool
                                            && agent.getToolkit()
                                                    .getToolNames()
                                                    .contains(tool.getToolCallName())) {
                                        policy.progress(
                                                claim,
                                                tool.getState() == ToolResultState.SUCCESS
                                                        ? "tool_completed"
                                                        : "tool_failed",
                                                null,
                                                tool.getToolCallName());
                                    }
                                })
                        // 停止信号覆盖完整 SDK 订阅，不能只停止父模型流：父 Agent 在等待子工具时，
                        // 模型流已经结束，单独停止它不会取消子 Agent 的 HTTP 请求。AgentScope 2.0.3
                        // 的 streamEvents.onCancel 会 dispose 当前 call，沿 Reactor 传递到子调用；
                        // cancelled 终态仍由 ExecuteTaskService 核验持久取消回执后确认。
                        .takeUntilOther(stop.asMono())
                        .blockLast(timeout);
                // 停止订阅会正常结束事件流，但不代表业务执行成功。先退出本次 attempt，
                // 避免把中断前的部分检查点解释为 waiting/completed；持久取消确认仍走业务层。
                if (interrupted.get()) {
                    throw new Fenced();
                }
                requireNoRejection(rejected);
                tools.checkCompatibility();
                var saved =
                        store.getVersioned(
                                scope.userId(), scope.sessionId(), "agent_state", AgentState.class);
                // SDK 可能把工具中的策略异常转换为普通错误结果；持久结果提交前必须再次核查。
                leases.fenced(
                        claim,
                        () -> {
                            policy.checkPolicy(claim);
                            return null;
                        });
                var outcome = classify(saved.value(), saved.version());
                return outcome.status().equals("waiting")
                        ? new Outcome(
                                "waiting", null, saved.version(), tools.pending(saved.value()))
                        : outcome;
            }
        } catch (RuntimeException failure) {
            // SDK 或检查点适配器可能包装原异常；业务终态必须保留实际拒绝原因。
            requireNoRejection(rejected);
            throw failure;
        } finally {
            active.remove(claim.command().attemptId());
        }
    }

    /** Workflow内有自己的预算门禁，这里只包装其真实供应商调用，不重复扣额。 */
    private Model observedModel(Claim claim, Model delegate) {
        return new Model() {
            @Override
            public String getModelName() {
                return delegate.getModelName();
            }

            @Override
            public Flux<ChatResponse> stream(
                    List<Msg> messages, List<ToolSchema> tools, GenerateOptions options) {
                return observations.model(claim, () -> delegate.stream(messages, tools, options));
            }
        };
    }

    private Model guarded(
            Claim claim,
            Model delegate,
            Sinks.Empty<Void> stop,
            boolean countModel,
            GovernedTools governedTools,
            AtomicReference<RuntimeException> rejected) {
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
                                    requireNoRejection(rejected);
                                    governedTools.checkCompatibility();
                                    if (countModel) {
                                        policy.consume(claim, true);
                                    }
                                    // 供应商/固定Driver可能在多次调用中复用响应ID（例如workflow）。
                                    // 每次模型派发生成服务端消息ID，同一流的所有chunk沿用，防止轨迹覆盖。
                                    String responseId = UUID.randomUUID().toString();
                                    // 固定workflow Driver不是供应商模型调用；它只负责生成本地工具步骤。
                                    Flux<ChatResponse> response =
                                            countModel
                                                    ? observations.model(
                                                            claim,
                                                            () ->
                                                                    delegate.stream(
                                                                            messages, tools,
                                                                            options))
                                                    : delegate.stream(messages, tools, options);
                                    return response.map(chunk -> chunk.withId(responseId));
                                })
                        .doOnError(
                                failure -> {
                                    if (failure instanceof Blocked || failure instanceof Fenced)
                                        rejected.compareAndSet(null, (RuntimeException) failure);
                                })
                        .takeUntilOther(stop.asMono());
            }
        };
    }

    private static void requireNoRejection(AtomicReference<RuntimeException> rejected) {
        var failure = rejected.get();
        if (failure != null) throw failure;
    }

    /** 为新执行构造受限历史，或从真实检查点生成批准/补充输入。 恢复会重新绑定到新 attempt scope，并通过 CAS 克隆；旧 scope 不继续承担新执行。 */
    private List<Msg> inputs(
            Claim claim,
            MyBatisAgentStateStore store,
            GovernedTools tools,
            WorkingContextSession workContext) {
        // 新 Turn 延续完整工作投影（其中可有结构化摘要），不再只拼接最近问答对。
        if (claim.command().resumeId() == null) {
            List<Msg> history =
                    new ArrayList<>(
                            leases.fenced(
                                    claim,
                                    () -> {
                                        policy.checkPolicy(claim);
                                        return workContext.previous();
                                    }));
            history.add(
                    Msg.builder()
                            .id(workContext.inputId())
                            .name("user")
                            .role(MsgRole.USER)
                            .textContent(claim.turn().message())
                            .build());
            workContext.record(history);
            return history;
        }
        // 恢复时从已回答交互定位旧 attempt，禁止使用客户端拼出的工具调用或状态。
        policy.checkDispatch(claim);
        {
            var anchor = context.interaction(claim.command().resumeId());
            if (anchor == null || !anchor.status().equals("answered") || !anchor.live()) {
                throw new Blocked("stale_interaction");
            }
            var sourceClaim =
                    new Claim(
                            new Command(
                                    anchor.commandId(),
                                    anchor.turnId(),
                                    "done",
                                    anchor.attemptId(),
                                    0,
                                    false,
                                    null),
                            claim.turn());
            var source = sourceClaim.scope();
            var original = new MyBatisAgentStateStore(states, source);
            var saved =
                    original.getVersioned(
                            source.userId(), source.sessionId(), "agent_state", AgentState.class);
            if (!saved.isPresent() || saved.version() != anchor.stateVersion()) {
                throw new Blocked("checkpoint_mismatch");
            }
            List<Msg> replies = new ArrayList<>();
            var answered =
                    context.interactions(claim.turn().id()).stream()
                            .filter(i -> i.attemptId().equals(anchor.attemptId()))
                            .toList();
            tools.prepareDelegatedResume(sourceClaim, answered);
            for (var i : answered) {
                if (i.attemptId().equals(anchor.attemptId())) {
                    if (!i.status().equals("answered")
                            || !i.live()
                            || i.grantRevision() != anchor.grantRevision()) {
                        throw new Blocked("stale_interaction");
                    }
                    if (tools.delegated(i)) continue;
                    var answer = PayloadJson.object(i.response());
                    replies.add(
                            i.kind().equals("approval")
                                    ? ResumeInputs.approval(
                                            original,
                                            source,
                                            i.stateVersion(),
                                            i.callId(),
                                            i.actionHash(),
                                            "approve".equals(answer.get("decision")))
                                    : tools.resumeInput(original, source, i, answer.get("answer")));
                }
            }
            var state = saved.value();
            // Native ASK handling ignores ordinary tool-result inputs in the same batch.
            // Persist already-validated clarification results before sending only confirmation
            // metadata.
            // Classify inherited history before adding this attempt's newly answered tools.
            // Otherwise resume() would mistake the new answers for historical projection-only data.
            workContext.resume(state.getContext(), anchor.attemptId(), anchor.stateVersion());
            var clarificationReplies =
                    replies.stream().filter(message -> message.getRole() == MsgRole.TOOL).toList();
            var resumedContext = new ArrayList<>(state.getContext());
            resumedContext.addAll(clarificationReplies);
            workContext.record(clarificationReplies);
            var copied =
                    AgentState.builder()
                            .userId(claim.scope().userId())
                            .sessionId(claim.scope().sessionId())
                            .context(resumedContext)
                            .summary(state.getSummary())
                            .permissionContext(tools.permissions())
                            .toolContext(state.getToolContext())
                            .tasksContext(state.getTasksContext())
                            .planModeContext(state.getPlanModeContext())
                            .build();
            store.saveIfVersion(
                    claim.scope().userId(), claim.scope().sessionId(), "agent_state", copied, 0);
            policy.checkDispatch(claim);
            return replies.stream().filter(message -> message.getRole() != MsgRole.TOOL).toList();
        }
    }

    @Override
    public void interrupt(Claim claim) {
        var agent = active.get(claim.command().attemptId());
        if (agent != null) {
            // 先记录停止意图，保证事件流结束后不会继续解释旧检查点。SDK 的协作式中断
            // 与 Reactor 订阅取消同时执行；即使 SDK 中断异常，也必须释放正在等待的子调用。
            agent.interrupted().set(true);
            try {
                agent.agent().interrupt(IsolatedAgentFactory.context(claim.scope()));
            } finally {
                agent.stop().tryEmitEmpty();
            }
        }
    }

    public static Outcome classify(AgentState state, long version) {
        return AgentStateInspector.classify(state, version);
    }
}
