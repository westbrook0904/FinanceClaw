package com.financeclaw.agent;

import com.financeclaw.application.*;
import com.financeclaw.domain.Governance.*;
import com.financeclaw.domain.Task.*;
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
  public static final Map<String, Object> CITY =
      schema("city", Map.of("type", "string", "minLength", 1, "maxLength", 100));
  public static final Map<String, Object> TITLE =
      schema("title", Map.of("type", "string", "minLength", 1, "maxLength", 200));
  public static final Map<String, Object> EMPTY =
      Map.of("type", "object", "properties", Map.of(), "additionalProperties", false);
  public static final Set<String> NAMES =
      Set.of("request_city", "create_report", "search_catalog", "read_artifact");
  private final TaskService tasks;
  private final GovernanceService governance;
  private final Claim claim;
  private final ReleaseCatalog.Release release;
  private final List<String> scopes;
  private final McpTools mcp = new McpTools();
  private io.agentscope.core.skill.repository.ClasspathSkillRepository skills;
  private MarketResearch research;
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
                "path",
                Map.of("type", "string", "maxLength", 256),
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
                                ((Number) p.getInput().getOrDefault("limit", 20)).intValue(),
                                (String) p.getInput().getOrDefault("path", ""))))));
    try {
      for (var service : release.business().remotes()) {
        String scope = service.service().equals("taibu") ? "taibu:read" : "travel:read";
        if (!scopes.contains(scope)) continue;
        tasks.checkDispatch(claim);
        mcp.register(toolkit, service, claim.turn().tenant());
        for (String name : service.tools()) {
          var nativeTool = toolkit.getTool(name);
          // Keep the native name/schema/call implementation. This adapter only enforces product
          // policy and archives results before they enter model context.
          register(
              name,
              nativeTool.getParameters(),
              scope,
              p -> {
                if (service.service().equals("taibu")) TaibuInputs.validate(name, p.getInput());
                return nativeTool
                    .callAsync(p)
                    .map(
                        result -> {
                          if (result.getState() == ToolResultState.ERROR)
                            throw new Blocked("mcp_service_error");
                          String text =
                              result.getOutput().stream()
                                  .filter(TextBlock.class::isInstance)
                                  .map(TextBlock.class::cast)
                                  .map(TextBlock::getText)
                                  .collect(java.util.stream.Collectors.joining("\n"));
                          Object data;
                          try {
                            data = GovernanceService.parse(text);
                          } catch (Blocked e) {
                            throw new Blocked("mcp_invalid_result");
                          }
                          if (data instanceof Map<?, ?> map
                              && (Boolean.FALSE.equals(map.get("success"))
                                  || map.containsKey("error")))
                            throw new Blocked("mcp_service_error");
                          if (service.service().equals("taibu"))
                            TaibuInputs.validateResult(name, data, p.getInput());
                          return archived(data, scope);
                        });
              },
              nativeTool);
        }
      }
      if (scopes.contains("skill:use")) {
        if (!release.business().skillHash().equals(BusinessCatalog.skillHash()))
          throw new Blocked("skill_release_changed");
        skills =
            new io.agentscope.core.skill.repository.ClasspathSkillRepository(
                BusinessCatalog.SKILLS);
      }
    } catch (java.io.IOException | RuntimeException e) {
      close();
      throw e instanceof Blocked blocked ? blocked : new Blocked("capability_unavailable");
    }
  }

  public void configure(io.agentscope.core.ReActAgent.Builder builder) {
    if (skills == null) return;
    builder.skillRepository(skills).skillCodeExecutionEnabled(false);
    // The SDK's skill access tool is an AgentTool in 2.0.3; enforce business grants through
    // native middleware without replacing its loader, tools, prompt or activation state.
    builder.middleware(
        new io.agentscope.core.middleware.MiddlewareBase() {
          @Override
          public reactor.core.publisher.Flux<io.agentscope.core.event.AgentEvent> onActing(
              io.agentscope.core.agent.Agent agent,
              io.agentscope.core.agent.RuntimeContext context,
              io.agentscope.core.middleware.ActingInput input,
              java.util.function.Function<
                      io.agentscope.core.middleware.ActingInput,
                      reactor.core.publisher.Flux<io.agentscope.core.event.AgentEvent>>
                  next) {
            return reactor.core.publisher.Flux.defer(
                () -> {
                  for (var call : input.toolCalls()) {
                    if (call.getName().equals("load_skill_through_path")) {
                      tasks.checkDispatch(claim);
                      if (!GovernanceService.scopes(
                              tasks.governance().policy(claim.turn().id()).scopes())
                          .contains("skill:use")) throw new Blocked("tool_denied");
                      tasks.consume(claim, false);
                    }
                  }
                  return next.apply(input);
                });
          }
        });
  }

  public void business(
      io.agentscope.core.model.Model model,
      com.financeclaw.infrastructure.state.RuntimeMapper states) {
    research = new MarketResearch(tasks, claim, states, model);
    register(
        "market_snapshot",
        MarketData.SCHEMA,
        "market:read",
        p ->
            Mono.fromCallable(
                () ->
                    ToolResultBlock.text(
                        ResumeInputs.canonical(
                            new MarketData(tasks, claim)
                                .read((String) p.getInput().get("symbol"))))));
    register("market_research", MarketResearch.SCHEMA, "research:run", research::run);
    var workflow = new PortfolioReview(tasks, governance, claim);
    register(
        "portfolio_review",
        PortfolioReview.SCHEMA,
        "workflow:run",
        p ->
            Mono.fromCallable(
                () ->
                    ToolResultBlock.text(ResumeInputs.canonical(workflow.prepare(p.getInput())))));
    if (scopes.contains("workflow:run"))
      register(
          "publish_portfolio_report",
          PortfolioReview.PUBLISH,
          "report:write",
          p ->
              Mono.fromCallable(
                  () ->
                      ToolResultBlock.text(
                          ResumeInputs.canonical(
                              workflow.publish((String) p.getInput().get("draft_hash"))))));
  }

  public Msg resumeInput(
      io.agentscope.core.state.AgentStateStore store,
      com.financeclaw.domain.ExecutionScope scope,
      Interaction i,
      Object answer) {
    if (i.tool().equals("request_city"))
      return ResumeInputs.clarification(
          store,
          scope,
          i.stateVersion(),
          i.callId(),
          i.actionHash(),
          (String) ((Map<?, ?>) answer).get("city"));
    if (!i.tool().equals("market_research")) throw new Blocked("unsupported_interaction");
    var call = ResumeInputs.pending(store, scope, i.stateVersion(), i.callId());
    if (!ResumeInputs.actionHash(call).equals(i.actionHash()))
      throw new Blocked("checkpoint_mismatch");
    var input = new TreeMap<>(call.getInput());
    ((Map<?, ?>) answer).forEach((k, v) -> input.put((String) k, v));
    if (MarketResearch.missing(input)) throw new Blocked("research_scope_missing");
    tasks.consume(claim, false);
    var result =
        research
            .run(ToolCallParam.builder().toolUseBlock(call).input(input).build())
            .block(java.time.Duration.ofSeconds(20));
    return Msg.builder()
        .name("tool")
        .role(MsgRole.TOOL)
        .content(
            ToolResultBlock.of(call.getId(), call.getName(), result.getOutput())
                .withState(ToolResultState.SUCCESS))
        .build();
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
              Set.of("create_report", "publish_portfolio_report").contains(name)
                  ? PermissionBehavior.ASK
                  : PermissionBehavior.ALLOW,
              "release-policy");
      if (Set.of("create_report", "publish_portfolio_report").contains(name))
        builder.addAskRule(name, rule);
      else builder.addAllowRule(name, rule);
    }
    return builder.build();
  }

  private void register(
      String name,
      Map<String, Object> schema,
      String scope,
      Function<ToolCallParam, Mono<ToolResultBlock>> action) {
    register(name, schema, scope, action, null);
  }

  private void register(
      String name,
      Map<String, Object> schema,
      String scope,
      Function<ToolCallParam, Mono<ToolResultBlock>> action,
      AgentTool nativeTool) {
    if (scope != null && (!scopes.contains(scope) || !release.scopes().contains(scope))) return;
    toolkit.registerAgentTool(
        new ToolBase(
            ToolBase.builder()
                .name(name)
                .description(nativeTool == null ? "Governed " + name : nativeTool.getDescription())
                .inputSchema(schema)) {
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
    return archived(result, "artifact:read");
  }

  private ToolResultBlock archived(Object result, String scope) {
    var a = governance.archive(claim, result, scope);
    return ToolResultBlock.text(
        ResumeInputs.canonical(
            Map.of("artifact_id", a.id(), "hash", a.hash(), "bytes", a.bytes())));
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
          if (call.getState() == ToolCallState.ASKING
              && Set.of("create_report", "publish_portfolio_report").contains(call.getName()))
            result.add(
                new Pending(
                    "approval",
                    call.getId(),
                    call.getName(),
                    ResumeInputs.actionHash(call),
                    "{}",
                    call.getName().equals("publish_portfolio_report")
                        ? "是否发布投资组合复盘报告？"
                        : "是否创建报告：" + Objects.toString(call.getInput().get("title"), "")));
          else if (call.getName().equals("request_city"))
            result.add(
                new Pending(
                    "input",
                    call.getId(),
                    call.getName(),
                    ResumeInputs.actionHash(call),
                    ResumeInputs.canonical(CITY),
                    "请输入城市"));
          else if (call.getName().equals("market_research"))
            result.add(
                new Pending(
                    "input",
                    call.getId(),
                    call.getName(),
                    ResumeInputs.actionHash(call),
                    ResumeInputs.canonical(MarketResearch.ANSWER),
                    "请提供研究证券代码和分析期间"));
          else throw new Blocked("unsupported_interaction");
        }
    return result;
  }

  @Override
  public void close() {
    mcp.close();
    if (skills != null) skills.close();
  }
}
