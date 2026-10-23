package com.financeclaw.tools;

import com.financeclaw.domain.Governance.Blocked;
import com.financeclaw.domain.agent.CapabilityBinding;
import com.financeclaw.domain.release.RemoteEndpoint;
import com.financeclaw.infrastructure.release.EnvironmentCredentials;
import io.agentscope.core.tool.AgentTool;
import io.agentscope.core.tool.Toolkit;
import io.agentscope.core.tool.mcp.*;
import io.modelcontextprotocol.spec.McpSchema;
import java.net.URI;
import java.net.http.HttpClient;
import java.nio.charset.StandardCharsets;
import java.time.Duration;
import java.util.*;
import java.util.function.Function;
import org.springframework.transaction.support.TransactionSynchronizationManager;
import reactor.core.publisher.Mono;

/**
 * MCP 客户端的请求内注册与释放边界。
 *
 * <p>连接前校验租户和端点白名单。旧发布保留实时注册，新发布用有界发现、临时 Toolkit 和冻结契约比较。 生命周期结束统一关闭客户端，避免跨任务共享可变工具上下文。
 */
public final class McpTools implements AutoCloseable {
    private final List<McpClientWrapper> clients = new ArrayList<>();
    private final DiscoveryLimits limits;

    public McpTools() {
        this(DiscoveryLimits.defaults());
    }

    public McpTools(DiscoveryLimits limits) {
        this.limits = Objects.requireNonNull(limits);
    }

    public record DiscoveryLimits(
            int maxTools, int maxContractBytes, int maxTotalBytes, Duration timeout) {
        public DiscoveryLimits {
            if (maxTools < 1
                    || maxTools > 4096
                    || maxContractBytes < 1
                    || maxContractBytes > CapabilityBinding.MAX_CONTRACT_BYTES
                    || maxTotalBytes < maxContractBytes
                    || maxTotalBytes > 16 * 1024 * 1024
                    || timeout == null
                    || timeout.isNegative()
                    || timeout.isZero()
                    || timeout.compareTo(Duration.ofSeconds(60)) > 0) {
                throw new IllegalArgumentException("invalid_mcp_discovery_limits");
            }
        }

        public static DiscoveryLimits defaults() {
            return new DiscoveryLimits(
                    256, CapabilityBinding.MAX_CONTRACT_BYTES, 1024 * 1024, Duration.ofSeconds(15));
        }

        public long deadline() {
            return System.nanoTime() + timeout.toNanos();
        }
    }

    public void register(Toolkit toolkit, RemoteEndpoint remote, String tenant) {
        register(toolkit, remote, tenant, EnvironmentCredentials::lookup);
    }

    public void register(
            Toolkit toolkit,
            RemoteEndpoint remote,
            String tenant,
            Function<String, String> credentials) {
        if (!Collections.disjoint(toolkit.getToolNames(), remote.tools())) {
            throw new Blocked("mcp_tool_name_collision");
        }
        var client = connect(remote, tenant, credentials);
        try {
            toolkit.registration().mcpClient(client).enableTools(remote.tools()).apply();
            if (!toolkit.getToolNames().containsAll(remote.tools())) {
                throw new Blocked("mcp_tool_unavailable");
            }
        } catch (RuntimeException e) {
            close();
            throw e instanceof Blocked blocked ? blocked : new Blocked("mcp_unavailable");
        }
    }

    /** 发现和注册同一份 SDK 工具对象，不让注册过程再次读取未经比较的实时 Schema。 */
    public SelectedSession registerSelected(
            Toolkit temporary,
            RemoteEndpoint remote,
            String tenant,
            Function<String, String> credentials,
            List<String> selected,
            long deadline) {
        if (TransactionSynchronizationManager.isActualTransactionActive()) {
            throw new Blocked("mcp_discovery_in_transaction");
        }
        if (selected == null
                || selected.isEmpty()
                || new HashSet<>(selected).size() != selected.size()
                || !remote.tools().containsAll(selected)) {
            throw new Blocked("mcp_tool_unavailable");
        }
        if (!Collections.disjoint(temporary.getToolNames(), selected)) {
            throw new Blocked("mcp_tool_name_collision");
        }
        remaining(deadline);
        var client = connect(remote, tenant, credentials, deadline);
        var session = new SelectedSession(client, remote.service(), selected, limits);
        try {
            client.initialize().block(remaining(deadline));
            var discovered = session.discover(deadline).block(remaining(deadline));
            if (discovered == null || !discovered.keySet().containsAll(selected)) {
                throw new Blocked("mcp_tool_unavailable");
            }
            for (String name : selected) {
                temporary.registerAgentTool(discovered.get(name));
            }
            return session;
        } catch (RuntimeException failure) {
            close();
            throw normalized(failure);
        }
    }

    private McpClientWrapper connect(
            RemoteEndpoint remote, String tenant, Function<String, String> credentials) {
        return connect(remote, tenant, credentials, null);
    }

    private McpClientWrapper connect(
            RemoteEndpoint remote,
            String tenant,
            Function<String, String> credentials,
            Long deadline) {
        if (!remote.tenants().isEmpty() && !remote.tenants().contains(tenant)) {
            throw new Blocked("mcp_tenant_denied");
        }
        if (remote.service().equals("taibu") && remote.external()) {
            throw new Blocked("confidential_egress_denied");
        }
        if (!remote.external()) {
            try {
                for (var address : addresses(URI.create(remote.url()).getHost(), deadline)) {
                    if (!address.isLoopbackAddress()
                            && !address.isSiteLocalAddress()
                            && !address.isLinkLocalAddress()) {
                        throw new Blocked("internal_mcp_host_required");
                    }
                }
            } catch (java.net.UnknownHostException e) {
                throw new Blocked("mcp_unavailable");
            }
        }
        var timeout = Duration.ofMillis(remote.timeoutMillis());
        var client =
                McpClientBuilder.create(remote.service())
                        .streamableHttpTransport(remote.url())
                        .timeout(timeout)
                        .initializationTimeout(timeout)
                        .customizeStreamableHttpClient(
                                b ->
                                        b.followRedirects(HttpClient.Redirect.NEVER)
                                                .connectTimeout(timeout))
                        .httpRequestCustomizer(
                                (b, method, uri, body, context) -> {
                                    String token = credentials.apply(remote.credentialRef());
                                    if (!token.isBlank()) {
                                        b.header("Authorization", "Bearer " + token);
                                    }
                                })
                        .buildSync();
        clients.add(client);
        return client;
    }

    private static java.net.InetAddress[] addresses(String host, Long deadline)
            throws java.net.UnknownHostException {
        if (deadline == null) {
            return java.net.InetAddress.getAllByName(host);
        }
        // DNS is synchronous in the JDK. Bound the caller's wait without holding a DB transaction;
        // cancellation may leave an OS resolver finishing in a daemon virtual thread.
        var lookup =
                new java.util.concurrent.FutureTask<>(
                        () -> java.net.InetAddress.getAllByName(host));
        Thread.ofVirtual().name("mcp-discovery-dns").start(lookup);
        try {
            return lookup.get(
                    remaining(deadline).toNanos(), java.util.concurrent.TimeUnit.NANOSECONDS);
        } catch (java.util.concurrent.TimeoutException failure) {
            throw new Blocked("mcp_discovery_timeout");
        } catch (InterruptedException failure) {
            Thread.currentThread().interrupt();
            throw new Blocked("mcp_unavailable");
        } catch (java.util.concurrent.ExecutionException failure) {
            throw new Blocked("mcp_unavailable");
        } finally {
            lookup.cancel(true);
        }
    }

    public static final class SelectedSession {
        private final McpClientWrapper client;
        private final String service;
        private final List<String> selected;
        private final DiscoveryLimits limits;

        private SelectedSession(
                McpClientWrapper client,
                String service,
                List<String> selected,
                DiscoveryLimits limits) {
            this.client = client;
            this.service = service;
            this.selected = List.copyOf(selected);
            this.limits = limits;
        }

        /** fresh tools/list；不使用初始化缓存。每次调用都在实际工具副作用前完成比较。 */
        public Mono<Void> verify(List<CapabilityBinding> frozen) {
            return Mono.defer(
                    () -> {
                        if (TransactionSynchronizationManager.isActualTransactionActive()) {
                            return Mono.error(new Blocked("mcp_discovery_in_transaction"));
                        }
                        return discover(limits.deadline())
                                .doOnNext(
                                        discovered -> {
                                            if (frozen.size() != selected.size()
                                                    || !new HashSet<>(
                                                                    frozen.stream()
                                                                            .map(
                                                                                    CapabilityBinding
                                                                                            ::name)
                                                                            .toList())
                                                            .equals(new HashSet<>(selected))
                                                    || !discovered.keySet().containsAll(selected)) {
                                                throw new Blocked("mcp_contract_changed");
                                            }
                                            for (var expected : frozen) {
                                                var tool = discovered.get(expected.name());
                                                if (tool == null
                                                        || !selected.contains(expected.name())) {
                                                    throw new Blocked("mcp_contract_changed");
                                                }
                                                McpContracts.requireMatch(expected, service, tool);
                                            }
                                        })
                                .onErrorMap(
                                        failure -> {
                                            var normalized = normalized(failure);
                                            return Set.of(
                                                                    "mcp_contract_invalid",
                                                                    "mcp_tool_unavailable")
                                                            .contains(normalized.getMessage())
                                                    ? new Blocked("mcp_contract_changed")
                                                    : normalized;
                                        })
                                .then();
                    });
        }

        private Mono<Map<String, AgentTool>> discover(long deadline) {
            return client.listTools()
                    .timeout(remaining(deadline))
                    .map(tools -> checked(tools, client, limits, deadline))
                    .onErrorMap(McpTools::normalized);
        }
    }

    private static Map<String, AgentTool> checked(
            List<McpSchema.Tool> tools,
            McpClientWrapper client,
            DiscoveryLimits limits,
            long deadline) {
        if (tools.size() > limits.maxTools()) {
            throw new Blocked("mcp_discovery_limit_exceeded");
        }
        var result = new LinkedHashMap<String, AgentTool>();
        long totalBytes = 0;
        for (var source : tools) {
            remaining(deadline);
            var nativeTool = McpContracts.nativeTool(source, client);
            if (result.putIfAbsent(nativeTool.getName(), nativeTool) != null) {
                throw new Blocked("mcp_contract_invalid");
            }
            int bytes = McpContracts.json(nativeTool).getBytes(StandardCharsets.UTF_8).length;
            totalBytes += bytes;
            if (bytes > limits.maxContractBytes() || totalBytes > limits.maxTotalBytes()) {
                throw new Blocked("mcp_discovery_limit_exceeded");
            }
        }
        return Collections.unmodifiableMap(result);
    }

    private static Duration remaining(long deadline) {
        long nanos = deadline - System.nanoTime();
        if (nanos <= 0) {
            throw new Blocked("mcp_discovery_timeout");
        }
        return Duration.ofNanos(nanos);
    }

    private static Blocked normalized(Throwable failure) {
        if (failure instanceof Blocked blocked) {
            return blocked;
        }
        for (Throwable cause = failure; cause != null; cause = cause.getCause()) {
            if (cause instanceof java.util.concurrent.TimeoutException
                    || (cause instanceof IllegalStateException
                            && cause.getMessage() != null
                            && cause.getMessage().startsWith("Timeout on blocking read"))) {
                return new Blocked("mcp_discovery_timeout");
            }
        }
        return new Blocked("mcp_unavailable");
    }

    @Override
    public void close() {
        clients.forEach(McpClientWrapper::close);
        clients.clear();
    }
}
