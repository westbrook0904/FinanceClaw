package com.financeclaw.infrastructure.agentscope.execution;

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
import com.financeclaw.domain.release.ModelProfile;
import com.financeclaw.infrastructure.agentscope.AgentStateInspector;
import com.financeclaw.infrastructure.agentscope.IsolatedAgentFactory;
import com.financeclaw.infrastructure.agentscope.ResumeInputs;
import com.financeclaw.infrastructure.agentscope.capability.BusinessCapabilities;
import com.financeclaw.infrastructure.state.MyBatisAgentStateStore;
import com.financeclaw.infrastructure.state.RuntimeMapper;
import com.financeclaw.support.PayloadJson;
import io.agentscope.core.ReActAgent;
import io.agentscope.core.event.*;
import io.agentscope.core.message.*;
import io.agentscope.core.model.*;
import io.agentscope.core.permission.*;
import io.agentscope.core.state.*;
import java.nio.charset.StandardCharsets;
import java.time.Duration;
import java.util.*;
import java.util.concurrent.ConcurrentHashMap;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.stereotype.Component;
import reactor.core.publisher.Flux;
import reactor.core.publisher.Sinks;

/**
 * 将持久业务执行绑定到 AgentScope ReAct 运行时。
 *
 * <p>每个 attempt 有独立 Agent、工具、状态 scope 和停止信号。人工画像与证据上下文 每次尝试读取一次，作为请求数据注入 prompt，不写入 SDK
 * 检查点；每次模型/工具派发 仍检查授权和版本。检查点写入、恢复和最终业务提交分别经过 fence，流结束不代表业务完成。
 */
@Component
public class TaskAgentRuntime implements com.financeclaw.app.execution.port.AgentExecutionPort {
    private static final int MAX_HISTORY_BYTES = 8192;
    private static final int HISTORY_SPARE_CAPACITY_DIVISOR = 4;
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

    private record Active(ReActAgent agent, Sinks.Empty<Void> stop) {}

    private final Map<String, Active> active = new ConcurrentHashMap<>();

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
        var release = releases.require(claim.turn().releaseHash());
        // 请求级记忆只读一次，避免逐 token 查询；之后每次派发和最终提交仍验证绑定版本。
        String userContext = policy.memoryContext(claim);
        int historyBudget = historyBudget(release.primary(), userContext, claim.turn().message());
        var scope = claim.scope();
        var store =
                new MyBatisAgentStateStore(states, scope) {
                    @Override
                    public long saveIfVersion(
                            String user, String session, String key, State state, long expected) {
                        return leases.fenced(
                                claim,
                                () -> super.saveIfVersion(user, session, key, state, expected));
                    }
                };
        var stop = Sinks.<Void>empty();
        try (var tools =
                        new GovernedTools(
                                policy, releases, credentials, artifacts, reports, claim);
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
            Model delegate = release.primary().name().equals("offline") ? model(claim) : provider;
            tools.business(
                    release.primary().name().equals("offline") ? null : provider, capabilities);
            boolean workflow = claim.turn().message().startsWith(PortfolioReviewService.COMMAND);
            if (workflow) {
                delegate = capabilities.portfolioDriver(claim);
            }
            Model primary = guarded(claim, delegate, stop, !workflow);
            Model fallback =
                    release.fallback() == null || workflow
                            ? null
                            : guarded(claim, backup, stop, true);
            try (var agent =
                    IsolatedAgentFactory.create(
                            primary,
                            fallback,
                            tools.toolkit,
                            store,
                            tools.permissions(),
                            builder -> {
                                tools.configure(builder);
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
                            })) {
                active.put(claim.command().attemptId(), new Active(agent, stop));
                List<Msg> inputs = inputs(claim, store, tools, historyBudget);
                agent.streamEvents(
                                inputs,
                                io.agentscope.core.agent.RuntimeContext.builder(
                                                IsolatedAgentFactory.context(scope))
                                        .put(ExecutionIdentity.class, policy.identity(claim))
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
                        .blockLast(timeout);
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
        } finally {
            active.remove(claim.command().attemptId());
        }
    }

    private Model guarded(Claim claim, Model delegate, Sinks.Empty<Void> stop, boolean countModel) {
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
                                    if (countModel) {
                                        policy.consume(claim, true);
                                    }
                                    return delegate.stream(messages, tools, options);
                                })
                        .takeUntilOther(stop.asMono());
            }
        };
    }

    /** 先为完整画像和当前请求预留容量，再计算历史上限；必需内容超过预算时显式阻断。 */
    private static int historyBudget(ModelProfile model, String userContext, String message) {
        long capacity =
                Math.min(model.inputLimit(), model.window() - model.outputReserve())
                        - model.margin();
        long mandatory =
                (long) userContext.getBytes(StandardCharsets.UTF_8).length
                        + message.getBytes(StandardCharsets.UTF_8).length;
        if (mandatory > capacity) {
            throw new Blocked("context_budget_exceeded");
        }
        // 完整画像和当前请求优先保留，历史只使用剩余预算的一部分。
        // 最终传输预算还会计算 system、工具 schema 和 JSON 封装，不能只检查用户消息。
        return (int)
                Math.min(
                        MAX_HISTORY_BYTES, (capacity - mandatory) / HISTORY_SPARE_CAPACITY_DIVISOR);
    }

    /** 为新执行构造受限历史，或从真实检查点生成批准/补充输入。 恢复会重新绑定到新 attempt scope，并通过 CAS 克隆；旧 scope 不继续承担新执行。 */
    private List<Msg> inputs(
            Claim claim, MyBatisAgentStateStore store, GovernedTools tools, int historyBudget) {
        // 新尝试只读取通过 release/grant/记忆版本过滤的已完成对话。
        if (claim.command().resumeId() == null) {
            List<Msg> history = new ArrayList<>();
            int used = 0;
            for (var previous : policy.history(claim)) {
                int bytes =
                        PayloadJson.canonical(List.of(previous.message(), previous.result()))
                                .getBytes(StandardCharsets.UTF_8)
                                .length;
                if (used + bytes > historyBudget) {
                    break;
                }
                history.addFirst(
                        Msg.builder()
                                .name("assistant")
                                .role(MsgRole.ASSISTANT)
                                .textContent(previous.result())
                                .build());
                history.addFirst(
                        Msg.builder()
                                .name("user")
                                .role(MsgRole.USER)
                                .textContent(previous.message())
                                .build());
                used += bytes;
            }
            history.add(
                    Msg.builder()
                            .name("user")
                            .role(MsgRole.USER)
                            .textContent(claim.turn().message())
                            .build());
            return history;
        }
        // 恢复时从已回答交互定位旧 attempt，禁止使用客户端拼出的工具调用或状态。
        policy.checkDispatch(claim);
        {
            var anchor = context.interaction(claim.command().resumeId());
            if (anchor == null || !anchor.status().equals("answered") || !anchor.live()) {
                throw new Blocked("stale_interaction");
            }
            var source =
                    new Claim(
                                    new Command(
                                            anchor.commandId(),
                                            anchor.turnId(),
                                            "done",
                                            anchor.attemptId(),
                                            0,
                                            false,
                                            null),
                                    claim.turn())
                            .scope();
            var original = new MyBatisAgentStateStore(states, source);
            var saved =
                    original.getVersioned(
                            source.userId(), source.sessionId(), "agent_state", AgentState.class);
            if (!saved.isPresent() || saved.version() != anchor.stateVersion()) {
                throw new Blocked("checkpoint_mismatch");
            }
            List<Msg> replies = new ArrayList<>();
            for (var i : context.interactions(claim.turn().id())) {
                if (i.attemptId().equals(anchor.attemptId())) {
                    if (!i.status().equals("answered")
                            || !i.live()
                            || i.grantRevision() != anchor.grantRevision()) {
                        throw new Blocked("stale_interaction");
                    }
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
            var copied =
                    AgentState.builder()
                            .userId(claim.scope().userId())
                            .sessionId(claim.scope().sessionId())
                            .context(state.getContext())
                            .summary(state.getSummary())
                            .permissionContext(tools.permissions())
                            .toolContext(state.getToolContext())
                            .tasksContext(state.getTasksContext())
                            .planModeContext(state.getPlanModeContext())
                            .build();
            store.saveIfVersion(
                    claim.scope().userId(), claim.scope().sessionId(), "agent_state", copied, 0);
            policy.checkDispatch(claim);
            return replies;
        }
    }

    @Override
    public void interrupt(Claim claim) {
        var agent = active.get(claim.command().attemptId());
        if (agent != null) {
            agent.agent().interrupt(IsolatedAgentFactory.context(claim.scope()));
            // SDK signals alone are only observed between model chunks; also cancel an idle stream.
            agent.stop().tryEmitEmpty();
        }
    }

    public static Outcome classify(AgentState state, long version) {
        return AgentStateInspector.classify(state, version);
    }
}
