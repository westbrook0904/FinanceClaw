package com.financeclaw.infrastructure.agentscope.capability;

import com.financeclaw.app.release.port.Credentials;
import com.financeclaw.domain.Governance.Blocked;
import com.financeclaw.domain.Task.Owner;
import com.financeclaw.domain.agent.AgentComposition;
import com.financeclaw.domain.agent.CapabilityBinding;
import com.financeclaw.domain.release.ReleaseDefinition;
import com.financeclaw.domain.release.RemoteEndpoint;
import com.financeclaw.tools.McpContracts;
import com.financeclaw.tools.McpTools;
import io.agentscope.core.tool.Toolkit;
import java.nio.charset.StandardCharsets;
import java.util.*;
import org.springframework.transaction.support.TransactionSynchronizationManager;

/** 发布准备中的只读 MCP 发现；只访问显式选中且在基础发布允许范围内的服务，不调用工具。 */
public final class McpCapabilityDiscovery {
    private final Credentials credentials;
    private final McpTools.DiscoveryLimits limits;

    public McpCapabilityDiscovery(Credentials credentials) {
        this(credentials, McpTools.DiscoveryLimits.defaults());
    }

    public McpCapabilityDiscovery(Credentials credentials, McpTools.DiscoveryLimits limits) {
        this.credentials = Objects.requireNonNull(credentials);
        this.limits = Objects.requireNonNull(limits);
    }

    public List<CapabilityBinding> discover(
            Owner owner, ReleaseDefinition base, List<String> selectedIds) {
        if (TransactionSynchronizationManager.isActualTransactionActive()) {
            throw new Blocked("mcp_discovery_in_transaction");
        }
        if (owner == null || base == null || selectedIds == null) {
            throw new Blocked("mcp_contract_invalid");
        }
        var selected = new TreeMap<String, List<String>>();
        var unique = new HashSet<String>();
        for (String id : selectedIds) {
            if (!AgentComposition.validId(id)) {
                throw new Blocked("mcp_contract_invalid");
            }
            if (!id.startsWith("mcp:")) {
                continue;
            }
            if (!unique.add(id)) {
                throw new Blocked("mcp_contract_invalid");
            }
            int separator = id.indexOf('/');
            selected.computeIfAbsent(id.substring(4, separator), ignored -> new ArrayList<>())
                    .add(id.substring(separator + 1));
        }
        var services = new HashMap<String, RemoteEndpoint>();
        for (var remote : base.business().remotes()) {
            if (!selected.containsKey(remote.service())) {
                continue;
            }
            if (services.putIfAbsent(remote.service(), remote) != null) {
                throw new Blocked("mcp_contract_invalid");
            }
        }
        for (var entry : selected.entrySet()) {
            var service = services.get(entry.getKey());
            if (service == null
                    || !service.tools().containsAll(entry.getValue())
                    || !base.scopes().contains(McpContracts.scope(entry.getKey()))) {
                throw new Blocked("mcp_tool_unavailable");
            }
        }
        var bindings = new ArrayList<CapabilityBinding>();
        var names = new HashSet<String>();
        long deadline = limits.deadline();
        int totalBytes = 0;
        try (var transport = new McpTools(limits)) {
            for (var entry : selected.entrySet()) {
                var temporary = new Toolkit();
                var remote = services.get(entry.getKey());
                transport.registerSelected(
                        temporary,
                        remote,
                        owner.tenant(),
                        credentials::resolve,
                        entry.getValue(),
                        deadline);
                for (String name : entry.getValue()) {
                    if (!names.add(name)) {
                        throw new Blocked("mcp_tool_name_collision");
                    }
                    var binding = McpContracts.binding(entry.getKey(), temporary.getTool(name));
                    totalBytes += binding.contractJson().getBytes(StandardCharsets.UTF_8).length;
                    if (totalBytes > limits.maxTotalBytes()) {
                        throw new Blocked("mcp_discovery_limit_exceeded");
                    }
                    bindings.add(binding);
                }
            }
        }
        return bindings.stream().sorted(Comparator.comparing(CapabilityBinding::id)).toList();
    }
}
