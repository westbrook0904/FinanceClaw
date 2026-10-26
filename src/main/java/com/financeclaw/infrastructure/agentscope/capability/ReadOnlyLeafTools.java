package com.financeclaw.infrastructure.agentscope.capability;

import com.financeclaw.app.agent.port.CredentialAccessGateway;
import com.financeclaw.app.artifact.ArtifactService;
import com.financeclaw.app.execution.ChildInvocationService;
import com.financeclaw.app.execution.ChildInvocationService.Invocation;
import com.financeclaw.app.execution.ExecutionPolicyService;
import com.financeclaw.app.market.MarketDataService;
import com.financeclaw.app.release.port.Credentials;
import com.financeclaw.app.report.ReportService;
import com.financeclaw.domain.ExecutionScope;
import com.financeclaw.domain.Governance.Blocked;
import com.financeclaw.domain.Governance.Pending;
import com.financeclaw.domain.Task.Fenced;
import com.financeclaw.domain.agent.*;
import com.financeclaw.infrastructure.agentscope.context.ToolResultOffloader;
import com.financeclaw.infrastructure.agentscope.skills.PackagedSkills;
import com.financeclaw.support.PayloadJson;
import com.financeclaw.support.PrivateAgentContracts;
import io.agentscope.core.ReActAgent;
import io.agentscope.core.agent.Agent;
import io.agentscope.core.agent.RuntimeContext;
import io.agentscope.core.event.AgentEvent;
import io.agentscope.core.message.*;
import io.agentscope.core.middleware.ActingInput;
import io.agentscope.core.middleware.MiddlewareBase;
import io.agentscope.core.middleware.ModelCallInput;
import io.agentscope.core.model.ToolSchema;
import io.agentscope.core.permission.*;
import io.agentscope.core.skill.repository.AgentSkillRepository;
import io.agentscope.core.tool.*;
import io.agentscope.core.util.JsonUtils;
import java.util.*;
import java.util.concurrent.atomic.AtomicReference;
import java.util.function.Function;
import org.springframework.stereotype.Component;
import reactor.core.publisher.Flux;
import reactor.core.publisher.Mono;
import reactor.core.scheduler.Schedulers;

/** The root owns authorization and budgets; the frozen child plan owns tool selection and data. */
@Component
public final class ReadOnlyLeafTools {
    private CredentialAccessGateway credentialAccess = CredentialAccessGateway.trustedFixture();

    @org.springframework.beans.factory.annotation.Autowired
    public void configureCredentialAccess(CredentialAccessGateway access) {
        this.credentialAccess = access;
    }

    private com.financeclaw.infrastructure.runtime.RuntimeObservations observations =
            com.financeclaw.infrastructure.runtime.RuntimeObservations.disabled();

    @org.springframework.beans.factory.annotation.Autowired(required = false)
    public void configureObservations(
            com.financeclaw.infrastructure.runtime.RuntimeObservations observations) {
        this.observations = observations;
    }

    private final ChildInvocationService calls;
    private final ExecutionPolicyService policies;
    private final Credentials credentials;
    private final ArtifactService artifacts;
    private final MarketDataService market;
    private final ReportService reports;
    private final ChildInteractionBridge bridge;

    public ReadOnlyLeafTools(
            ChildInvocationService calls,
            ExecutionPolicyService policies,
            Credentials credentials,
            ArtifactService artifacts,
            MarketDataService market,
            ReportService reports,
            ChildInteractionBridge bridge) {
        this.calls = calls;
        this.policies = policies;
        this.credentials = credentials;
        this.artifacts = artifacts;
        this.market = market;
        this.reports = reports;
        this.bridge = bridge;
    }

    public Session open(
            PrivateAgentTarget target,
            Invocation invocation,
            ResolvedAgentRelease release,
            AtomicReference<RuntimeException> fatal) {
        return new Session(target, invocation, release, fatal);
    }

    public final class Session implements AutoCloseable {
        public final Toolkit toolkit = new Toolkit();
        private Toolkit runtimeToolkit = toolkit;
        private AgentTool wrappedSkill;
        private final Invocation invocation;
        private final AtomicReference<RuntimeException> fatal;
        private final Map<String, CapabilityBinding> selected = new TreeMap<>();
        private final Map<String, InteractionHandler> interactions = new TreeMap<>();
        private final McpToolContribution mcp;
        private AgentSkillRepository skills;
        private Set<String> skillIds = Set.of();
        private List<CapabilityBinding> skillBindings = List.of();
        private final ToolResultOffloader offloader = new ToolResultOffloader(artifacts, 8192);

        private Session(
                PrivateAgentTarget target,
                Invocation invocation,
                ResolvedAgentRelease release,
                AtomicReference<RuntimeException> fatal) {
            this.invocation = invocation;
            this.fatal = fatal;
            if (target.profile() == PrivateAgentTarget.Profile.CONTROLLED)
                PrivateAgentContracts.requireControlled(release.plan());
            else PrivateAgentContracts.requireReadOnly(release.plan());
            LocalCapabilityCatalog.requireCompatible(release.plan());
            release.plan().capabilities().stream()
                    .filter(b -> !b.kind().equals("skill"))
                    .forEach(binding -> selected.put(binding.name(), binding));
            mcp =
                    new McpToolContribution(
                            policies,
                            credentials,
                            artifacts,
                            invocation.parent(),
                            release.definition(),
                            target.scopes(),
                            toolkit,
                            release.plan(),
                            credentialAccess);
            mcp.boundaries(
                    () -> calls.checkpoint(invocation, () -> null),
                    work -> calls.checkpoint(invocation, work));
            try {
                calls.checkpoint(invocation, () -> null);
                new LocalToolContribution(artifacts, reports, invocation.parent(), target.scopes())
                        .contribute(
                                (name, description, schema, scope, action, interaction) -> {
                                    if (!selected.containsKey(name)) return;
                                    if (interaction != null) {
                                        if (target.profile()
                                                != PrivateAgentTarget.Profile.CONTROLLED)
                                            throw new Blocked("child_interaction_requires_root");
                                        interactions.put(name, interaction);
                                    }
                                    if (name.equals("create_report")) {
                                        registerLocal(
                                                name,
                                                description,
                                                schema,
                                                scope,
                                                param ->
                                                        Mono.fromCallable(
                                                                () -> {
                                                                    bridge.requireApproved(
                                                                            invocation,
                                                                            param
                                                                                    .getToolUseBlock());
                                                                    String effectId =
                                                                            "child-"
                                                                                    + ExecutionScope
                                                                                            .hash(
                                                                                                    List
                                                                                                            .of(
                                                                                                                    invocation
                                                                                                                            .callId(),
                                                                                                                    target
                                                                                                                            .agentId(),
                                                                                                                    target
                                                                                                                            .releaseHash(),
                                                                                                                    param.getToolUseBlock()
                                                                                                                            .getId()));
                                                                    String effectHash =
                                                                            ExecutionScope.hash(
                                                                                    List.of(
                                                                                            invocation
                                                                                                    .callId(),
                                                                                            target
                                                                                                    .releaseHash(),
                                                                                            com
                                                                                                    .financeclaw
                                                                                                    .infrastructure
                                                                                                    .agentscope
                                                                                                    .ResumeInputs
                                                                                                    .actionHash(
                                                                                                            param
                                                                                                                    .getToolUseBlock())));
                                                                    return ToolResultBlock.text(
                                                                            reports.report(
                                                                                    invocation
                                                                                            .parent(),
                                                                                    effectId,
                                                                                    effectHash,
                                                                                    (String)
                                                                                            param.getInput()
                                                                                                    .get(
                                                                                                            "title")));
                                                                }));
                                    } else registerLocal(name, description, schema, scope, action);
                                });
                if (selected.containsKey("market_snapshot")) {
                    register(
                            "market_snapshot",
                            "Governed market_snapshot",
                            MarketToolSchemas.SNAPSHOT,
                            "market:read",
                            param ->
                                    Mono.fromCallable(
                                            () ->
                                                    ToolResultBlock.text(
                                                            PayloadJson.canonical(
                                                                    market.readFrozenReserved(
                                                                            invocation.parent(),
                                                                            target,
                                                                            release,
                                                                            (String)
                                                                                    param.getInput()
                                                                                            .get(
                                                                                                    "symbol"))))));
                }
                mcp.contribute(
                        (name, description, schema, scope, action, interaction) -> {
                            if (interaction != null)
                                throw new Blocked("child_interaction_requires_root");
                            register(name, description, schema, scope, action);
                        });
                if (release.plan().capabilities().stream()
                        .anyMatch(b -> b.kind().equals("skill"))) {
                    skillBindings =
                            release.plan().capabilities().stream()
                                    .filter(binding -> binding.kind().equals("skill"))
                                    .toList();
                    skills = PackagedSkills.selected(release.plan().capabilities());
                    skillIds =
                            Set.copyOf(
                                    skills.getAllSkills().stream()
                                            .map(io.agentscope.core.skill.AgentSkill::getSkillId)
                                            .toList());
                }
                if (!toolkit.getToolNames().containsAll(selected.keySet()))
                    throw new Blocked("capability_unavailable");
            } catch (RuntimeException failure) {
                close();
                throw failure;
            }
        }

        public void configure(ReActAgent.Builder builder) {
            if (skills == null) return;
            builder.skillRepository(skills).skillCodeExecutionEnabled(false);
            builder.middleware(
                    new MiddlewareBase() {
                        @Override
                        public Flux<AgentEvent> onModelCall(
                                Agent agent,
                                RuntimeContext context,
                                ModelCallInput input,
                                Function<ModelCallInput, Flux<AgentEvent>> next) {
                            return Flux.defer(
                                    () -> {
                                        // DynamicSkillMiddleware installs the loader during
                                        // onSystemPrompt.
                                        // ReActAgent.Builder copies Toolkit, so wrap the actual
                                        // execution copy.
                                        bindNativeSkills((ReActAgent) agent);
                                        return next.apply(input);
                                    });
                        }

                        @Override
                        public Flux<AgentEvent> onActing(
                                Agent agent,
                                RuntimeContext context,
                                ActingInput input,
                                Function<ActingInput, Flux<AgentEvent>> next) {
                            return Flux.defer(
                                    () -> {
                                        if (input.toolCalls().stream()
                                                .anyMatch(
                                                        call ->
                                                                call.getName()
                                                                        .equals(
                                                                                "load_skill_through_path")))
                                            bindNativeSkills((ReActAgent) agent);
                                        return next.apply(input);
                                    });
                        }
                    });
        }

        private void bindNativeSkills(ReActAgent agent) {
            runtimeToolkit = agent.getToolkit();
            var nativeTool = runtimeToolkit.getTool("load_skill_through_path");
            if (nativeTool == null) {
                var denied = new Blocked("skill_release_changed");
                fatal.compareAndSet(null, denied);
                throw denied;
            }
            if (nativeTool == wrappedSkill) return;
            wrappedSkill =
                    register(
                            runtimeToolkit,
                            "load_skill_through_path",
                            nativeTool.getDescription(),
                            nativeTool.getParameters(),
                            "skill:use",
                            param -> {
                                if (!skillIds.contains(param.getInput().get("skillId")))
                                    throw new Blocked("skill_denied");
                                return nativeTool.callAsync(param);
                            });
        }

        /**
         * Local contributions are synchronous, contain no remote I/O, and share the child
         * authorization transaction.
         */
        private void registerLocal(
                String name,
                String description,
                Map<String, Object> schema,
                String scope,
                Function<ToolCallParam, Mono<ToolResultBlock>> action) {
            register(
                    name,
                    description,
                    schema,
                    scope,
                    param ->
                            Mono.fromCallable(
                                    () ->
                                            calls.checkpoint(
                                                    invocation,
                                                    () -> action.apply(param).block())));
        }

        private void register(
                String name,
                String description,
                Map<String, Object> schema,
                String scope,
                Function<ToolCallParam, Mono<ToolResultBlock>> action) {
            register(toolkit, name, description, schema, scope, action);
        }

        private AgentTool register(
                Toolkit destination,
                String name,
                String description,
                Map<String, Object> schema,
                String scope,
                Function<ToolCallParam, Mono<ToolResultBlock>> action) {
            var guarded =
                    new ToolBase(
                            ToolBase.builder()
                                    .name(name)
                                    .description(description)
                                    .inputSchema(schema)) {
                        @Override
                        public Mono<ToolResultBlock> callAsync(ToolCallParam param) {
                            return Mono.defer(
                                            () -> {
                                                healthy();
                                                if (ToolValidator.validateInput(
                                                                PayloadJson.canonical(
                                                                        param.getInput()),
                                                                schema)
                                                        != null)
                                                    throw new Blocked("invalid_tool_input");
                                                if (name.equals("load_skill_through_path")) {
                                                    if (!skillIds.contains(
                                                            param.getInput().get("skillId")))
                                                        throw new Blocked("skill_denied");
                                                    try (var current =
                                                            PackagedSkills.selected(
                                                                    skillBindings)) {
                                                        // Each dispatch rechecks the selected
                                                        // frozen asset contract.
                                                    }
                                                }
                                                String id = param.getToolUseBlock().getId();
                                                String cached =
                                                        calls.reservePrivateRead(
                                                                invocation,
                                                                id,
                                                                name,
                                                                param.getInput());
                                                if (cached != null)
                                                    return Mono.just(
                                                            JsonUtils.getJsonCodec()
                                                                    .fromJson(
                                                                            cached,
                                                                            ToolResultBlock.class));
                                                return observations
                                                        .tool(
                                                                invocation.parent(),
                                                                () -> action.apply(param))
                                                        .map(
                                                                result -> {
                                                                    if (result.isSuspended()) {
                                                                        if (!interactions
                                                                                .containsKey(name))
                                                                            throw new Blocked(
                                                                                    "child_read_incomplete");
                                                                        return result;
                                                                    }
                                                                    var normalized =
                                                                            com.financeclaw
                                                                                    .infrastructure
                                                                                    .agentscope
                                                                                    .ResumeInputs
                                                                                    .result(
                                                                                            param
                                                                                                    .getToolUseBlock(),
                                                                                            result);
                                                                    var wrapped =
                                                                            (ToolResultBlock)
                                                                                    normalized
                                                                                            .getContent()
                                                                                            .getFirst();
                                                                    Object stored =
                                                                            calls.checkpoint(
                                                                                    invocation,
                                                                                    () -> {
                                                                                        var small =
                                                                                                offloader
                                                                                                        .offload(
                                                                                                                invocation
                                                                                                                        .parent(),
                                                                                                                wrapped,
                                                                                                                scope);
                                                                                        return calls
                                                                                                .completePrivateRead(
                                                                                                        invocation,
                                                                                                        id,
                                                                                                        name,
                                                                                                        param
                                                                                                                .getInput(),
                                                                                                        PayloadJson
                                                                                                                .parse(
                                                                                                                        JsonUtils
                                                                                                                                .getJsonCodec()
                                                                                                                                .toJson(
                                                                                                                                        small)));
                                                                                    });
                                                                    return JsonUtils.getJsonCodec()
                                                                            .fromJson(
                                                                                    PayloadJson
                                                                                            .canonical(
                                                                                                    stored),
                                                                                    ToolResultBlock
                                                                                            .class);
                                                                });
                                            })
                                    .onErrorMap(
                                            error ->
                                                    error instanceof Blocked
                                                                    || error instanceof Fenced
                                                            ? error
                                                            : new Blocked("private_agent_failed"))
                                    .doOnError(
                                            error -> {
                                                if (error instanceof Blocked
                                                        || error instanceof Fenced)
                                                    fatal.compareAndSet(
                                                            null, (RuntimeException) error);
                                            })
                                    .subscribeOn(Schedulers.boundedElastic());
                        }
                    };
            destination.registerAgentTool(guarded);
            return guarded;
        }

        public PermissionContextState permissions() {
            var builder = PermissionContextState.builder().mode(PermissionMode.DEFAULT);
            var names = new TreeSet<>(toolkit.getToolNames());
            if (skills != null) names.add("load_skill_through_path");
            for (String name : names) {
                if (interactions.containsKey(name) && interactions.get(name).approval())
                    builder.addAskRule(
                            name,
                            new PermissionRule(
                                    name, null, PermissionBehavior.ASK, "frozen-controlled-leaf"));
                else
                    builder.addAllowRule(
                            name,
                            new PermissionRule(
                                    name,
                                    null,
                                    PermissionBehavior.ALLOW,
                                    "frozen-controlled-leaf"));
            }
            return builder.build();
        }

        public List<Pending> pending(io.agentscope.core.state.AgentState state) {
            return new com.financeclaw.infrastructure.agentscope.AgentStateInspector(state)
                    .pending().stream()
                            .filter(call -> interactions.containsKey(call.getName()))
                            .map(
                                    call -> {
                                        var handler = interactions.get(call.getName());
                                        if (handler == null)
                                            throw new Blocked("unsupported_interaction");
                                        return handler.pending(call);
                                    })
                            .toList();
        }

        public boolean allows(String name) {
            return selected.containsKey(name)
                    || skills != null && name.equals("load_skill_through_path");
        }

        public boolean allowsSchemas(List<ToolSchema> schemas) {
            if (schemas.size() != runtimeToolkit.getToolNames().size()) return false;
            var names = new HashSet<String>();
            for (var schema : schemas) {
                if (!allows(schema.getName()) || !names.add(schema.getName())) return false;
                var tool = runtimeToolkit.getTool(schema.getName());
                if (tool == null
                        || !PayloadJson.canonical(tool.getParameters())
                                .equals(PayloadJson.canonical(schema.getParameters())))
                    return false;
            }
            return true;
        }

        private void healthy() {
            if (fatal.get() != null) throw fatal.get();
        }

        @Override
        public void close() {
            try {
                mcp.close();
            } finally {
                if (skills != null) skills.close();
            }
        }
    }
}
