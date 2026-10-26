package com.financeclaw.app.interaction.port;

import com.financeclaw.domain.InteractionBinding;

/** Resolve a delegated question only through both its root and child native checkpoints. */
public interface ChildInteractionCheckpoint {
    InteractionBinding.Checkpoint checkpoint(InteractionBinding binding);
}
