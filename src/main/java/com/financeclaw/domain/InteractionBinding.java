package com.financeclaw.domain;

import com.financeclaw.domain.Governance.*;
import com.financeclaw.domain.Task.*;
import java.util.*;

/** The saved question binds an answer to one attempt, policy revision and exact action. */
public record InteractionBinding(Claim claim, Interaction interaction) {
  public record Checkpoint(
      String callId, String tool, String actionHash, boolean asking, Map<String, Object> input) {
    public Checkpoint {
      input = Collections.unmodifiableMap(new LinkedHashMap<>(input));
    }
  }

  public AnswerReceipt replay(String responseHash) {
    if (!interaction.status().equals("answered")) return null;
    if (!responseHash.equals(interaction.responseHash()))
      throw new Problem(409, "idempotency_conflict");
    return receipt(true);
  }

  public void requirePending() {
    if (!claim.turn().status().equals("waiting")
        || !interaction.status().equals("pending")
        || !interaction.live()
        || !interaction.attemptId().equals(claim.command().attemptId())
        || !interaction.releaseHash().equals(claim.turn().releaseHash()))
      throw new Problem(409, "stale_interaction");
  }

  public void requireGrant(long revision) {
    if (interaction.grantRevision() != revision) throw new Problem(409, "authorization_changed");
  }

  public void requireAnswer(Answer answer) {
    if (!interaction.kind().equals(answer.kind())
        || interaction.revision() != answer.revision()
        || (interaction.kind().equals("approval")
            && !interaction.actionHash().equals(answer.actionHash())))
      throw new Problem(409, "interaction_mismatch");
  }

  public void requireCheckpoint(Checkpoint saved) {
    if (!interaction.callId().equals(saved.callId())
        || !interaction.tool().equals(saved.tool())
        || !interaction.actionHash().equals(saved.actionHash())
        || (interaction.kind().equals("approval") != saved.asking()))
      throw new Problem(409, "checkpoint_mismatch");
  }

  public void requireContent(Answer answer) {
    if (interaction.kind().equals("approval")) {
      if (!Set.of("approve", "reject").contains(Objects.toString(answer.decision(), ""))
          || answer.answer() != null) throw new Problem(422, "invalid_decision");
    } else if (answer.decision() != null || answer.actionHash() != null)
      throw new Problem(422, "invalid_answer");
  }

  public AnswerReceipt receipt(boolean replay) {
    return new AnswerReceipt(
        interaction.id(), interaction.turnId(), "accepted", interaction.revision(), replay);
  }
}
