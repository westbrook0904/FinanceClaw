package com.financeclaw.domain.release;

import java.net.URI;
import java.util.*;

public record RemoteEndpoint(
    String service,
    String url,
    List<String> allowedHosts,
    String credentialRef,
    boolean external,
    List<String> tenants,
    List<String> tools,
    int timeoutMillis) {
  public RemoteEndpoint {
    allowedHosts = List.copyOf(allowedHosts);
    tenants = List.copyOf(tenants);
    tools = List.copyOf(tools);
    if (tools.isEmpty()) throw new IllegalArgumentException("MCP tool allowlist is required");
    URI uri = URI.create(url);
    if (!Set.of("https", "http").contains(uri.getScheme())
        || uri.getHost() == null
        || uri.getUserInfo() != null
        || uri.getFragment() != null
        || uri.getRawQuery() != null
        || !allowedHosts.contains(uri.getAuthority())
        || timeoutMillis < 100
        || timeoutMillis > 10000
        || (external && !uri.getScheme().equals("https")))
      throw new IllegalArgumentException("Invalid MCP endpoint policy");
  }
}
