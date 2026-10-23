package com.financeclaw.channel;

import com.financeclaw.app.execution.port.ExecutionContextGateway;
import com.financeclaw.app.task.TaskQueryService;
import com.financeclaw.domain.Feishu.*;
import com.financeclaw.domain.Task.Owner;
import com.financeclaw.infrastructure.channel.FeishuMapper;
import com.financeclaw.worker.IdlePoll;
import java.nio.charset.StandardCharsets;
import java.util.*;
import java.util.concurrent.atomic.AtomicBoolean;
import org.springframework.beans.factory.annotation.Autowired;
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
  private final TaskQueryService tasks;
  private final ExecutionContextGateway interactions;
  private final TransactionTemplate tx;
  private final AgentScopeFeishu channel;
  private final boolean enabled;
  private final IdlePoll idle;
  private final AtomicBoolean polling = new AtomicBoolean();
  private final AtomicBoolean recovering = new AtomicBoolean();

  @Autowired
  public FeishuDelivery(
      FeishuMapper db,
      TaskQueryService tasks,
      ExecutionContextGateway interactions,
      TransactionTemplate tx,
      AgentScopeFeishu channel,
      FeishuSettings settings,
      @Value("${financeclaw.integrations.enabled:true}") boolean integrations,
      @Value("${financeclaw.feishu.poll-ms:1000}") long pollMillis,
      @Value("${financeclaw.feishu.idle-max-ms:5000}") long idleMaxMillis) {
    this(
        db,
        tasks,
        interactions,
        tx,
        channel,
        settings.enabled && integrations,
        new IdlePoll(pollMillis, idleMaxMillis));
  }

  public FeishuDelivery(
      FeishuMapper db,
      TaskQueryService tasks,
      ExecutionContextGateway interactions,
      TransactionTemplate tx,
      AgentScopeFeishu channel,
      FeishuSettings settings,
      boolean integrations) {
    this(
        db,
        tasks,
        interactions,
        tx,
        channel,
        settings.enabled && integrations,
        new IdlePoll(1000, 5000));
  }

  FeishuDelivery(
      FeishuMapper db,
      TaskQueryService tasks,
      ExecutionContextGateway interactions,
      TransactionTemplate tx,
      AgentScopeFeishu channel,
      boolean enabled,
      IdlePoll idle) {
    this.db = db;
    this.tasks = tasks;
    this.interactions = interactions;
    this.tx = tx;
    this.channel = channel;
    this.enabled = enabled;
    this.idle = Objects.requireNonNull(idle);
  }

  @Scheduled(fixedDelayString = "${financeclaw.feishu.poll-ms:1000}")
  public void scheduled() {
    if (!enabled || !polling.compareAndSet(false, true)) return;
    try {
      var ticket = idle.acquire(0);
      if (ticket == null) return;
      try {
        if (pollWork()) idle.worked();
        else idle.empty(ticket);
      } catch (RuntimeException failure) {
        idle.empty(ticket);
        throw failure;
      }
    } finally {
      polling.set(false);
    }
  }

  /** Recovery remains independent of notification backoff and never requeues an uncertain send. */
  @Scheduled(fixedDelayString = "${financeclaw.feishu.recovery-ms:5000}")
  public void scheduledRecovery() {
    if (enabled) recover();
  }

  private void recover() {
    if (!recovering.compareAndSet(false, true)) return;
    try {
      db.updateExpiredSendingNoticesUnknown();
    } finally {
      recovering.set(false);
    }
  }

  /**
   * Explicit maintenance polling bypasses local backoff; normal scheduled ticks use scheduled().
   */
  public void poll() {
    recover();
    pollWork();
  }

  private boolean pollWork() {
    var dirty = db.listBindingIdsRequiringProjection();
    for (String binding : dirty) project(binding);
    var job = claim();
    if (job == null) return !dirty.isEmpty();
    var binding = tx.execute(s -> db.getBindingForUpdate(job.bindingId()));
    try {
      channel.send(binding.chatId(), job.payload());
      db.updateNoticeOutcome(job.id(), "submitted_unverified", "native_sdk_no_receipt");
    } catch (RuntimeException e) {
      db.updateNoticeOutcome(job.id(), "unknown", "native_send_unconfirmed");
    }
    return true;
  }

  public void project(String id) {
    tx.executeWithoutResult(
        s -> {
          var b = db.getBindingForUpdate(id);
          if (b == null || b.muted() || b.activeTurn() == null) return;
          Owner owner = new Owner(b.tenant(), b.subject());
          var turn = tasks.get(owner, b.conversationId(), b.activeTurn());
          if (!Set.of("waiting", "blocked", "completed", "failed", "cancelled")
              .contains(turn.status())) return;
          String text = FeishuText.render(b, turn, interactions.interactions(turn.id()));
          if (text.isBlank()) return;
          String operation =
              UUID.nameUUIDFromBytes(
                      (turn.id() + ":" + turn.revision()).getBytes(StandardCharsets.UTF_8))
                  .toString();
          db.insertOrReactivateNotice(
              new Notice(
                  operation, turn.id(), id, b.revision(), turn.revision(), text, "pending", null));
        });
  }

  public Notice claim() {
    var pending = db.getNextPendingNotice();
    if (pending == null) return null;
    return tx.execute(
        s -> {
          var b = db.getBindingForUpdate(pending.bindingId());
          var notice = db.getNoticeForUpdate(pending.id());
          if (!notice.status().equals("pending")) return null;
          if (b.muted()
              || !Objects.equals(b.activeTurn(), notice.turnId())
              || b.revision() != notice.bindingRevision()) {
            db.updateNoticeOutcome(notice.id(), "suppressed", "binding_changed");
            return null;
          }
          var turn =
              tasks.get(new Owner(b.tenant(), b.subject()), b.conversationId(), notice.turnId());
          if (turn.revision() != notice.revision()) {
            db.updateNoticeOutcome(notice.id(), "suppressed", "revision_changed");
            return null;
          }
          return db.updatePendingNoticeSending(notice.id()) == 1 ? notice : null;
        });
  }

  public List<Notice> list(Owner owner) {
    return db.listNoticesByOwner(owner);
  }
}
