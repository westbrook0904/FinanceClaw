package com.financeclaw.infrastructure.interaction;

import com.financeclaw.app.ziwei.ZiweiInputService;
import com.financeclaw.domain.InteractionBinding;
import com.financeclaw.domain.InteractionBinding.Checkpoint;
import com.financeclaw.infrastructure.ziwei.ZiweiSchemas;
import org.springframework.stereotype.Component;

@Component
public class ZiweiAnswerValidator implements BusinessAnswerValidator {
  private final ZiweiInputService inputs;

  public ZiweiAnswerValidator(ZiweiInputService inputs) {
    this.inputs = inputs;
  }

  public boolean supports(String tool) {
    return ZiweiSchemas.supports(tool);
  }

  public void validate(InteractionBinding binding, Checkpoint checkpoint, Object answer) {
    inputs.validateAnswer(binding.claim(), checkpoint.tool(), checkpoint.input(), answer);
  }
}
