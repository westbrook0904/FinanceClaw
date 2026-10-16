package com.financeclaw.infrastructure.agentscope.capability;

import com.financeclaw.application.*;
import com.financeclaw.domain.Governance.*;
import com.financeclaw.domain.Task.Claim;
import com.financeclaw.infrastructure.agentscope.ResumeInputs;
import com.financeclaw.tools.ZiweiTools;
import com.financeclaw.ziwei.*;
import io.agentscope.core.message.*;
import io.agentscope.core.tool.ToolCallParam;
import java.time.Duration;

public final class ZiweiToolContribution implements ToolContribution, InteractionHandler {
  private final TaskService tasks;
  private final Claim claim;
  private final ZiweiTools tools;

  public ZiweiToolContribution(TaskService tasks, GovernanceService governance, Claim claim) {
    this.tasks = tasks;
    this.claim = claim;
    this.tools = new ZiweiTools(tasks, governance, claim);
  }

  public void contribute(Registrar registrar) {
    for (var level : ZiweiDates.Level.values()) {
      var name = level.toolName();
      registrar.tool(
          name,
          ZiweiInputs.description(name),
          ZiweiInputs.schema(name),
          "ziwei:read",
          tools::run,
          this);
    }
  }

  public Pending pending(ToolUseBlock call) {
    return tools.pending(call);
  }

  public Msg resume(ToolUseBlock call, Object answer) {
    var corrected = ZiweiInputs.corrected(call.getName(), call.getInput(), answer);
    if (!ZiweiInputs.inspect(call.getName(), corrected, tools.clock()).complete())
      throw new Blocked("ziwei_input_incomplete");
    tasks.consume(claim, false);
    var result =
        tools
            .run(ToolCallParam.builder().toolUseBlock(call).input(corrected).build())
            .block(Duration.ofSeconds(20));
    return ResumeInputs.result(call, result);
  }
}
