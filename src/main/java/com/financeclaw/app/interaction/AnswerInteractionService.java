package com.financeclaw.app.interaction;

import com.financeclaw.app.interaction.port.*;
import com.financeclaw.domain.ExecutionScope;
import com.financeclaw.domain.Governance.*;
import com.financeclaw.domain.IdempotencyKey;
import com.financeclaw.domain.Task.*;
import com.financeclaw.support.PayloadJson;
import java.nio.charset.StandardCharsets;
import java.util.List;
import org.springframework.stereotype.Service;
import org.springframework.transaction.support.TransactionTemplate;

/** Accept an answer and enqueue its resume in the same short Spring transaction. */
@Service
public class AnswerInteractionService {
  private final InteractionGateway interactions;
  private final InteractionCheckpoint checkpoints;
  private final InteractionAnswerRules rules;
  private final TransactionTemplate tx;

  public AnswerInteractionService(
      InteractionGateway interactions,
      InteractionCheckpoint checkpoints,
      InteractionAnswerRules rules,
      TransactionTemplate tx) {
    this.interactions = interactions;
    this.checkpoints = checkpoints;
    this.rules = rules;
    this.tx = tx;
  }

  public Interaction ownedInteraction(Owner owner, String id) {
    var interaction = interactions.find(id);
    if (interaction == null) throw new Problem(404, "not_found");
    var turn = interactions.turn(interaction.turnId());
    interactions.ownedTurn(owner, turn.conversationId(), turn.id());
    return interaction;
  }

  public List<Interaction> interactions(Owner owner, String conversation, String turn) {
    interactions.ownedTurn(owner, conversation, turn);
    return interactions.interactions(turn);
  }

  public AnswerReceipt answer(Owner owner, String id, String key, Answer answer) {
    var interaction = ownedInteraction(owner, id);
    var turn = interactions.turn(interaction.turnId());
    return answer(owner, turn.conversationId(), turn.id(), id, key, answer);
  }

  public AnswerReceipt answer(
      Owner owner, String conversation, String turn, String id, String key, Answer answer) {
    new IdempotencyKey(key);
    interactions.ownedTurn(owner, conversation, turn);
    if (answer == null
        || answer.revision() < 1
        || (answer.reason() != null && answer.reason().length() > 2000))
      throw new Problem(422, "invalid_answer");
    String json = PayloadJson.canonical(answer);
    if (json.getBytes(StandardCharsets.UTF_8).length > 16384)
      throw new Problem(422, "invalid_answer");
    String hash = ExecutionScope.hash(List.of(json));
    return tx.execute(
        status -> {
          var binding = interactions.lock(turn, id);
          var replay = binding.replay(hash);
          if (replay != null) return replay;
          binding.requirePending();
          try {
            interactions.checkPolicy(binding.claim());
          } catch (Blocked error) {
            throw new Problem(409, error.getMessage());
          }
          binding.requireGrant(interactions.grantRevision(conversation));
          binding.requireAnswer(answer);
          var saved = checkpoints.pending(binding);
          binding.requireCheckpoint(saved);
          binding.requireContent(answer);
          if (!binding.interaction().kind().equals("approval"))
            rules.validate(binding, saved, answer);
          interactions.answer(id, hash, json);
          boolean complete =
              interactions.interactions(turn).stream()
                  .filter(i -> i.attemptId().equals(binding.interaction().attemptId()))
                  .allMatch(i -> i.status().equals("answered"));
          if (complete) interactions.resume(binding);
          return binding.receipt(false);
        });
  }
}
