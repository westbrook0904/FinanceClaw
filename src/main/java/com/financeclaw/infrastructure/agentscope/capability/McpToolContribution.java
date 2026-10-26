package com.financeclaw.infrastructure.agentscope.capability;

import com.financeclaw.app.agent.port.CredentialAccessGateway;
import com.financeclaw.app.artifact.ArtifactService;
import com.financeclaw.app.execution.ExecutionPolicyService;
import com.financeclaw.app.release.port.Credentials;
import com.financeclaw.domain.Governance.*;
import com.financeclaw.domain.Task.Claim;
import com.financeclaw.domain.Task.Owner;
import com.financeclaw.domain.agent.AgentExecutionPlan;
import com.financeclaw.domain.agent.CapabilityBinding;
import com.financeclaw.domain.release.ReleaseDefinition;
import com.financeclaw.domain.release.RemoteEndpoint;
import com.financeclaw.support.PayloadJson;
import com.financeclaw.tools.*;
import io.agentscope.core.message.*;
import io.agentscope.core.tool.AgentTool;
import io.agentscope.core.tool.Toolkit;
import java.util.*;
import java.util.function.Function;
import java.util.function.Supplier;
import reactor.core.publisher.Mono;

/**
 * Keep native MCP transport and schemas; add only dispatch policy and governed artifact storage.
 */
public final class McpToolContribution implements ToolContribution, AutoCloseable {
    private final ExecutionPolicyService policy;
    private final Credentials credentials;
    private final ArtifactService artifacts;
    private final Claim claim;
    private final ReleaseDefinition release;
    private final List<String> scopes;
    private final Toolkit toolkit;
    private final AgentExecutionPlan plan;
    private final CredentialAccessGateway credentialAccess;
    private final McpTools mcp = new McpTools();
    private Runnable executionBoundary = () -> {};
    private Function<Supplier<ToolResultBlock>, ToolResultBlock> resultBoundary = Supplier::get;

    /**
     * Child authorization must be checked again after remote discovery and when persisting its
     * result.
     */
    public void boundaries(
            Runnable executionBoundary,
            Function<Supplier<ToolResultBlock>, ToolResultBlock> resultBoundary) {
        this.executionBoundary = Objects.requireNonNull(executionBoundary);
        this.resultBoundary = Objects.requireNonNull(resultBoundary);
    }

    public McpToolContribution(
            ExecutionPolicyService policy,
            Credentials credentials,
            ArtifactService artifacts,
            Claim claim,
            ReleaseDefinition release,
            List<String> scopes,
            Toolkit toolkit) {
        this(policy, credentials, artifacts, claim, release, scopes, toolkit, null);
    }

    public McpToolContribution(
            ExecutionPolicyService policy,
            Credentials credentials,
            ArtifactService artifacts,
            Claim claim,
            ReleaseDefinition release,
            List<String> scopes,
            Toolkit toolkit,
            AgentExecutionPlan plan) {
        this(
                policy,
                credentials,
                artifacts,
                claim,
                release,
                scopes,
                toolkit,
                plan,
                CredentialAccessGateway.trustedFixture());
    }

    public McpToolContribution(
            ExecutionPolicyService policy,
            Credentials credentials,
            ArtifactService artifacts,
            Claim claim,
            ReleaseDefinition release,
            List<String> scopes,
            Toolkit toolkit,
            AgentExecutionPlan plan,
            CredentialAccessGateway credentialAccess) {
        this.credentialAccess = credentialAccess;
        this.plan = plan;
        this.policy = policy;
        this.credentials = credentials;
        this.artifacts = artifacts;
        this.claim = claim;
        this.release = release;
        this.scopes = List.copyOf(scopes);
        this.toolkit = toolkit;
    }

    @Override
    public void contribute(Registrar registrar) {
        if (plan != null) {
            contributeFrozen(registrar);
            return;
        }
        for (var service : release.business().remotes()) {
            String scope = McpContracts.scope(service.service());
            if (!scopes.contains(scope)) {
                continue;
            }
            policy.checkDispatch(claim);
            authorize(service);
            mcp.register(toolkit, service, claim.turn().tenant(), credentials::resolve);
            for (String name : service.tools()) {
                register(registrar, service, toolkit.getTool(name), null, List.of());
            }
        }
    }

    private void contributeFrozen(Registrar registrar) {
        var selected = new TreeMap<String, List<CapabilityBinding>>();
        var ids = new HashSet<String>();
        var names = new HashSet<String>();
        for (var binding : plan.capabilities()) {
            if (!"mcp".equals(binding.kind())) {
                continue;
            }
            int slash = binding.id().indexOf('/');
            if (!binding.id().startsWith("mcp:")
                    || slash < 5
                    || !binding.id().substring(slash + 1).equals(binding.name())
                    || !ids.add(binding.id())
                    || !names.add(binding.name())) {
                throw new Blocked("mcp_contract_changed");
            }
            selected.computeIfAbsent(binding.id().substring(4, slash), ignored -> new ArrayList<>())
                    .add(binding);
        }
        if (!Collections.disjoint(toolkit.getToolNames(), names)) {
            throw new Blocked("mcp_tool_name_collision");
        }
        var services = new HashMap<String, RemoteEndpoint>();
        for (var service : release.business().remotes()) {
            if (selected.containsKey(service.service())
                    && services.putIfAbsent(service.service(), service) != null) {
                throw new Blocked("mcp_contract_changed");
            }
        }
        for (var entry : selected.entrySet()) {
            var service = services.get(entry.getKey());
            if (service == null
                    || !service.tools()
                            .containsAll(
                                    entry.getValue().stream()
                                            .map(CapabilityBinding::name)
                                            .toList())) {
                throw new Blocked("mcp_contract_changed");
            }
        }
        long deadline = McpTools.DiscoveryLimits.defaults().deadline();
        for (var entry : selected.entrySet()) {
            var service = services.get(entry.getKey());
            String scope = McpContracts.scope(service.service());
            if (!scopes.contains(scope)) {
                continue;
            }
            policy.checkDispatch(claim);
            authorize(service);
            var temporary = new Toolkit();
            McpTools.SelectedSession session;
            try {
                session =
                        mcp.registerSelected(
                                temporary,
                                service,
                                claim.turn().tenant(),
                                credentials::resolve,
                                entry.getValue().stream().map(CapabilityBinding::name).toList(),
                                deadline);
            } catch (Blocked failure) {
                if (Set.of("mcp_contract_invalid", "mcp_tool_unavailable")
                        .contains(failure.getMessage())) {
                    throw new Blocked("mcp_contract_changed");
                }
                throw failure;
            }
            // Compare the actual native objects before exposing any selected tool to the model.
            for (var binding : entry.getValue()) {
                McpContracts.requireMatch(
                        binding, service.service(), temporary.getTool(binding.name()));
            }
            for (var binding : entry.getValue()) {
                register(
                        registrar,
                        service,
                        temporary.getTool(binding.name()),
                        session,
                        entry.getValue());
            }
        }
    }

    private void register(
            Registrar registrar,
            RemoteEndpoint service,
            AgentTool nativeTool,
            McpTools.SelectedSession session,
            List<CapabilityBinding> frozen) {
        String name = nativeTool.getName();
        String scope = McpContracts.scope(service.service());
        registrar.tool(
                name,
                nativeTool.getDescription(),
                nativeTool.getParameters(),
                scope,
                p -> {
                    executionBoundary.run();
                    authorize(service);
                    Mono<Void> ready = session == null ? Mono.empty() : session.verify(frozen);
                    return ready.then(
                            Mono.defer(
                                    () -> {
                                        // This must run after fresh discovery, immediately before
                                        // native dispatch.
                                        if (session != null) {
                                            policy.checkDispatch(claim);
                                        }
                                        executionBoundary.run();
                                        authorize(service);
                                        if (service.service().equals("taibu")) {
                                            TaibuInputs.validate(name, p.getInput());
                                        }
                                        return nativeTool
                                                .callAsync(p)
                                                .map(
                                                        result -> {
                                                            if (result.getState()
                                                                    == ToolResultState.ERROR) {
                                                                throw new Blocked(
                                                                        "mcp_service_error");
                                                            }
                                                            String text =
                                                                    result.getOutput().stream()
                                                                            .filter(
                                                                                    TextBlock.class
                                                                                            ::isInstance)
                                                                            .map(
                                                                                    TextBlock.class
                                                                                            ::cast)
                                                                            .map(TextBlock::getText)
                                                                            .collect(
                                                                                    java.util.stream
                                                                                            .Collectors
                                                                                            .joining(
                                                                                                    "\n"));
                                                            Object data;
                                                            try {
                                                                data = PayloadJson.parse(text);
                                                            } catch (Blocked e) {
                                                                throw new Blocked(
                                                                        "mcp_invalid_result");
                                                            }
                                                            if (data instanceof Map<?, ?> map
                                                                    && (Boolean.FALSE.equals(
                                                                                    map.get(
                                                                                            "success"))
                                                                            || map.containsKey(
                                                                                    "error"))) {
                                                                throw new Blocked(
                                                                        "mcp_service_error");
                                                            }
                                                            if (service.service().equals("taibu")) {
                                                                TaibuInputs.validateResult(
                                                                        name, data, p.getInput());
                                                            }
                                                            Object validated = data;
                                                            return resultBoundary.apply(
                                                                    () ->
                                                                            archived(
                                                                                    validated,
                                                                                    scope));
                                                        });
                                    }));
                },
                null);
    }

    private void authorize(RemoteEndpoint service) {
        credentialAccess.requireCredential(
                new Owner(claim.turn().tenant(), claim.turn().subject()), service.credentialRef());
    }

    private ToolResultBlock archived(Object result, String scope) {
        // MCP 回执属于工具输出，沿用原工具权限并绑定遗忘状态，不额外要求 artifact:read。
        var artifact = artifacts.archiveToolResult(claim, result, scope);
        return ToolResultBlock.text(
                PayloadJson.canonical(
                        Map.of(
                                "artifact_id",
                                artifact.id(),
                                "hash",
                                artifact.hash(),
                                "bytes",
                                artifact.bytes(),
                                "download_url",
                                "/v1/artifacts/" + artifact.id() + "/download",
                                "read_range",
                                Map.of(
                                        "tool",
                                        "read_artifact_range",
                                        "artifact_id",
                                        artifact.id(),
                                        "start",
                                        0,
                                        "max_bytes",
                                        512,
                                        "instructions",
                                        "start 是 UTF-8 字节偏移；接续使用 nextStart，text 拼接后为完整 JSON。"))));
    }

    @Override
    public void close() {
        mcp.close();
    }
}
