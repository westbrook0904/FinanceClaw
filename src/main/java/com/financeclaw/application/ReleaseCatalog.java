package com.financeclaw.application;

import com.financeclaw.domain.ExecutionScope;
import com.financeclaw.domain.Governance.Blocked;
import com.financeclaw.support.PayloadJson;
import java.util.*;
import java.util.concurrent.ConcurrentHashMap;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.stereotype.Component;

/**
 * Immutable release definitions; changing configuration creates a new hash, never mutates a running
 * Turn.
 */
@Component
public class ReleaseCatalog {
  public record ModelProfile(
      String name,
      String baseUrl,
      int inputLimit,
      int window,
      int outputReserve,
      int margin,
      boolean thinking) {}

  public record Release(
      String version,
      String agent,
      List<String> scopes,
      ModelProfile primary,
      ModelProfile fallback,
      int maxModelCalls,
      int maxToolCalls,
      com.financeclaw.tools.BusinessCatalog.Features business) {
    public Release(
        String version,
        String agent,
        List<String> scopes,
        ModelProfile primary,
        ModelProfile fallback,
        int maxModelCalls,
        int maxToolCalls) {
      this(
          version,
          agent,
          scopes,
          primary,
          fallback,
          maxModelCalls,
          maxToolCalls,
          com.financeclaw.tools.BusinessCatalog.defaults());
    }

    public Release {
      scopes = List.copyOf(scopes);
    }

    public String hash() {
      return ExecutionScope.hash(List.of("agentscope-2.0.3", PayloadJson.canonical(this)));
    }
  }

  public static final List<String> SCOPES =
      List.of(
          "catalog:read",
          "report:write",
          "artifact:read",
          "travel:read",
          "taibu:read",
          "skill:use",
          "market:read",
          "research:run",
          "workflow:run",
          "ziwei:read");
  public static final ModelProfile OFFLINE =
      new ModelProfile("offline", "", 65536, 131072, 2048, 512, false);
  private final Map<String, Release> releases = new ConcurrentHashMap<>();
  private volatile String current;
  private final String apiKey;
  private java.util.function.Function<String, String> credentials = System::getenv;

  public ReleaseCatalog() {
    this("offline", "", "", false);
  }

  public ReleaseCatalog(
      @Value("${financeclaw.model.name:offline}") String model,
      @Value("${financeclaw.model.base-url:}") String url,
      @Value("${financeclaw.model.api-key:}") String apiKey,
      @Value("${financeclaw.model.thinking:false}") boolean thinking) {
    this.apiKey = apiKey;
    publish(
        new Release(
            "m3-v1",
            "finance_agent",
            SCOPES,
            new ModelProfile(model, url, 65536, 131072, 2048, 512, thinking),
            null,
            12,
            32));
  }

  @org.springframework.beans.factory.annotation.Autowired
  public ReleaseCatalog(org.springframework.core.env.Environment env) {
    this.credentials = env::getProperty;
    this.apiKey = env.getProperty("financeclaw.model.api-key", "");
    ModelProfile primary = profile(env, "financeclaw.model", "offline");
    String fallback = env.getProperty("financeclaw.fallback.name", "");
    publish(
        new Release(
            "m3-v1",
            "finance_agent",
            SCOPES,
            primary,
            fallback.isBlank() ? null : profile(env, "financeclaw.fallback", fallback),
            12,
            32,
            com.financeclaw.tools.BusinessCatalog.load(env)));
  }

  private static ModelProfile profile(
      org.springframework.core.env.Environment e, String prefix, String defaultName) {
    return new ModelProfile(
        e.getProperty(prefix + ".name", defaultName),
        e.getProperty(prefix + ".base-url", ""),
        e.getProperty(prefix + ".input-limit", Integer.class, 65536),
        e.getProperty(prefix + ".context-window", Integer.class, 131072),
        e.getProperty(prefix + ".output-reserve", Integer.class, 2048),
        e.getProperty(prefix + ".safety-margin", Integer.class, 512),
        e.getProperty(prefix + ".thinking", Boolean.class, false));
  }

  public String currentHash() {
    return current;
  }

  public String apiKey() {
    return apiKey;
  }

  public String credential(String ref) {
    return com.financeclaw.tools.BusinessCatalog.credential(ref, credentials);
  }

  public synchronized void publish(Release release) {
    if (!"finance_agent".equals(release.agent())
        || !SCOPES.containsAll(release.scopes())
        || release.maxModelCalls() < 1
        || release.maxToolCalls() < 1) throw new IllegalArgumentException("Invalid release policy");
    validate(release.primary());
    if (release.business().ziwei() != null) release.business().ziwei().verifyRuntime();
    if (release.fallback() != null) validate(release.fallback());
    String hash = release.hash();
    releases.putIfAbsent(hash, release);
    current = hash;
  }

  private static void validate(ModelProfile p) {
    if (p.thinking() && !p.name().toLowerCase(java.util.Locale.ROOT).contains("qwen"))
      throw new IllegalArgumentException("Thinking profile is only supported for Qwen");
    if (p.inputLimit() < 1
        || p.outputReserve() < 1
        || p.window() <= p.outputReserve() + p.margin()
        || p.margin() < 0) throw new IllegalArgumentException("Invalid model budget");
    if (!p.name().equals("offline") && (p.baseUrl() == null || p.baseUrl().isBlank()))
      throw new IllegalArgumentException("Missing model endpoint");
  }

  public Release require(String hash) {
    Release r = releases.get(hash);
    if (r == null || !r.hash().equals(hash)) throw new Blocked("release_unavailable");
    return r;
  }
}
