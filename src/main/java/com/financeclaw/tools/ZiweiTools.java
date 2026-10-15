package com.financeclaw.tools;

import com.financeclaw.agent.ResumeInputs;
import com.financeclaw.application.*;
import com.financeclaw.domain.Governance.*;
import com.financeclaw.domain.Task.*;
import com.financeclaw.ziwei.*;
import io.agentscope.core.message.*;
import io.agentscope.core.tool.ToolCallParam;
import java.util.*;
import reactor.core.publisher.Mono;

/** Ziwei business adapter for native ToolBase suspension and existing scoped artifacts/receipts. */
public final class ZiweiTools {
  private final TaskService tasks;
  private final GovernanceService governance;
  private final Claim claim;
  private final ZiweiPolicy policy;

  public ZiweiTools(TaskService tasks, GovernanceService governance, Claim claim) {
    this.tasks = tasks;
    this.governance = governance;
    this.claim = claim;
    this.policy = tasks.releases().require(claim.turn().releaseHash()).business().ziwei();
    if (policy == null) throw new Blocked("ziwei_disabled");
    policy.verifyRuntime();
  }

  public ZiweiDates.QueryClock clock() {
    return new ZiweiDates.QueryClock(
        tasks.governance().requestClock(claim.turn().id()), policy.queryTimezone());
  }

  private void authorize() {
    tasks.checkDispatch(claim);
    if (!GovernanceService.scopes(tasks.governance().policy(claim.turn().id()).scopes())
        .contains("ziwei:read")) throw new Blocked("ziwei_denied");
  }

  public Pending pending(ToolUseBlock call) {
    authorize();
    var review = ZiweiInputs.inspect(call.getName(), call.getInput(), clock());
    if (review.complete()) throw new Blocked("ziwei_interaction_mismatch");
    return new Pending(
        "input",
        call.getId(),
        call.getName(),
        ResumeInputs.actionHash(call),
        ResumeInputs.canonical(ZiweiInputs.answerSchema(call.getName())),
        review.question());
  }

  public Mono<ToolResultBlock> run(ToolCallParam param) {
    return Mono.fromCallable(
        () -> {
          authorize();
          String tool = param.getToolUseBlock().getName();
          byte[] key = ZiweiPolicy.key(tasks.releases().credential(policy.keyRef()));
          try {
            String binding =
                ZiweiInputs.hmac(
                    key, List.of("ziwei-key-binding", policy.keyVersion(), claim.turn().id()));
            new CapabilitySteps(tasks, claim, "ziwei:key")
                .run("binding", binding, () -> Map.of("bound", true));
            var review = ZiweiInputs.inspect(tool, param.getInput(), clock());
            if (!review.complete()) return ToolResultBlock.suspended(param.getToolUseBlock());
            var input = review.value();
            if (!policy.tzdb().equals(ZiweiDates.tzdbVersion(input.timezone())))
              throw new Blocked("ziwei_tzdb_changed");
            String fingerprint =
                input.fingerprint(
                    new Owner(claim.turn().tenant(), claim.turn().subject()), policy, key);
            Object receipt =
                new CapabilitySteps(tasks, claim, "ziwei:" + param.getToolUseBlock().getId())
                    .run(
                        "result",
                        List.of(tool, param.getInput(), fingerprint),
                        () -> {
                          var result =
                              ZiweiCalculation.calculate(
                                  input, ZiweiDates.Level.forTool(tool), fingerprint, policy);
                          var response = new TreeMap<String, Object>();
                          response.put("status", "success");
                          response.put("mode", input.mode());
                          response.put("projection", result.projection());
                          response.put(
                              "time_context",
                              Map.of(
                                  "request_clock",
                                  clock().requestClock().toString(),
                                  "timezone",
                                  policy.queryTimezone()));
                          response.put(
                              "artifact",
                              Map.of(
                                  "artifact_id",
                                  "0".repeat(36),
                                  "hash",
                                  "0".repeat(64),
                                  "bytes",
                                  2097152));
                          if (ZiweiCalculation.bytes(response) > policy.projectionBytes() + 1024)
                            throw new Blocked("ziwei_envelope_budget_exceeded");
                          var artifact = governance.archive(claim, result.artifact(), "ziwei:read");
                          response.put(
                              "artifact",
                              Map.of(
                                  "artifact_id",
                                  artifact.id(),
                                  "hash",
                                  artifact.hash(),
                                  "bytes",
                                  artifact.bytes()));
                          return response;
                        });
            return ToolResultBlock.text(ResumeInputs.canonical(receipt));
          } catch (ZiweiDates.InputError error) {
            // Domain errors are safe, actionable model feedback; never expose submitted values.
            return ToolResultBlock.text(
                ResumeInputs.canonical(
                    Map.of(
                        "status",
                        "rejected",
                        "code",
                        error.code(),
                        "field",
                        error.field(),
                        "message",
                        "请缩小查询范围或确认日期；未生成成功盘面。")));
          } finally {
            Arrays.fill(key, (byte) 0);
          }
        });
  }
}
