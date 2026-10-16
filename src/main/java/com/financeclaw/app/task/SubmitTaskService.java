package com.financeclaw.app.task;

import com.financeclaw.app.execution.port.*;
import com.financeclaw.app.task.port.*;
import com.financeclaw.domain.ExecutionScope;
import com.financeclaw.domain.Governance.*;
import com.financeclaw.domain.IdempotencyKey;
import com.financeclaw.domain.Task.*;
import com.financeclaw.support.PayloadJson;
import java.util.*;
import org.springframework.stereotype.Service;
import org.springframework.transaction.support.TransactionTemplate;

@Service
public class SubmitTaskService {
  private final TaskAdmissionGateway db;
  private final ExecutionPolicyGateway governance;
  private final ExecutionRelease releases;
  private final ExecutionMemory memory;
  private final TransactionTemplate tx;

  public SubmitTaskService(
      TaskAdmissionGateway db,
      ExecutionPolicyGateway governance,
      ExecutionRelease releases,
      ExecutionMemory memory,
      TransactionTemplate tx) {
    this.db = db;
    this.governance = governance;
    this.releases = releases;
    this.memory = memory;
    this.tx = tx;
  }

  private static String id() {
    return UUID.randomUUID().toString();
  }

  public String createConversation(Owner owner) {
    String id = id();
    tx.executeWithoutResult(
        s -> {
          db.createConversation(id, owner);
          governance.ensureGrant(
              id, PayloadJson.canonical(releases.require(releases.currentHash()).scopes()));
        });
    return id;
  }

  public Accepted submit(Owner owner, String conversation, String key, String message) {
    new IdempotencyKey(key);
    if (message == null || message.isBlank() || message.length() > 32000)
      throw new Problem(422, "invalid_message");
    String hash = ExecutionScope.hash(List.of(message));
    return tx.execute(
        s -> {
          if (db.lockConversation(conversation, owner) == null) throw new Problem(404, "not_found");
          Turn old = db.replay(conversation, key);
          if (old != null) {
            return old.replayAdmission(hash);
          }
          // Keep this shared row lock until admission commits. A pause waits for admitted writes,
          // while independent conversations can still submit concurrently. Replays stay available.
          db.ensureAdmission(owner);
          if (db.admissionPaused(owner)) throw new Problem(503, "admission_paused");
          if (db.active(conversation)) throw new Problem(409, "conversation_busy");
          Turn t =
              new Turn(
                  id(),
                  conversation,
                  owner.tenant(),
                  owner.subject(),
                  key,
                  hash,
                  message,
                  releases.currentHash(),
                  "queued",
                  1,
                  null,
                  null);
          db.insertTurn(t);
          memory.admit(t);
          var grant = governance.lockGrant(conversation);
          if (grant == null) throw new Problem(409, "grant_unavailable");
          governance.insertPolicy(
              t.id(),
              grant.revision(),
              PayloadJson.canonical(
                  PayloadJson.scopes(grant.scopes()).stream()
                      .filter(releases.require(t.releaseHash()).scopes()::contains)
                      .toList()));
          db.recordAdmission(t, id());
          return t.accepted(false);
        });
  }
}
