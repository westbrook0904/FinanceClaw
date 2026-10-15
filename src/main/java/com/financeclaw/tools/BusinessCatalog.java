package com.financeclaw.tools;

import com.financeclaw.agent.ResumeInputs;
import com.financeclaw.application.GovernanceService;
import com.financeclaw.domain.Governance.Blocked;
import java.net.URI;
import java.nio.charset.StandardCharsets;
import java.nio.file.*;
import java.util.*;
import org.springframework.core.env.Environment;

/** Deployment-only configuration. Secrets are resolved lazily and never enter release snapshots. */
public final class BusinessCatalog {
  public record Remote(
      String service,
      String url,
      List<String> allowedHosts,
      String credentialRef,
      boolean external,
      List<String> tenants,
      List<String> tools,
      int timeoutMillis) {
    public Remote {
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

  public record Features(List<Remote> remotes, String skillHash, String marketJson) {
    public Features {
      remotes = List.copyOf(remotes);
    }
  }

  private BusinessCatalog() {}

  public static Features defaults() {
    return new Features(List.of(), skillHash(), "{}");
  }

  public static Features load(Environment env) {
    if (env.getProperty("financeclaw.business.ziwei.enabled", Boolean.class, false))
      throw new IllegalArgumentException("Ziwei adapter is not available in this release");
    List<Remote> remotes = new ArrayList<>();
    for (String name : List.of("hotel", "taibu")) {
      String key = "financeclaw.business." + name;
      // Disabled integrations do not even resolve their credential or endpoint settings.
      if (!env.getProperty(key + ".enabled", Boolean.class, false)) continue;
      remotes.add(
          new Remote(
              name,
              env.getRequiredProperty(key + ".url"),
              split(env.getRequiredProperty(key + ".allowed-hosts")),
              env.getProperty(key + ".credential-ref", ""),
              env.getProperty(key + ".external", Boolean.class, true),
              split(env.getProperty(key + ".tenants", "")),
              name.equals("hotel")
                  ? List.of("searchHotels", "getHotelDetail", "getHotelSearchTags")
                  : List.of("almanac", "bazi"),
              env.getProperty(key + ".timeout-ms", Integer.class, 5000)));
    }
    String market = env.getProperty("financeclaw.business.market-file", "");
    return new Features(remotes, skillHash(), market.isBlank() ? "{}" : read(market));
  }

  public static final String SKILLS = "skills";

  /** Release fingerprint only. Loading, discovery and activation belong to AgentScope. */
  public static String skillHash() {
    try (var repository =
        new io.agentscope.core.skill.repository.ClasspathSkillRepository(SKILLS)) {
      var assets = new TreeMap<String, Object>();
      for (var skill : repository.getAllSkills())
        assets.put(
            skill.getName(),
            List.of(skill.getMetadata(), skill.getSkillContent(), skill.getResources()));
      return GovernanceService.digest(ResumeInputs.canonical(assets));
    } catch (java.io.IOException e) {
      throw new IllegalArgumentException("Cannot load packaged skills", e);
    }
  }

  private static List<String> split(String text) {
    return Arrays.stream(text.split(",")).map(String::trim).filter(s -> !s.isBlank()).toList();
  }

  public static String read(String location) {
    try {
      if (location.startsWith("classpath:")) {
        try (var in =
            BusinessCatalog.class.getClassLoader().getResourceAsStream(location.substring(10))) {
          if (in == null) throw new IllegalArgumentException("Missing packaged resource");
          return new String(in.readNBytes(2097153), StandardCharsets.UTF_8);
        }
      }
      if (Files.size(Path.of(location)) > 2097152)
        throw new IllegalArgumentException("Resource too large");
      return Files.readString(Path.of(location));
    } catch (java.io.IOException e) {
      throw new IllegalArgumentException("Cannot load business resource");
    }
  }

  public static String credential(String ref) {
    if (ref.isBlank()) return "";
    if (!ref.matches("[A-Z][A-Z0-9_]{0,100}")) throw new Blocked("invalid_credential_reference");
    String value = System.getenv(ref);
    if (value == null || value.isBlank()) throw new Blocked("mcp_credentials_unavailable");
    return value;
  }
}
