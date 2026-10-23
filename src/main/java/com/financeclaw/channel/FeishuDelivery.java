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
 * 持久通知投递协调器：领取通知后调用原生 SDK，并记录可证明的提交结果。
 *
 * <p>轮询通过空闲退避减少数据库扫描；恢复扫描独立运行。超时或缺少可靠回执不能推断为未发送， unknown/submitted_unverified 需要明确核对，不能自动换新身份重投。
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
    /** 轮询可用工作；原子标记只防本进程重入，跨进程排他仍由数据库锁负责。 */
    public void scheduled() {
        if (!enabled || !polling.compareAndSet(false, true)) {
            return;
        }
        try {
            var ticket = idle.acquire(0);
            if (ticket == null) {
                return;
            }
            try {
                if (pollWork()) {
                    idle.worked();
                } else {
                    idle.empty(ticket);
                }
            } catch (RuntimeException failure) {
                idle.empty(ticket);
                throw failure;
            }
        } finally {
            polling.set(false);
        }
    }

    /**
     * Recovery remains independent of notification backoff and never requeues an uncertain send.
     */
    @Scheduled(fixedDelayString = "${financeclaw.feishu.recovery-ms:5000}")
    public void scheduledRecovery() {
        if (enabled) {
            recover();
        }
    }

    /** 将失去执行者的 sending 通知交由持久恢复规则处理，未知送达不能重新变成待发送。 */
    private void recover() {
        if (!recovering.compareAndSet(false, true)) {
            return;
        }
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
        for (String binding : dirty) {
            project(binding);
        }
        var job = claim();
        if (job == null) {
            return !dirty.isEmpty();
        }
        var binding = tx.execute(s -> db.getBindingForUpdate(job.bindingId()));
        // 网络调用在领取事务之外执行；发送成功但落库失败也只能记为未知，不能直接重试。
        try {
            channel.send(binding.chatId(), job.payload());
            db.updateNoticeOutcome(job.id(), "submitted_unverified", "native_sdk_no_receipt");
        } catch (RuntimeException e) {
            db.updateNoticeOutcome(job.id(), "unknown", "native_send_unconfirmed");
        }
        return true;
    }

    /** 锁定当前绑定并按任务修订号生成通知，抑制旧版本回答被投递到新的任务。 */
    public void project(String id) {
        tx.executeWithoutResult(
                s -> {
                    var b = db.getBindingForUpdate(id);
                    if (b == null || b.muted() || b.activeTurn() == null) {
                        return;
                    }
                    Owner owner = new Owner(b.tenant(), b.subject());
                    var turn = tasks.get(owner, b.conversationId(), b.activeTurn());
                    if (!Set.of("waiting", "blocked", "completed", "failed", "cancelled")
                            .contains(turn.status())) {
                        return;
                    }
                    String text = FeishuText.render(b, turn, interactions.interactions(turn.id()));
                    if (text.isBlank()) {
                        return;
                    }
                    String operation =
                            UUID.nameUUIDFromBytes(
                                            (turn.id() + ":" + turn.revision())
                                                    .getBytes(StandardCharsets.UTF_8))
                                    .toString();
                    db.insertOrReactivateNotice(
                            new Notice(
                                    operation,
                                    turn.id(),
                                    id,
                                    b.revision(),
                                    turn.revision(),
                                    text,
                                    "pending",
                                    null));
                });
    }

    /** 领取通知前再次核对绑定版本和当前任务；不再有效的通知直接抑制。 */
    public Notice claim() {
        var pending = db.getNextPendingNotice();
        if (pending == null) {
            return null;
        }
        return tx.execute(
                s -> {
                    var b = db.getBindingForUpdate(pending.bindingId());
                    var notice = db.getNoticeForUpdate(pending.id());
                    if (!notice.status().equals("pending")) {
                        return null;
                    }
                    if (b.muted()
                            || !Objects.equals(b.activeTurn(), notice.turnId())
                            || b.revision() != notice.bindingRevision()) {
                        db.updateNoticeOutcome(notice.id(), "suppressed", "binding_changed");
                        return null;
                    }
                    var turn =
                            tasks.get(
                                    new Owner(b.tenant(), b.subject()),
                                    b.conversationId(),
                                    notice.turnId());
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
