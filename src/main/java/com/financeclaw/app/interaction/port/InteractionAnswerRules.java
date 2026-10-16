package com.financeclaw.app.interaction.port;

import com.financeclaw.domain.Governance.Answer;
import com.financeclaw.domain.InteractionBinding;
import com.financeclaw.domain.InteractionBinding.Checkpoint;

public interface InteractionAnswerRules {
  void validate(InteractionBinding binding, Checkpoint checkpoint, Answer answer);
}
