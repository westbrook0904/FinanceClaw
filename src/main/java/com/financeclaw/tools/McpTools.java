package com.financeclaw.tools;

import com.financeclaw.domain.Governance.Blocked;
import com.financeclaw.domain.release.RemoteEndpoint;
import com.financeclaw.infrastructure.release.EnvironmentCredentials;
import io.agentscope.core.tool.Toolkit;
import io.agentscope.core.tool.mcp.*;
import java.net.URI;
import java.net.http.HttpClient;
import java.time.Duration;
import java.util.*;
import java.util.function.Function;

/** Deployment policy around AgentScope registration; protocol and tools are SDK-owned. */
public final class McpTools implements AutoCloseable {
  private final List<McpClientWrapper> clients = new ArrayList<>();

  public void register(Toolkit toolkit, RemoteEndpoint remote, String tenant) {
    register(toolkit, remote, tenant, EnvironmentCredentials::lookup);
  }

  public void register(
      Toolkit toolkit, RemoteEndpoint remote, String tenant, Function<String, String> credentials) {
    if (!remote.tenants().isEmpty() && !remote.tenants().contains(tenant))
      throw new Blocked("mcp_tenant_denied");
    if (remote.service().equals("taibu") && remote.external())
      throw new Blocked("confidential_egress_denied");
    if (!Collections.disjoint(toolkit.getToolNames(), remote.tools()))
      throw new Blocked("mcp_tool_name_collision");
    if (!remote.external()) {
      try {
        for (var address : java.net.InetAddress.getAllByName(URI.create(remote.url()).getHost()))
          if (!address.isLoopbackAddress()
              && !address.isSiteLocalAddress()
              && !address.isLinkLocalAddress()) throw new Blocked("internal_mcp_host_required");
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
                b -> b.followRedirects(HttpClient.Redirect.NEVER).connectTimeout(timeout))
            .httpRequestCustomizer(
                (b, method, uri, body, context) -> {
                  String token = credentials.apply(remote.credentialRef());
                  if (!token.isBlank()) b.header("Authorization", "Bearer " + token);
                })
            .buildSync();
    clients.add(client);
    try {
      toolkit.registration().mcpClient(client).enableTools(remote.tools()).apply();
      if (!toolkit.getToolNames().containsAll(remote.tools()))
        throw new Blocked("mcp_tool_unavailable");
    } catch (RuntimeException e) {
      close();
      throw e instanceof Blocked blocked ? blocked : new Blocked("mcp_unavailable");
    }
  }

  @Override
  public void close() {
    clients.forEach(McpClientWrapper::close);
    clients.clear();
  }
}
