package com.financeclaw.application;

import com.fasterxml.jackson.core.type.TypeReference;
import com.fasterxml.jackson.databind.ObjectMapper;
import com.financeclaw.agent.ResumeInputs;
import com.financeclaw.domain.ExecutionScope;
import com.financeclaw.domain.Governance.*;
import com.financeclaw.domain.Task.*;
import com.financeclaw.infrastructure.governance.GovernanceMapper;
import com.financeclaw.infrastructure.state.MyBatisAgentStateStore;
import com.financeclaw.infrastructure.state.RuntimeMapper;
import com.financeclaw.infrastructure.task.TaskMapper;
import io.agentscope.core.tool.ToolValidator;
import java.nio.charset.StandardCharsets;
import java.util.*;
import org.springframework.stereotype.Service;
import org.springframework.transaction.support.TransactionTemplate;

/**
 * Business authorization and durable interaction/artifact receipts around native SDK operations.
 */
@Service
public class GovernanceService {
  private static final ObjectMapper JSON = new ObjectMapper();
  private final TaskService tasks;
  private final TaskMapper turns;
  private final GovernanceMapper db;
  private final RuntimeMapper states;
  private final TransactionTemplate tx;

  public GovernanceService(
      TaskService tasks,
      TaskMapper turns,
      GovernanceMapper db,
      RuntimeMapper states,
      TransactionTemplate tx) {
    this.tasks = tasks;
    this.turns = turns;
    this.db = db;
    this.states = states;
    this.tx = tx;
  }

  public static String digest(String text) {
    try {
      return java.util.HexFormat.of()
          .formatHex(
              java.security.MessageDigest.getInstance("SHA-256")
                  .digest(text.getBytes(StandardCharsets.UTF_8)));
    } catch (java.security.NoSuchAlgorithmException e) {
      throw new IllegalStateException(e);
    }
  }

  public static Object parse(String json) {
    try {
      return JSON.readValue(json, Object.class);
    } catch (Exception e) {
      throw new Blocked("invalid_stored_json");
    }
  }

  public static Map<String, Object> object(String json) {
    try {
      return JSON.readValue(json, new TypeReference<Map<String, Object>>() {});
    } catch (Exception e) {
      throw new Blocked("invalid_stored_schema");
    }
  }

  public static List<String> scopes(String json) {
    try {
      return JSON.readValue(json, new TypeReference<List<String>>() {});
    } catch (Exception e) {
      throw new Blocked("invalid_stored_scopes");
    }
  }

  public Grant grant(Owner owner, String conversation) {
    tasks.owned(owner, conversation);
    return db.grant(conversation);
  }

  public Grant changeGrant(
      Owner owner, String conversation, String key, long revision, List<String> scopes) {
    TaskService.validateKey(key);
    tasks.owned(owner, conversation);
    if (scopes == null || !ReleaseCatalog.SCOPES.containsAll(scopes))
      throw new Problem(422, "invalid_scopes");
    String json = ResumeInputs.canonical(scopes.stream().distinct().sorted().toList());
    String hash = ExecutionScope.hash(List.of(Long.toString(revision), json));
    return tx.execute(
        s -> {
          var grant = db.lockGrant(conversation);
          String old = db.grantRequest(conversation, key);
          if (old != null) {
            if (!old.equals(hash)) throw new Problem(409, "idempotency_conflict");
            return db.grantReplay(conversation, key);
          }
          if (grant == null || grant.revision() != revision) throw new Problem(409, "stale_grant");
          db.updateGrant(conversation, json);
          db.grantChange(conversation, key, hash);
          return db.grant(conversation);
        });
  }

  public List<Interaction> interactions(Owner owner, String conversation, String turn) {
    tasks.get(owner, conversation, turn);
    return db.interactions(turn);
  }

  public Interaction ownedInteraction(Owner owner, String id) {
    var i = db.interaction(id);
    if (i == null) throw new Problem(404, "not_found");
    var t = turns.turn(i.turnId());
    tasks.get(owner, t.conversationId(), t.id());
    return i;
  }

  public AnswerReceipt answer(Owner owner, String id, String key, Answer answer) {
    var i = ownedInteraction(owner, id);
    var t = turns.turn(i.turnId());
    return answer(owner, t.conversationId(), t.id(), id, key, answer);
  }

  public AnswerReceipt answer(
      Owner owner, String conversation, String turn, String id, String key, Answer answer) {
    TaskService.validateKey(key);
    tasks.get(owner, conversation, turn);
    if (answer != null
        && (answer.revision() < 1 || (answer.reason() != null && answer.reason().length() > 2000)))
      throw new Problem(422, "invalid_answer");
    if (answer == null
        || ResumeInputs.canonical(answer).getBytes(StandardCharsets.UTF_8).length > 16384)
      throw new Problem(422, "invalid_answer");
    String hash = ExecutionScope.hash(List.of(ResumeInputs.canonical(answer)));
    return tx.execute(
        s -> {
          var command = turns.lockTurnCommand(turn);
          var t = turns.lockTurn(turn);
          var i = db.lockInteraction(id);
          if (i == null || !i.turnId().equals(turn)) throw new Problem(404, "not_found");
          if (i.status().equals("answered")) {
            if (!hash.equals(i.responseHash())) throw new Problem(409, "idempotency_conflict");
            return new AnswerReceipt(id, turn, "accepted", i.revision(), true);
          }
          if (!t.status().equals("waiting")
              || !i.status().equals("pending")
              || !i.live()
              || !i.attemptId().equals(command.attemptId())
              || !i.releaseHash().equals(t.releaseHash()))
            throw new Problem(409, "stale_interaction");
          var claim = new Claim(command, t);
          try {
            tasks.checkPolicy(claim);
          } catch (Blocked e) {
            throw new Problem(409, e.getMessage());
          }
          if (i.grantRevision() != db.grant(conversation).revision())
            throw new Problem(409, "authorization_changed");
          if (!i.kind().equals(answer.kind())
              || i.revision() != answer.revision()
              || (i.kind().equals("approval") && !i.actionHash().equals(answer.actionHash())))
            throw new Problem(409, "interaction_mismatch");
          try {
            var saved =
                ResumeInputs.pending(
                    new MyBatisAgentStateStore(states, claim.scope()),
                    claim.scope(),
                    i.stateVersion(),
                    i.callId());
            if (!ResumeInputs.actionHash(saved).equals(i.actionHash()))
              throw new IllegalArgumentException();
          } catch (IllegalArgumentException e) {
            throw new Problem(409, "checkpoint_mismatch");
          }
          if (i.kind().equals("approval")) {
            if (!Set.of("approve", "reject").contains(Objects.toString(answer.decision(), ""))
                || answer.answer() != null) throw new Problem(422, "invalid_decision");
          } else if (answer.decision() != null
              || answer.actionHash() != null
              || ToolValidator.validateInput(
                      ResumeInputs.canonical(answer.answer()), object(i.schema()))
                  != null) throw new Problem(422, "invalid_answer");
          db.answer(id, hash, ResumeInputs.canonical(answer));
          boolean complete =
              db.interactions(turn).stream()
                  .filter(x -> x.attemptId().equals(i.attemptId()))
                  .allMatch(x -> x.status().equals("answered"));
          if (complete) {
            if (db.resume(command.id(), id) != 1) throw new Problem(409, "stale_interaction");
            tasks.change(t, "resuming", null, null);
          }
          return new AnswerReceipt(id, turn, "accepted", i.revision(), false);
        });
  }

  public Artifact archive(Claim claim, Object payload) {
    String json = ResumeInputs.canonical(payload);
    int size = json.getBytes(StandardCharsets.UTF_8).length;
    if (size > 2097152) throw new Blocked("artifact_too_large");
    return tasks.fenced(
        claim,
        () -> {
          tasks.checkPolicy(claim);
          var t = claim.turn();
          var a =
              new Artifact(
                  UUID.randomUUID().toString(),
                  t.id(),
                  t.conversationId(),
                  t.tenant(),
                  t.subject(),
                  t.releaseHash(),
                  digest(json),
                  json,
                  size);
          db.insertArtifact(a);
          return a;
        });
  }

  public Page read(Owner owner, String id, int start, int limit) {
    return tx.execute(
        s -> {
          var a = db.artifact(id);
          if (a == null
              || !a.tenant().equals(owner.tenant())
              || !a.subject().equals(owner.subject())) throw new Problem(404, "not_found");
          var grant = db.lockGrant(a.conversationId());
          if (grant == null || !scopes(grant.scopes()).contains("artifact:read"))
            throw new Problem(403, "artifact_denied");
          try {
            tasks.releases().require(a.releaseHash());
          } catch (Blocked e) {
            throw new Problem(409, e.getMessage());
          }
          if (!a.hash().equals(digest(a.payload()))) throw new Problem(409, "artifact_corrupt");
          Object data = parse(a.payload());
          if (data instanceof List<?> values) {
            if (start < 0 || limit < 1 || limit > 100) throw new Problem(422, "invalid_pagination");
            int offset = Math.min(start, values.size()),
                end = Math.min(offset + limit, values.size());
            while (end > offset
                && ResumeInputs.canonical(values.subList(offset, end))
                        .getBytes(StandardCharsets.UTF_8)
                        .length
                    > 16384) end--;
            if (end == offset && offset < values.size())
              throw new Problem(422, "artifact_item_too_large");
            return new Page(
                id, a.hash(), values.size(), offset, end - offset, values.subList(offset, end));
          }
          if (a.bytes() > 16384) throw new Problem(422, "artifact_object_too_large");
          return new Page(id, a.hash(), 1, 0, 1, data);
        });
  }

  public String report(Claim claim, String call, String actionHash, String title) {
    return tasks.fenced(
        claim,
        () -> {
          tasks.checkPolicy(claim);
          var old = db.effect(claim.turn().id(), call);
          if (old != null) {
            if (!old.actionHash().equals(actionHash)) throw new Blocked("effect_conflict");
            if (!old.status().equals("completed")) throw new Blocked("reconciliation_required");
            return old.result();
          }
          db.beginEffect(claim.turn().id(), call, actionHash);
          String id = UUID.randomUUID().toString();
          db.report(id, claim.turn().id(), call, title);
          String result = ResumeInputs.canonical(Map.of("report_id", id, "status", "created"));
          db.completeEffect(claim.turn().id(), call, result);
          return result;
        });
  }
}
