package com.financeclaw.infrastructure.interaction;

import com.financeclaw.app.interaction.port.InteractionAnswerRules;
import com.financeclaw.domain.Governance.Answer;
import com.financeclaw.domain.InteractionBinding;
import com.financeclaw.domain.InteractionBinding.Checkpoint;
import com.financeclaw.domain.Task.Problem;
import com.financeclaw.support.PayloadJson;
import io.agentscope.core.tool.ToolValidator;
import java.util.List;
import org.springframework.stereotype.Component;

@Component
public class AgentScopeAnswerRules implements InteractionAnswerRules {
  private final List<BusinessAnswerValidator> validators;

  public AgentScopeAnswerRules(List<BusinessAnswerValidator> validators) {
    this.validators = List.copyOf(validators);
  }

  public void validate(InteractionBinding binding, Checkpoint checkpoint, Answer answer) {
    if (ToolValidator.validateInput(
            PayloadJson.canonical(answer.answer()),
            PayloadJson.object(binding.interaction().schema()))
        != null) throw new Problem(422, "invalid_answer");
    for (var validator : validators)
      if (validator.supports(binding.interaction().tool()))
        validator.validate(binding, checkpoint, answer.answer());
  }
}
