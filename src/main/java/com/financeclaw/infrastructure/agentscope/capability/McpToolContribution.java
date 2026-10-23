package com.financeclaw.infrastructure.agentscope.capability;

import com.financeclaw.app.artifact.ArtifactService;
import com.financeclaw.app.execution.ExecutionPolicyService;
import com.financeclaw.app.release.port.Credentials;
import com.financeclaw.domain.Governance.*;
import com.financeclaw.domain.Task.Claim;
import com.financeclaw.domain.release.ReleaseDefinition;
import com.financeclaw.support.PayloadJson;
import com.financeclaw.tools.*;
import io.agentscope.core.message.*;
import io.agentscope.core.tool.Toolkit;
import java.util.*;

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
    private final McpTools mcp = new McpTools();

    public McpToolContribution(
            ExecutionPolicyService policy,
            Credentials credentials,
            ArtifactService artifacts,
            Claim claim,
            ReleaseDefinition release,
            List<String> scopes,
            Toolkit toolkit) {
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
        for (var service : release.business().remotes()) {
            String scope = service.service().equals("taibu") ? "taibu:read" : "travel:read";
            if (!scopes.contains(scope)) {
                continue;
            }
            policy.checkDispatch(claim);
            mcp.register(toolkit, service, claim.turn().tenant(), credentials::resolve);
            for (String name : service.tools()) {
                var nativeTool = toolkit.getTool(name);
                // Keep the native name/schema/call implementation. This adapter only enforces
                // product
                // policy and archives results before they enter model context.
                registrar.tool(
                        name,
                        nativeTool.getDescription(),
                        nativeTool.getParameters(),
                        scope,
                        p -> {
                            if (service.service().equals("taibu")) {
                                TaibuInputs.validate(name, p.getInput());
                            }
                            return nativeTool
                                    .callAsync(p)
                                    .map(
                                            result -> {
                                                if (result.getState() == ToolResultState.ERROR) {
                                                    throw new Blocked("mcp_service_error");
                                                }
                                                String text =
                                                        result.getOutput().stream()
                                                                .filter(TextBlock.class::isInstance)
                                                                .map(TextBlock.class::cast)
                                                                .map(TextBlock::getText)
                                                                .collect(
                                                                        java.util.stream.Collectors
                                                                                .joining("\n"));
                                                Object data;
                                                try {
                                                    data = PayloadJson.parse(text);
                                                } catch (Blocked e) {
                                                    throw new Blocked("mcp_invalid_result");
                                                }
                                                if (data instanceof Map<?, ?> map
                                                        && (Boolean.FALSE.equals(map.get("success"))
                                                                || map.containsKey("error"))) {
                                                    throw new Blocked("mcp_service_error");
                                                }
                                                if (service.service().equals("taibu")) {
                                                    TaibuInputs.validateResult(
                                                            name, data, p.getInput());
                                                }
                                                return archived(data, scope);
                                            });
                        },
                        null);
            }
        }
    }

    private ToolResultBlock archived(Object result, String scope) {
        var artifact = artifacts.archive(claim, result, scope);
        return ToolResultBlock.text(
                PayloadJson.canonical(
                        Map.of(
                                "artifact_id",
                                artifact.id(),
                                "hash",
                                artifact.hash(),
                                "bytes",
                                artifact.bytes())));
    }

    @Override
    public void close() {
        mcp.close();
    }
}
