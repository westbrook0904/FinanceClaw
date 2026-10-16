package com.financeclaw.infrastructure.agentscope;

import com.financeclaw.app.interaction.port.InteractionCheckpoint;
import com.financeclaw.domain.InteractionBinding;
import com.financeclaw.domain.InteractionBinding.Checkpoint;
import com.financeclaw.domain.Task.Problem;
import com.financeclaw.infrastructure.state.MyBatisAgentStateStore;
import com.financeclaw.infrastructure.state.RuntimeMapper;
import io.agentscope.core.message.ToolCallState;
import org.springframework.stereotype.Repository;

@Repository
public class AgentScopeInteractionCheckpoint implements InteractionCheckpoint {
  private final RuntimeMapper states;

  public AgentScopeInteractionCheckpoint(RuntimeMapper states) {
    this.states = states;
  }

  public Checkpoint pending(InteractionBinding binding) {
    var scope = binding.claim().scope();
    var interaction = binding.interaction();
    try {
      var call =
          ResumeInputs.pending(
              new MyBatisAgentStateStore(states, scope),
              scope,
              interaction.stateVersion(),
              interaction.callId());
      return new Checkpoint(
          call.getId(),
          call.getName(),
          ResumeInputs.actionHash(call),
          call.getState() == ToolCallState.ASKING,
          call.getInput());
    } catch (IllegalArgumentException error) {
      throw new Problem(409, "checkpoint_mismatch");
    }
  }
}
