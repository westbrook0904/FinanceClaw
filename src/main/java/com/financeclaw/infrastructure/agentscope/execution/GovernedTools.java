package com.financeclaw.infrastructure.agentscope.execution;

import com.financeclaw.app.agent.port.CredentialAccessGateway;
import com.financeclaw.app.artifact.ArtifactService;
import com.financeclaw.app.execution.ExecutionPolicyService;
import com.financeclaw.app.release.ReleaseService;
import com.financeclaw.app.release.port.Credentials;
import com.financeclaw.app.report.ReportService;
import com.financeclaw.domain.Governance.*;
import com.financeclaw.domain.Task.*;
import com.financeclaw.domain.agent.*;
import com.financeclaw.domain.release.ReleaseDefinition;
import com.financeclaw.infrastructure.agentscope.AgentStateInspector;
import com.financeclaw.infrastructure.agentscope.ResumeInputs;
import com.financeclaw.infrastructure.agentscope.capability.*;
import com.financeclaw.infrastructure.agentscope.skills.PackagedSkills;
import com.financeclaw.support.PayloadJson;
import com.financeclaw.support.ScopeRequirements;
import com.financeclaw.support.ToolSchemas;
import com.financeclaw.tools.*;
import io.agentscope.core.message.*;
import io.agentscope.core.permission.*;
import io.agentscope.core.tool.*;
import java.util.*;
import java.util.function.Function;
import reactor.core.publisher.Mono;
import reactor.core.scheduler.Schedulers;

/** A ToolBase boundary, shared by local tools and SDK MCP transport. No model-supplied identity. */
public final class GovernedTools implements AutoCloseable {
    public static final Map<String, Object> CITY = LocalToolContribution.CITY;
    public static final Map<String, Object> TITLE = LocalToolContribution.TITLE;
    public static final Map<String, Object> EMPTY = LocalToolContribution.EMPTY;
    public static final Set<String> NAMES = LocalToolContribution.NAMES;
    private com.financeclaw.infrastructure.runtime.RuntimeObservations observations =
            com.financeclaw.infrastructure.runtime.RuntimeObservations.disabled();

    public void configureObservations(
            com.financeclaw.infrastructure.runtime.RuntimeObservations observations) {
        this.observations = observations;
    }

    private Runnable executionCheck = () -> {};

    public void configureExecutionCheck(Runnable executionCheck) {
        this.executionCheck = Objects.requireNonNull(executionCheck);
    }

    private final ExecutionPolicyService policy;
    private final ReleaseService releases;
    private final Credentials credentials;
    private final ArtifactService artifacts;
    private final ReportService reports;
    private final Claim claim;
    private final ReleaseDefinition release;
    private final List<String> scopes;
    private final AgentExecutionPlan plan;
    private final CredentialAccessGateway credentialAccess;
    private final Map<String, CapabilityBinding> selectedTools = new HashMap<>();
    private Set<String> selectedSkillIds = Set.of();
    private final java.util.concurrent.atomic.AtomicReference<Blocked> compatibilityFailure =
            new java.util.concurrent.atomic.AtomicReference<>();
    private final McpToolContribution mcp;
    private io.agentscope.core.skill.repository.AgentSkillRepository skills;
    private PrivateAgentDelegation privateDelegation;
    private final Map<String, InteractionHandler> interactions = new HashMap<>();
    private final Map<String, String> requiredScopes = new HashMap<>();
    public final Toolkit toolkit = new Toolkit();

    public GovernedTools(
            ExecutionPolicyService policy,
            ReleaseService releases,
            Credentials credentials,
            ArtifactService artifacts,
            ReportService reports,
            Claim claim) {
        this(
                policy,
                releases,
                credentials,
                artifacts,
                reports,
                claim,
                null,
                null,
                CredentialAccessGateway.trustedFixture());
    }

    public GovernedTools(
            ExecutionPolicyService policy,
            ReleaseService releases,
            Credentials credentials,
            ArtifactService artifacts,
            ReportService reports,
            Claim claim,
            AgentExecutionPlan plan) {
        this(
                policy,
                releases,
                credentials,
                artifacts,
                reports,
                claim,
                null,
                plan,
                CredentialAccessGateway.trustedFixture());
    }

    public GovernedTools(
            ExecutionPolicyService policy,
            ReleaseService releases,
            Credentials credentials,
            ArtifactService artifacts,
            ReportService reports,
            Claim claim,
            AgentExecutionPlan plan,
            CredentialAccessGateway credentialAccess) {
        this(
                policy,
                releases,
                credentials,
                artifacts,
                reports,
                claim,
                null,
                plan,
                credentialAccess);
    }

    private GovernedTools(
            ExecutionPolicyService policy,
            ReleaseService releases,
            Credentials credentials,
            ArtifactService artifacts,
            ReportService reports,
            Claim claim,
            List<String> allowed,
            AgentExecutionPlan plan,
            CredentialAccessGateway credentialAccess) {
        this.credentialAccess = credentialAccess;
        this.policy = policy;
        this.releases = releases;
        this.credentials = credentials;
        this.artifacts = artifacts;
        this.reports = reports;
        this.claim = claim;
        this.plan = plan;
        LocalCapabilityCatalog.requireCompatible(plan);
        release = plan == null ? releases.require(claim.turn().releaseHash()) : plan.definition();
        if (plan != null) {
            plan.capabilities().stream()
                    .filter(binding -> !binding.kind().equals("skill"))
                    .forEach(binding -> selectedTools.put(binding.name(), binding));
        }
        var root = policy.scopes(claim);
        if (allowed == null) {
            allowed = root;
        }
        if (!root.containsAll(allowed)) {
            throw new Blocked("scope_expansion_denied");
        }
        scopes = List.copyOf(allowed.stream().filter(release.scopes()::contains).toList());
        new LocalToolContribution(artifacts, reports, claim, plan == null ? null : scopes)
                .contribute(this::register);
        mcp =
                new McpToolContribution(
                        policy,
                        credentials,
                        artifacts,
                        claim,
                        release,
                        scopes,
                        toolkit,
                        plan,
                        credentialAccess);
        try {
            mcp.contribute(
                    (name, description, schema, scope, action, interaction) ->
                            register("mcp", name, description, schema, scope, action, interaction));
            if (scopes.contains("skill:use")) {
                if (plan == null) {
                    if (!release.business().skillHash().equals(PackagedSkills.hash())) {
                        throw new Blocked("skill_release_changed");
                    }
                    skills =
                            new io.agentscope.core.skill.repository.ClasspathSkillRepository(
                                    PackagedSkills.SKILLS);
                } else if (plan.capabilities().stream()
                        .anyMatch(binding -> binding.kind().equals("skill"))) {
                    skills = PackagedSkills.selected(plan.capabilities());
                    selectedSkillIds =
                            Set.copyOf(
                                    skills.getAllSkills().stream()
                                            .map(io.agentscope.core.skill.AgentSkill::getSkillId)
                                            .toList());
                }
            }
        } catch (java.io.IOException | RuntimeException e) {
            close();
            throw e instanceof Blocked blocked ? blocked : new Blocked("capability_unavailable");
        }
    }

    public void configure(io.agentscope.core.ReActAgent.Builder builder) {
        if (skills == null) {
            return;
        }
        builder.skillRepository(skills).skillCodeExecutionEnabled(false);
        // The SDK's skill access tool is an AgentTool in 2.0.3; enforce business grants through
        // native middleware without replacing its loader, tools, prompt or activation state.
        builder.middleware(
                new io.agentscope.core.middleware.MiddlewareBase() {
                    @Override
                    public reactor.core.publisher.Flux<io.agentscope.core.event.AgentEvent>
                            onActing(
                                    io.agentscope.core.agent.Agent agent,
                                    io.agentscope.core.agent.RuntimeContext context,
                                    io.agentscope.core.middleware.ActingInput input,
                                    java.util.function.Function<
                                                    io.agentscope.core.middleware.ActingInput,
                                                    reactor.core.publisher.Flux<
                                                            io.agentscope.core.event.AgentEvent>>
                                            next) {
                        return reactor.core.publisher.Flux.defer(
                                () -> {
                                    executionCheck.run();
                                    for (var call : input.toolCalls()) {
                                        if (call.getName().equals("load_skill_through_path")) {
                                            if (plan != null
                                                    && !selectedSkillIds.contains(
                                                            call.getInput().get("skillId"))) {
                                                throw new Blocked("skill_denied");
                                            }
                                            policy.consumeTool(claim, "skill:use");
                                        }
                                    }
                                    executionCheck.run();
                                    return next.apply(input);
                                });
                    }
                });
    }

    public void business(io.agentscope.core.model.Model model, BusinessCapabilities capabilities) {
        privateDelegation = capabilities.privateDelegation();
        if (release.business().ziwei() != null && scopes.contains("ziwei:read")) {
            capabilities.ziwei(claim, this::register);
        }
        capabilities.contribute(claim, model, scopes.contains("workflow:run"), this::register);
        capabilities.privateAgents(
                claim,
                plan,
                (name, description, schema, scope, action, interaction) ->
                        register("agent", name, description, schema, scope, action, interaction));
        if (plan != null
                && selectedTools.values().stream()
                        .filter(binding -> scopes.containsAll(binding.scopes()))
                        .anyMatch(binding -> !toolkit.getToolNames().contains(binding.name()))) {
            throw new Blocked("capability_unavailable");
        }
    }

    public Msg resumeInput(
            io.agentscope.core.state.AgentStateStore store,
            com.financeclaw.domain.ExecutionScope scope,
            Interaction interaction,
            Object answer) {
        var call =
                ResumeInputs.pending(
                        store, scope, interaction.stateVersion(), interaction.callId());
        if (!ResumeInputs.actionHash(call).equals(interaction.actionHash())
                || !call.getName().equals(interaction.tool())
                || call.getState() == ToolCallState.ASKING) {
            throw new Blocked("checkpoint_mismatch");
        }
        var handler = interactions.get(interaction.tool());
        if (handler == null || handler.approval()) {
            throw new Blocked("unsupported_interaction");
        }
        return handler.resume(call, answer);
    }

    /**
     * Future child/workflow entry points inherit the same Turn counters and may only narrow scopes.
     */
    public GovernedTools narrow(List<String> requested) {
        if (!scopes.containsAll(requested)) {
            throw new Blocked("scope_expansion_denied");
        }
        var narrowed =
                new GovernedTools(
                        policy,
                        releases,
                        credentials,
                        artifacts,
                        reports,
                        claim,
                        List.copyOf(requested),
                        plan,
                        credentialAccess);
        narrowed.configureObservations(observations);
        narrowed.configureExecutionCheck(executionCheck);
        return narrowed;
    }

    public static Map<String, Object> schema(String key, Map<String, Object> property) {
        return ToolSchemas.required(key, property);
    }

    public PermissionContextState permissions() {
        var builder = PermissionContextState.builder().mode(PermissionMode.DEFAULT);
        for (String name : toolkit.getToolNames()) {
            var rule =
                    new PermissionRule(
                            name,
                            null,
                            (interactions.containsKey(name) && interactions.get(name).approval())
                                    ? PermissionBehavior.ASK
                                    : PermissionBehavior.ALLOW,
                            "release-policy");
            if (interactions.containsKey(name) && interactions.get(name).approval()) {
                builder.addAskRule(name, rule);
            } else {
                builder.addAllowRule(name, rule);
            }
        }
        return builder.build();
    }

    private void register(
            String name,
            String description,
            Map<String, Object> schema,
            String scope,
            Function<ToolCallParam, Mono<ToolResultBlock>> action,
            InteractionHandler interaction) {
        register("function", name, description, schema, scope, action, interaction);
    }

    private void register(
            String kind,
            String name,
            String description,
            Map<String, Object> schema,
            String scope,
            Function<ToolCallParam, Mono<ToolResultBlock>> action,
            InteractionHandler interaction) {
        if (plan != null) {
            var binding = selectedTools.get(name);
            if (binding == null
                    || !binding.kind().equals(kind)
                    || !scopes.containsAll(binding.scopes())) return;
            if (binding.kind().equals("function") || binding.kind().equals("agent")) {
                var frozen = PayloadJson.object(binding.contractJson());
                if (!Objects.equals(frozen.get("description"), description)
                        || !PayloadJson.canonical(frozen.get("inputSchema"))
                                .equals(PayloadJson.canonical(schema))
                        || !Objects.equals(
                                frozen.get("approval"),
                                interaction != null && interaction.approval())) {
                    throw new Blocked("capability_release_changed");
                }
            }
        }
        if (scope != null
                && (!ScopeRequirements.allowed(scopes, scope)
                        || !ScopeRequirements.allowed(release.scopes(), scope))) {
            return;
        }
        if (scope != null) requiredScopes.put(name, scope);
        toolkit.registerAgentTool(
                new ToolBase(
                        ToolBase.builder()
                                .name(name)
                                .description(description)
                                .inputSchema(schema)) {
                    @Override
                    public Mono<ToolResultBlock> callAsync(ToolCallParam param) {
                        return Mono.defer(
                                        () -> {
                                            executionCheck.run();
                                            // SDK 2.0.3 validates raw content before merging
                                            // input/presets. This is
                                            // the effective-input boundary, also used by direct
                                            // adapter calls.
                                            if (ToolValidator.validateInput(
                                                            PayloadJson.canonical(param.getInput()),
                                                            schema)
                                                    != null) {
                                                throw new Blocked("invalid_tool_input");
                                            }
                                            // 固定本地行情工具由 MarketDataService 在读取前核验授权并扣额。
                                            // 子 Agent 和 Workflow 直接调用该服务，因此服务必须保留扣额；
                                            // 主 Agent 不能再扣第二次。同名 MCP 仍走统一扣额边界。
                                            if (!kind.equals("function")
                                                    || !name.equals("market_snapshot")) {
                                                policy.consumeTool(claim, scope);
                                            }
                                            checkCompatibility();
                                            return observations.tool(
                                                    claim, () -> action.apply(param));
                                        })
                                .doOnError(
                                        error -> {
                                            // SDK may translate exceptions into ordinary tool
                                            // results. A
                                            // frozen MCP contract failure must still block the
                                            // attempt.
                                            if (plan != null
                                                    && error instanceof Blocked blocked
                                                    && (kind.equals("agent")
                                                            || blocked.getMessage()
                                                                    .startsWith("mcp_"))) {
                                                compatibilityFailure.compareAndSet(null, blocked);
                                            }
                                        })
                                .onErrorMap(
                                        error ->
                                                error instanceof Blocked || error instanceof Fenced
                                                        ? error
                                                        : new Blocked("tool_execution_failed"))
                                .subscribeOn(Schedulers.boundedElastic());
                    }
                });
        if (interaction != null) {
            interactions.put(name, interaction);
        }
    }

    public void checkCompatibility() {
        executionCheck.run();
        var failure = compatibilityFailure.get();
        if (failure != null) throw failure;
    }

    /** 大结果归档沿用产生它的工具权限，原生 Skill loader 不经过本地注册函数。 */
    public String requiredScope(String toolName) {
        return "load_skill_through_path".equals(toolName)
                ? "skill:use"
                : requiredScopes.get(toolName);
    }

    public List<Pending> pending(io.agentscope.core.state.AgentState state) {
        var result =
                new AgentStateInspector(state)
                        .pending().stream()
                                .flatMap(
                                        call -> {
                                            var handler = interactions.get(call.getName());
                                            if (handler == null
                                                    && call.getState() != ToolCallState.ASKING)
                                                return java.util.stream.Stream.<Pending>empty();
                                            if (handler == null
                                                    || (call.getState() == ToolCallState.ASKING
                                                            && !handler.approval())) {
                                                throw new Blocked("unsupported_interaction");
                                            }
                                            return handler.pendingAll(call).stream();
                                        })
                                .toList();
        if (result.isEmpty()) throw new Blocked("unsupported_interaction");
        return result;
    }

    public boolean delegated(Interaction interaction) {
        return privateDelegation != null && privateDelegation.delegated(interaction);
    }

    public void prepareDelegatedResume(Claim source, List<Interaction> answers) {
        if (privateDelegation != null)
            privateDelegation.prepareResume(claim, source, plan, answers);
    }

    @Override
    public void close() {
        mcp.close();
        if (skills != null) {
            skills.close();
        }
    }
}
