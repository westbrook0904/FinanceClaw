package com.financeclaw.channel;

import com.financeclaw.application.*;
import com.financeclaw.domain.Feishu.*;
import com.financeclaw.domain.Task.Owner;
import com.financeclaw.infrastructure.channel.FeishuMapper;
import java.nio.charset.StandardCharsets;
import java.util.*;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.scheduling.annotation.Scheduled;
import org.springframework.stereotype.Component;
import org.springframework.transaction.support.TransactionTemplate;

/**
 * Durable admission around native SDK delivery. SDK completion is never promoted to confirmed
 * delivery.
 */
@Component
public class FeishuDelivery {
  private final FeishuMapper db;
  private final TaskService tasks;
  private final GovernanceService governance;
  private final TransactionTemplate tx;
  private final AgentScopeFeishu channel;
  private final boolean enabled;

  public FeishuDelivery(
      FeishuMapper db,
      TaskService tasks,
      GovernanceService governance,
      TransactionTemplate tx,
      AgentScopeFeishu channel,
      FeishuSettings settings,
      @Value("${financeclaw.integrations.enabled:true}") boolean integrations) {
    this.db = db;
    this.tasks = tasks;
    this.governance = governance;
    this.tx = tx;
    this.channel = channel;
    enabled = settings.enabled && integrations;
  }

  @Scheduled(fixedDelayString = "${financeclaw.feishu.poll-ms:1000}")
  public void scheduled() {
    if (enabled) poll();
  }

  public void poll() {
    db.recover();
    for (String binding : db.dirty()) project(binding);
    var job = claim();
    if (job == null) return;
    var binding = tx.execute(s -> db.lock(job.bindingId()));
    try {
      channel.send(binding.chatId(), job.payload());
      db.outcome(job.id(), "submitted_unverified", "native_sdk_no_receipt");
    } catch (RuntimeException e) {
      db.outcome(job.id(), "unknown", "native_send_unconfirmed");
    }
  }

  public void project(String id) {
    tx.executeWithoutResult(
        s -> {
          var b = db.lock(id);
          if (b == null || b.muted() || b.activeTurn() == null) return;
          Owner owner = new Owner(b.tenant(), b.subject());
          var turn = tasks.get(owner, b.conversationId(), b.activeTurn());
          if (!Set.of("waiting", "blocked", "completed", "failed", "cancelled")
              .contains(turn.status())) return;
          String text =
              FeishuText.render(
                  b, turn, governance.interactions(owner, b.conversationId(), turn.id()));
          if (text.isBlank()) return;
          String operation =
              UUID.nameUUIDFromBytes(
                      (turn.id() + ":" + turn.revision()).getBytes(StandardCharsets.UTF_8))
                  .toString();
          db.insertNotice(
              new Notice(
                  operation, turn.id(), id, b.revision(), turn.revision(), text, "pending", null));
        });
  }

  public Notice claim() {
    var pending = db.pending();
    if (pending == null) return null;
    return tx.execute(
        s -> {
          var b = db.lock(pending.bindingId());
          var notice = db.notice(pending.id());
          if (!notice.status().equals("pending")) return null;
          if (b.muted()
              || !Objects.equals(b.activeTurn(), notice.turnId())
              || b.revision() != notice.bindingRevision()) {
            db.outcome(notice.id(), "suppressed", "binding_changed");
            return null;
          }
          var turn =
              tasks.get(new Owner(b.tenant(), b.subject()), b.conversationId(), notice.turnId());
          if (turn.revision() != notice.revision()) {
            db.outcome(notice.id(), "suppressed", "revision_changed");
            return null;
          }
          return db.sending(notice.id()) == 1 ? notice : null;
        });
  }

  public List<Notice> list(Owner owner) {
    return db.notices(owner);
  }
}
