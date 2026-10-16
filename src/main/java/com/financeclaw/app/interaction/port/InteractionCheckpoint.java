package com.financeclaw.app.interaction.port;

import com.financeclaw.domain.InteractionBinding;
import com.financeclaw.domain.InteractionBinding.Checkpoint;

public interface InteractionCheckpoint {
  Checkpoint pending(InteractionBinding binding);
}
