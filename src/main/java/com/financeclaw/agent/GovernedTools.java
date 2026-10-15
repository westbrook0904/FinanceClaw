package com.financeclaw.agent;

import com.financeclaw.application.*;
import com.financeclaw.domain.Governance.*;
import com.financeclaw.domain.Task.*;
import io.agentscope.core.message.*;
import io.agentscope.core.permission.*;
import io.agentscope.core.tool.*;
import io.agentscope.core.tool.mcp.*;
import java.time.Duration;
import java.util.*;
import java.util.function.Function;
import reactor.core.publisher.Mono;
import reactor.core.scheduler.Schedulers;
import reactor.util.retry.Retry;

/** A ToolBase boundary, shared by local tools and SDK MCP transport. No model-supplied identity. */
public final class GovernedTools implements AutoCloseable {
  public static final Map<String, Object> CITY =
      schema("city", Map.of("type", "string", "minLength", 1, "maxLength", 100));
  public static final Map<String, Object> TITLE =
      schema("title", Map.of("type", "string", "minLength", 1, "maxLength", 200));
  public static final Map<String, Object> EMPTY =
      Map.of("type", "object", "properties", Map.of(), "additionalProperties", false);
  public static final Set<String> NAMES =
      Set.of("request_city", "create_report", "search_catalog", "read_artifact", "mcp_lookup");
  private final TaskService tasks;
  private final GovernanceService governance;
  private final Claim claim;
  private final ReleaseCatalog.Release release;
  private final List<String> scopes;
  private McpClientWrapper mcp;
  public final Toolkit toolkit = new Toolkit();

  public GovernedTools(TaskService tasks, GovernanceService governance, Claim claim) {
    this(
        tasks,
        governance,
        claim,
        GovernanceService.scopes(tasks.governance().policy(claim.turn().id()).scopes()));
  }

  private GovernedTools(
      TaskService tasks, GovernanceService governance, Claim claim, List<String> allowed) {
    this.tasks = tasks;
    this.governance = governance;
    this.claim = claim;
    release = tasks.releases().require(claim.turn().releaseHash());
    var root = GovernanceService.scopes(tasks.governance().policy(claim.turn().id()).scopes());
    if (!root.containsAll(allowed)) throw new Blocked("scope_expansion_denied");
    scopes = List.copyOf(allowed.stream().filter(release.scopes()::contains).toList());
    register(
        "request_city",
        EMPTY,
        null,
        p -> Mono.just(ToolResultBlock.suspended(p.getToolUseBlock())));
    register(
        "search_catalog",
        CITY,
        "catalog:read",
        p ->
            Mono.fromCallable(
                () ->
                    archived(
                        List.of(
                            Map.of(
                                "city",
                                p.getInput().get("city"),
                                "name",
                                "示例酒店",
                                "source",
                                "local-fixture")))));
    register(
        "create_report",
        TITLE,
        "report:write",
        p ->
            Mono.fromCallable(
                () ->
                    ToolResultBlock.text(
                        governance.report(
                            claim,
                            p.getToolUseBlock().getId(),
                            ResumeInputs.actionHash(p.getToolUseBlock()),
                            (String) p.getInput().get("title")))));
    register(
        "read_artifact",
        Map.of(
            "type",
            "object",
            "properties",
            Map.of(
                "artifact_id",
                Map.of("type", "string"),
                "start",
                Map.of("type", "integer", "minimum", 0),
                "limit",
                Map.of("type", "integer", "minimum", 1, "maximum", 100)),
            "required",
            List.of("artifact_id"),
            "additionalProperties",
            false),
        "artifact:read",
        p ->
            Mono.fromCallable(
                () ->
                    ToolResultBlock.text(
                        ResumeInputs.canonical(
                            governance.read(
                                new Owner(claim.turn().tenant(), claim.turn().subject()),
                                (String) p.getInput().get("artifact_id"),
                                ((Number) p.getInput().getOrDefault("start", 0)).intValue(),
                                ((Number) p.getInput().getOrDefault("limit", 20)).intValue())))));
    if (!release.mcpUrl().isBlank())
      register(
          "mcp_lookup",
          GovernanceService.object(release.mcpSchema()),
          "catalog:read",
          this::mcpRead);
  }

  /**
   * Future child/workflow entry points inherit the same Turn counters and may only narrow scopes.
   */
  public GovernedTools narrow(List<String> requested) {
    if (!scopes.containsAll(requested)) throw new Blocked("scope_expansion_denied");
    return new GovernedTools(tasks, governance, claim, List.copyOf(requested));
  }

  public static Map<String, Object> schema(String key, Map<String, Object> property) {
    return Map.of(
        "type",
        "object",
        "properties",
        Map.of(key, property),
        "required",
        List.of(key),
        "additionalProperties",
        false);
  }

  public PermissionContextState permissions() {
    var builder = PermissionContextState.builder().mode(PermissionMode.DEFAULT);
    for (String name : toolkit.getToolNames()) {
      var rule =
          new PermissionRule(
              name,
              null,
              name.equals("create_report") ? PermissionBehavior.ASK : PermissionBehavior.ALLOW,
              "release-policy");
      if (name.equals("create_report")) builder.addAskRule(name, rule);
      else builder.addAllowRule(name, rule);
    }
    return builder.build();
  }

  private void register(
      String name,
      Map<String, Object> schema,
      String scope,
      Function<ToolCallParam, Mono<ToolResultBlock>> action) {
    if (scope != null && (!scopes.contains(scope) || !release.scopes().contains(scope))) return;
    toolkit.registerAgentTool(
        new ToolBase(
            ToolBase.builder().name(name).description("Governed " + name).inputSchema(schema)) {
          @Override
          public Mono<ToolResultBlock> callAsync(ToolCallParam param) {
            return Mono.defer(
                    () -> {
                      tasks.checkDispatch(claim);
                      if (scope != null
                          && !GovernanceService.scopes(
                                  tasks.governance().policy(claim.turn().id()).scopes())
                              .contains(scope)) throw new Blocked("tool_denied");
                      if (ToolValidator.validateInput(
                              ResumeInputs.canonical(param.getInput()), schema)
                          != null) throw new Blocked("invalid_tool_input");
                      tasks.consume(claim, false);
                      return action.apply(param);
                    })
                .onErrorMap(
                    error ->
                        error instanceof Blocked ? error : new Blocked("tool_execution_failed"))
                .subscribeOn(Schedulers.boundedElastic());
          }
        });
  }

  private ToolResultBlock archived(Object result) {
    var a = governance.archive(claim, result);
    return ToolResultBlock.text(
        ResumeInputs.canonical(
            Map.of("artifact_id", a.id(), "hash", a.hash(), "bytes", a.bytes())));
  }

  private synchronized McpClientWrapper client() {
    if (mcp == null) {
      // Endpoint and contract are release configuration, never tool arguments. No writes exposed.
      mcp =
          McpClientBuilder.create("release-mcp")
              .streamableHttpTransport(release.mcpUrl())
              .timeout(Duration.ofSeconds(5))
              .initializationTimeout(Duration.ofSeconds(5))
              .buildSync();
      mcp.initialize().block(Duration.ofSeconds(6));
    }
    return mcp;
  }

  /** SDK JsonSchema records contain absent optional fields as null; they are not constraints. */
  static String schemaJson(Object value) {
    var mapper = new com.fasterxml.jackson.databind.ObjectMapper();
    com.fasterxml.jackson.databind.JsonNode node = mapper.valueToTree(value);
    removeNulls(node);
    return ResumeInputs.canonical(mapper.convertValue(node, Object.class));
  }

  private static void removeNulls(com.fasterxml.jackson.databind.JsonNode node) {
    // Only optional fields of the root MCP JsonSchema record. Nested enum/const data is opaque.
    if (node.isObject())
      for (String field :
          List.of(
              "type",
              "properties",
              "required",
              "additionalProperties",
              "defs",
              "$defs",
              "definitions"))
        if (node.has(field) && node.get(field).isNull())
          ((com.fasterxml.jackson.databind.node.ObjectNode) node).remove(field);
  }

  private Mono<ToolResultBlock> mcpRead(ToolCallParam param) {
    return Mono.defer(
            () -> {
              tasks.checkDispatch(claim);
              var client = client();
              return client
                  .listTools()
                  .flatMap(
                      tools -> {
                        var remote =
                            tools.stream()
                                .filter(t -> t.name().equals(release.mcpTool()))
                                .findFirst()
                                .orElseThrow(() -> new Blocked("mcp_contract_changed"));
                        if (!schemaJson(remote.inputSchema())
                            .equals(
                                ResumeInputs.canonical(
                                    GovernanceService.object(release.mcpSchema()))))
                          throw new Blocked("mcp_contract_changed");
                        return client.callTool(release.mcpTool(), param.getInput());
                      })
                  .map(
                      result -> {
                        if (Boolean.TRUE.equals(result.isError()))
                          throw new Blocked("mcp_service_error");
                        return archived(
                            result.structuredContent() != null
                                ? result.structuredContent()
                                : result.content());
                      });
            })
        .retryWhen(
            Retry.backoff(1, Duration.ofMillis(100))
                .filter(
                    e ->
                        e instanceof java.util.concurrent.TimeoutException
                            || e instanceof java.io.IOException)
                .onRetryExhaustedThrow((spec, signal) -> signal.failure()));
  }

  public List<Pending> pending(io.agentscope.core.state.AgentState state) {
    List<Pending> result = new ArrayList<>();
    Set<String> resolved = new HashSet<>();
    state
        .getContext()
        .forEach(
            m ->
                m.getContentBlocks(ToolResultBlock.class).stream()
                    .filter(r -> !r.isSuspended())
                    .forEach(r -> resolved.add(r.getId())));
    for (var message : state.getContext())
      for (var call : message.getContentBlocks(ToolUseBlock.class))
        if (!resolved.contains(call.getId())) {
          if (call.getState() == ToolCallState.ASKING && call.getName().equals("create_report"))
            result.add(
                new Pending(
                    "approval",
                    call.getId(),
                    call.getName(),
                    ResumeInputs.actionHash(call),
                    "{}",
                    "是否创建报告：" + Objects.toString(call.getInput().get("title"), "")));
          else if (call.getName().equals("request_city"))
            result.add(
                new Pending(
                    "input",
                    call.getId(),
                    call.getName(),
                    ResumeInputs.actionHash(call),
                    ResumeInputs.canonical(CITY),
                    "请输入城市"));
          else throw new Blocked("unsupported_interaction");
        }
    return result;
  }

  @Override
  public void close() {
    if (mcp != null) mcp.close();
  }
}
