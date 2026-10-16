package com.financeclaw.infrastructure.interaction;

import com.financeclaw.domain.InteractionBinding;
import com.financeclaw.domain.InteractionBinding.Checkpoint;

/** Business semantics beyond the SDK's schema validation, without executing the suspended tool. */
public interface BusinessAnswerValidator {
  boolean supports(String tool);

  void validate(InteractionBinding binding, Checkpoint checkpoint, Object answer);
}
