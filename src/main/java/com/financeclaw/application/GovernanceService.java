package com.financeclaw.application;

import com.fasterxml.jackson.databind.ObjectMapper;
import com.financeclaw.app.execution.ExecutionLeaseService;
import com.financeclaw.app.execution.ExecutionPolicyService;
import com.financeclaw.app.interaction.AnswerInteractionService;
import com.financeclaw.app.release.ReleaseService;
import com.financeclaw.app.task.TaskQueryService;
import com.financeclaw.domain.ExecutionScope;
import com.financeclaw.domain.Governance.*;
import com.financeclaw.domain.Task.*;
import com.financeclaw.domain.release.ReleaseDefinition;
import com.financeclaw.infrastructure.governance.GovernanceMapper;
import com.financeclaw.infrastructure.task.TaskMapper;
import com.financeclaw.support.PayloadDigest;
import com.financeclaw.support.PayloadJson;
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
  private final TaskQueryService queries;
  private final ExecutionLeaseService leases;
  private final ExecutionPolicyService policy;
  private final ReleaseService releases;
  private final TaskMapper turns;
  private final GovernanceMapper db;
  private final AnswerInteractionService answers;
  private final TransactionTemplate tx;

  public GovernanceService(
      TaskQueryService queries,
      ExecutionLeaseService leases,
      ExecutionPolicyService policy,
      ReleaseService releases,
      TaskMapper turns,
      GovernanceMapper db,
      AnswerInteractionService answers,
      TransactionTemplate tx) {
    this.queries = queries;
    this.leases = leases;
    this.policy = policy;
    this.releases = releases;
    this.turns = turns;
    this.db = db;
    this.answers = answers;
    this.tx = tx;
  }

  public Grant grant(Owner owner, String conversation) {
    queries.owned(owner, conversation);
    return db.grant(conversation);
  }

  public Grant changeGrant(
      Owner owner, String conversation, String key, long revision, List<String> scopes) {
    new com.financeclaw.domain.IdempotencyKey(key);
    queries.owned(owner, conversation);
    if (scopes == null || !ReleaseDefinition.SCOPES.containsAll(scopes))
      throw new Problem(422, "invalid_scopes");
    String json = PayloadJson.canonical(scopes.stream().distinct().sorted().toList());
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
    return answers.interactions(owner, conversation, turn);
  }

  public Interaction ownedInteraction(Owner owner, String id) {
    return answers.ownedInteraction(owner, id);
  }

  public AnswerReceipt answer(Owner owner, String id, String key, Answer answer) {
    return answers.answer(owner, id, key, answer);
  }

  public AnswerReceipt answer(
      Owner owner, String conversation, String turn, String id, String key, Answer answer) {
    return answers.answer(owner, conversation, turn, id, key, answer);
  }

  public Artifact archive(Claim claim, Object payload) {
    String json = PayloadJson.canonical(payload);
    int size = json.getBytes(StandardCharsets.UTF_8).length;
    if (size > 2097152) throw new Blocked("artifact_too_large");
    return leases.fenced(
        claim,
        () -> {
          policy.checkPolicy(claim);
          var t = claim.turn();
          var a =
              new Artifact(
                  UUID.randomUUID().toString(),
                  t.id(),
                  t.conversationId(),
                  t.tenant(),
                  t.subject(),
                  t.releaseHash(),
                  PayloadDigest.sha256(json),
                  json,
                  size);
          db.insertArtifact(a);
          return a;
        });
  }

  public Page read(Owner owner, String id, int start, int limit) {
    return read(owner, id, start, limit, "");
  }

  public Artifact archive(Claim claim, Object payload, String requiredScope) {
    return leases.fenced(
        claim,
        () -> {
          policy.checkPolicy(claim);
          if (!PayloadJson.scopes(db.policy(claim.turn().id()).scopes()).contains(requiredScope))
            throw new Blocked("artifact_scope_denied");
          var artifact = archive(claim, payload);
          db.setArtifactScope(artifact.id(), requiredScope);
          return artifact;
        });
  }

  public Page read(Owner owner, String id, int start, int limit, String path) {
    return tx.execute(
        s -> {
          var a = db.artifact(id);
          if (a == null
              || !a.tenant().equals(owner.tenant())
              || !a.subject().equals(owner.subject())) throw new Problem(404, "not_found");
          // Match execution's owner-before-grant lock order when privacy governance is enabled.
          if ("ziwei:read".equals(db.artifactScope(id)))
            policy.checkArtifactPrivacy(turns.turn(a.turnId()));
          var grant = db.lockGrant(a.conversationId());
          if (grant == null || !PayloadJson.scopes(grant.scopes()).contains("artifact:read"))
            throw new Problem(403, "artifact_denied");
          if (!PayloadJson.scopes(grant.scopes()).contains(db.artifactScope(id)))
            throw new Problem(403, "artifact_scope_denied");
          try {
            releases.require(a.releaseHash());
          } catch (Blocked e) {
            throw new Problem(409, e.getMessage());
          }
          if (!a.hash().equals(PayloadDigest.sha256(a.payload())))
            throw new Problem(409, "artifact_corrupt");
          Object data = PayloadJson.parse(a.payload());
          if (path == null || path.length() > 256 || (!path.isEmpty() && !path.startsWith("/")))
            throw new Problem(422, "invalid_artifact_path");
          if (!path.isEmpty()) {
            try {
              var selected = JSON.valueToTree(data).at(path);
              if (selected.isMissingNode()) throw new Problem(422, "artifact_path_missing");
              data = JSON.convertValue(selected, Object.class);
            } catch (IllegalArgumentException e) {
              throw new Problem(422, "invalid_artifact_path");
            }
          }
          if (data instanceof List<?> values) {
            if (start < 0 || limit < 1 || limit > 100) throw new Problem(422, "invalid_pagination");
            int offset = Math.min(start, values.size()),
                end = Math.min(offset + limit, values.size());
            while (end > offset
                && PayloadJson.canonical(values.subList(offset, end))
                        .getBytes(StandardCharsets.UTF_8)
                        .length
                    > 16384) end--;
            if (end == offset && offset < values.size())
              throw new Problem(422, "artifact_item_too_large");
            return new Page(
                id, a.hash(), values.size(), offset, end - offset, values.subList(offset, end));
          }
          if (PayloadJson.canonical(data).getBytes(StandardCharsets.UTF_8).length > 16384)
            throw new Problem(422, "artifact_object_too_large");
          return new Page(id, a.hash(), 1, 0, 1, data);
        });
  }

  public String report(Claim claim, String call, String actionHash, String title) {
    return leases.fenced(
        claim,
        () -> {
          policy.checkPolicy(claim);
          var old = db.effect(claim.turn().id(), call);
          if (old != null) {
            if (!old.actionHash().equals(actionHash)) throw new Blocked("effect_conflict");
            if (!old.status().equals("completed")) throw new Blocked("reconciliation_required");
            return old.result();
          }
          db.beginEffect(claim.turn().id(), call, actionHash);
          String id = UUID.randomUUID().toString();
          db.report(id, claim.turn().id(), call, title);
          String result = PayloadJson.canonical(Map.of("report_id", id, "status", "created"));
          db.completeEffect(claim.turn().id(), call, result);
          return result;
        });
  }
}
