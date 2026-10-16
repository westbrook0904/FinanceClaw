package com.financeclaw.infrastructure.agentscope.capability;

import com.financeclaw.domain.Governance.*;
import com.financeclaw.infrastructure.agentscope.ResumeInputs;
import io.agentscope.core.message.*;
import java.util.function.Function;

public record ApprovalInteractionHandler(Function<ToolUseBlock, String> question)
    implements InteractionHandler {
  public boolean approval() {
    return true;
  }

  public Pending pending(ToolUseBlock call) {
    if (call.getState() != ToolCallState.ASKING) throw new Blocked("unsupported_interaction");
    return new Pending(
        "approval",
        call.getId(),
        call.getName(),
        ResumeInputs.actionHash(call),
        "{}",
        question.apply(call));
  }
}
