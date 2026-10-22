package com.financeclaw.infrastructure.agentscope.execution;

import com.financeclaw.app.artifact.ArtifactService;
import com.financeclaw.app.execution.ExecutionPolicyService;
import com.financeclaw.app.release.ReleaseService;
import com.financeclaw.app.release.port.Credentials;
import com.financeclaw.app.report.ReportService;
import com.financeclaw.domain.Governance.*;
import com.financeclaw.domain.Task.*;
import com.financeclaw.domain.release.ReleaseDefinition;
import com.financeclaw.infrastructure.agentscope.AgentStateInspector;
import com.financeclaw.infrastructure.agentscope.ResumeInputs;
import com.financeclaw.infrastructure.agentscope.capability.*;
import com.financeclaw.infrastructure.agentscope.skills.PackagedSkills;
import com.financeclaw.support.PayloadJson;
import com.financeclaw.support.ToolSchemas;
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
  public static final Map<String, Object> CITY = LocalToolContribution.CITY;
  public static final Map<String, Object> TITLE = LocalToolContribution.TITLE;
  public static final Map<String, Object> EMPTY = LocalToolContribution.EMPTY;
  public static final Set<String> NAMES = LocalToolContribution.NAMES;
  private final ExecutionPolicyService policy;
  private final ReleaseService releases;
  private final Credentials credentials;
  private final ArtifactService artifacts;
  private final ReportService reports;
  private final Claim claim;
  private final ReleaseDefinition release;
  private final List<String> scopes;
  private final McpToolContribution mcp;
  private io.agentscope.core.skill.repository.ClasspathSkillRepository skills;
  private final Map<String, InteractionHandler> interactions = new HashMap<>();
  public final Toolkit toolkit = new Toolkit();

  public GovernedTools(
      ExecutionPolicyService policy,
      ReleaseService releases,
      Credentials credentials,
      ArtifactService artifacts,
      ReportService reports,
      Claim claim) {
    this(policy, releases, credentials, artifacts, reports, claim, null);
  }

  private GovernedTools(
      ExecutionPolicyService policy,
      ReleaseService releases,
      Credentials credentials,
      ArtifactService artifacts,
      ReportService reports,
      Claim claim,
      List<String> allowed) {
    this.policy = policy;
    this.releases = releases;
    this.credentials = credentials;
    this.artifacts = artifacts;
    this.reports = reports;
    this.claim = claim;
    release = releases.require(claim.turn().releaseHash());
    var root = policy.scopes(claim);
    if (allowed == null) allowed = root;
    if (!root.containsAll(allowed)) throw new Blocked("scope_expansion_denied");
    scopes = List.copyOf(allowed.stream().filter(release.scopes()::contains).toList());
    new LocalToolContribution(artifacts, reports, claim).contribute(this::register);
    mcp = new McpToolContribution(policy, credentials, artifacts, claim, release, scopes, toolkit);
    try {
      mcp.contribute(this::register);
      if (scopes.contains("skill:use")) {
        if (!release.business().skillHash().equals(PackagedSkills.hash()))
          throw new Blocked("skill_release_changed");
        skills =
            new io.agentscope.core.skill.repository.ClasspathSkillRepository(PackagedSkills.SKILLS);
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
                      policy.consumeTool(claim, "skill:use");
                    }
                  }
                  return next.apply(input);
                });
          }
        });
  }

  public void business(io.agentscope.core.model.Model model, BusinessCapabilities capabilities) {
    if (release.business().ziwei() != null && scopes.contains("ziwei:read"))
      capabilities.ziwei(claim, this::register);
    capabilities.contribute(claim, model, scopes.contains("workflow:run"), this::register);
  }

  public Msg resumeInput(
      io.agentscope.core.state.AgentStateStore store,
      com.financeclaw.domain.ExecutionScope scope,
      Interaction interaction,
      Object answer) {
    var call = ResumeInputs.pending(store, scope, interaction.stateVersion(), interaction.callId());
    if (!ResumeInputs.actionHash(call).equals(interaction.actionHash())
        || !call.getName().equals(interaction.tool())
        || call.getState() == ToolCallState.ASKING) throw new Blocked("checkpoint_mismatch");
    var handler = interactions.get(interaction.tool());
    if (handler == null || handler.approval()) throw new Blocked("unsupported_interaction");
    return handler.resume(call, answer);
  }

  /**
   * Future child/workflow entry points inherit the same Turn counters and may only narrow scopes.
   */
  public GovernedTools narrow(List<String> requested) {
    if (!scopes.containsAll(requested)) throw new Blocked("scope_expansion_denied");
    return new GovernedTools(
        policy, releases, credentials, artifacts, reports, claim, List.copyOf(requested));
  }

  public static Map<String, Object> schema(String key, Map<String, Object> property) {
    return ToolSchemas.required(key, property);
  }

  public PermissionContextState permissions() {
    var builder = PermissionContextState.builder().mode(PermissionMode.DEFAULT);
    for (String name : toolkit.getToolNames()) {
      var rule =
          new PermissionRule(
              name,
              null,
              (interactions.containsKey(name) && interactions.get(name).approval())
                  ? PermissionBehavior.ASK
                  : PermissionBehavior.ALLOW,
              "release-policy");
      if (interactions.containsKey(name) && interactions.get(name).approval())
        builder.addAskRule(name, rule);
      else builder.addAllowRule(name, rule);
    }
    return builder.build();
  }

  private void register(
      String name,
      String description,
      Map<String, Object> schema,
      String scope,
      Function<ToolCallParam, Mono<ToolResultBlock>> action,
      InteractionHandler interaction) {
    if (scope != null && (!scopes.contains(scope) || !release.scopes().contains(scope))) return;
    toolkit.registerAgentTool(
        new ToolBase(ToolBase.builder().name(name).description(description).inputSchema(schema)) {
          @Override
          public Mono<ToolResultBlock> callAsync(ToolCallParam param) {
            return Mono.defer(
                    () -> {
                      // SDK 2.0.3 validates raw content before merging input/presets. This is
                      // the effective-input boundary, also used by direct adapter calls.
                      if (ToolValidator.validateInput(
                              PayloadJson.canonical(param.getInput()), schema)
                          != null) throw new Blocked("invalid_tool_input");
                      policy.consumeTool(claim, scope);
                      return action.apply(param);
                    })
                .onErrorMap(
                    error ->
                        error instanceof Blocked ? error : new Blocked("tool_execution_failed"))
                .subscribeOn(Schedulers.boundedElastic());
          }
        });
    if (interaction != null) interactions.put(name, interaction);
  }

  public List<Pending> pending(io.agentscope.core.state.AgentState state) {
    return new AgentStateInspector(state)
        .pending().stream()
            .map(
                call -> {
                  var handler = interactions.get(call.getName());
                  if (handler == null
                      || (call.getState() == ToolCallState.ASKING && !handler.approval()))
                    throw new Blocked("unsupported_interaction");
                  return handler.pending(call);
                })
            .toList();
  }

  @Override
  public void close() {
    mcp.close();
    if (skills != null) skills.close();
  }
}
