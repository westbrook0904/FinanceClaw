package com.financeclaw.channel;

import com.financeclaw.app.execution.port.ExecutionContextGateway;
import com.financeclaw.app.task.TaskQueryService;
import com.financeclaw.app.task.WorkSignals;
import com.financeclaw.domain.Feishu.*;
import com.financeclaw.domain.Governance.Interaction;
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
 * <p>提交后只推进本地通知代次，使下一调度周期绕过退避；代次不承载内容、权限或发送资格。低频扫描处理重启、 跨实例提交和信号丢失，恢复扫描独立运行。超时或缺少可靠回执不能推断为未发送，
 * unknown/submitted_unverified 需要明确核对，不能自动换新身份重投。
 */
@Component
public class FeishuDelivery {
    private static final int MAX_CLAIM_CANDIDATES = 20;
    private final FeishuMapper db;
    private final TaskQueryService tasks;
    private final ExecutionContextGateway interactions;
    private final TransactionTemplate tx;
    private final AgentScopeFeishu channel;
    private final boolean enabled;
    private final IdlePoll idle;
    private final WorkSignals signals;
    private final AtomicBoolean polling = new AtomicBoolean();
    private final AtomicBoolean recovering = new AtomicBoolean();

    @Autowired
    public FeishuDelivery(
            FeishuMapper db,
            TaskQueryService tasks,
            ExecutionContextGateway interactions,
            TransactionTemplate tx,
            AgentScopeFeishu channel,
            WorkSignals signals,
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
                signals,
                settings.enabled && integrations,
                new IdlePoll(pollMillis, idleMaxMillis));
    }

    public FeishuDelivery(
            FeishuMapper db,
            TaskQueryService tasks,
            ExecutionContextGateway interactions,
            TransactionTemplate tx,
            AgentScopeFeishu channel,
            WorkSignals signals,
            FeishuSettings settings,
            boolean integrations) {
        this(
                db,
                tasks,
                interactions,
                tx,
                channel,
                signals,
                settings.enabled && integrations,
                new IdlePoll(1000, 5000));
    }

    FeishuDelivery(
            FeishuMapper db,
            TaskQueryService tasks,
            ExecutionContextGateway interactions,
            TransactionTemplate tx,
            AgentScopeFeishu channel,
            WorkSignals signals,
            boolean enabled,
            IdlePoll idle) {
        this.db = db;
        this.tasks = tasks;
        this.interactions = interactions;
        this.tx = tx;
        this.channel = channel;
        this.signals = Objects.requireNonNull(signals);
        this.enabled = enabled;
        this.idle = Objects.requireNonNull(idle);
    }

    /** 轮询可用工作；原子标记只防本进程重入，跨进程排他仍由数据库锁负责。 */
    @Scheduled(fixedDelayString = "${financeclaw.feishu.poll-ms:1000}")
    public void scheduled() {
        if (!enabled || !polling.compareAndSet(false, true)) {
            return;
        }
        try {
            var ticket = idle.acquire(signals.generation(WorkSignals.Queue.NOTICE));
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

    /** 恢复独立于通知退避，只标记失联发送的不确定性，不把它重新置为 pending。 */
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

    /** 显式维护入口绕过空闲退避；正常定时调用使用 scheduled，避免每个 tick 都扫描数据库。 */
    public void poll() {
        recover();
        pollWork();
    }

    /** 区分真正空队列与已经抑制/被其他执行者领取的候选，不能把旧通知积压当作空闲。 */
    private enum ClaimState {
        EMPTY,
        SKIPPED,
        CLAIMED
    }

    /** 路由来自领取事务中已加锁的 binding；它不是可跨领取复用的授权缓存。 */
    private record ClaimResult(ClaimState state, Notice notice, String chatId) {}

    private boolean pollWork() {
        var dirty = db.listBindingIdsRequiringProjection();
        for (String binding : dirty) {
            project(binding);
        }
        boolean worked = !dirty.isEmpty();
        // 有界跳过已失效候选，让后续有效通知能继续前进；每轮仍最多一次外部 SDK 发送。
        for (int checked = 0; checked < MAX_CLAIM_CANDIDATES; checked++) {
            var claimed = claimForDelivery();
            if (claimed.state() == ClaimState.EMPTY) {
                return worked;
            }
            worked = true;
            if (claimed.state() == ClaimState.SKIPPED) {
                continue;
            }
            var job = claimed.notice();
            // 领取已经提交，网络在事务外调用。此处只复用同次领取时的 chat 身份，
            // 不增加“发送前撤回”保证；后续绑定修改仍不能追回已经授予的外部发送资格。
            try {
                channel.send(claimed.chatId(), job.payload());
                db.updateNoticeOutcome(job.id(), "submitted_unverified", "native_sdk_no_receipt");
            } catch (RuntimeException failure) {
                db.updateNoticeOutcome(job.id(), "unknown", "native_send_unconfirmed");
            }
            return true;
        }
        return worked;
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
                    // 只有 waiting 文案使用交互；完成、失败、取消和阻断直接使用结果或固定提示。
                    List<Interaction> pending =
                            turn.status().equals("waiting")
                                    ? interactions.interactions(turn.id())
                                    : List.of();
                    String text = FeishuText.render(b, turn, pending);
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
                    signals.afterCommit(WorkSignals.Queue.NOTICE);
                });
    }

    /** 单次维护领取入口：保留遇到旧通知时抑制并返回 null 的原语义。 */
    public Notice claim() {
        return claimForDelivery().notice();
    }

    /** 领取通知前锁 binding 再锁 notice，并重新检查 owner、当前任务与两个 revision。 */
    private ClaimResult claimForDelivery() {
        var pending = db.getNextPendingNotice();
        if (pending == null) {
            return new ClaimResult(ClaimState.EMPTY, null, null);
        }
        return tx.execute(
                s -> {
                    var b = db.getBindingForUpdate(pending.bindingId());
                    var notice = db.getNoticeForUpdate(pending.id());
                    if (!notice.status().equals("pending")) {
                        return new ClaimResult(ClaimState.SKIPPED, null, null);
                    }
                    if (b.muted()
                            || !Objects.equals(b.activeTurn(), notice.turnId())
                            || b.revision() != notice.bindingRevision()) {
                        db.updateNoticeOutcome(notice.id(), "suppressed", "binding_changed");
                        return new ClaimResult(ClaimState.SKIPPED, null, null);
                    }
                    var turn =
                            tasks.get(
                                    new Owner(b.tenant(), b.subject()),
                                    b.conversationId(),
                                    notice.turnId());
                    if (turn.revision() != notice.revision()) {
                        db.updateNoticeOutcome(notice.id(), "suppressed", "revision_changed");
                        return new ClaimResult(ClaimState.SKIPPED, null, null);
                    }
                    return db.updatePendingNoticeSending(notice.id()) == 1
                            ? new ClaimResult(ClaimState.CLAIMED, notice, b.chatId())
                            : new ClaimResult(ClaimState.SKIPPED, null, null);
                });
    }

    public List<Notice> list(Owner owner) {
        return db.listNoticesByOwner(owner);
    }
}
