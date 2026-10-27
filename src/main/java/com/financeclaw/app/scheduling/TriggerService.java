package com.financeclaw.app.scheduling;

import com.financeclaw.app.agent.AgentService;
import com.financeclaw.app.scheduling.port.TriggerCadence;
import com.financeclaw.app.scheduling.port.TriggerRepository;
import com.financeclaw.app.task.SubmitTaskService;
import com.financeclaw.domain.IdempotencyKey;
import com.financeclaw.domain.Task.Owner;
import com.financeclaw.domain.Task.Problem;
import com.financeclaw.domain.scheduling.TriggerFiring;
import com.financeclaw.domain.scheduling.TriggerRule;
import java.time.Duration;
import java.time.Instant;
import java.util.List;
import java.util.UUID;
import org.springframework.transaction.support.TransactionTemplate;

/** 将持久触发转换为正常任务受理；规则、Turn 与触发回执共享短事务，沿用原任务幂等和执行权威。 Cron 编译结果可复用，但到期时间、规则修订、目标发布与权限必须在本次受理中确定。 */
public final class TriggerService {
    private final TriggerRepository db;
    private final TriggerCadence cadence;
    private final SubmitTaskService submission;
    private final AgentService agents;
    private final TransactionTemplate tx;
    private final long graceSeconds;

    public TriggerService(
            TriggerRepository db,
            TriggerCadence cadence,
            SubmitTaskService submission,
            AgentService agents,
            TransactionTemplate tx,
            long graceSeconds) {
        if (graceSeconds < 1 || graceSeconds > 86400)
            throw new IllegalArgumentException("Invalid misfire grace");
        this.db = db;
        this.cadence = cadence;
        this.submission = submission;
        this.agents = agents;
        this.tx = tx;
        this.graceSeconds = graceSeconds;
    }

    /** 锁定已有规则后检查修订并解析可访问目标；失败不留下新规则或部分版本绑定。 */
    public TriggerRule put(
            Owner owner, String id, long expected, TriggerRule.Definition definition) {
        TriggerRule.requireId(id);
        definition.validate();
        if (expected < 0 || expected == Long.MAX_VALUE)
            throw new Problem(422, "invalid_schedule_revision");
        return tx.execute(
                s -> {
                    var previous = db.owned(owner, id, true);
                    if ((previous == null && expected != 0)
                            || (previous != null && previous.revision() != expected))
                        throw new Problem(409, "schedule_revision_changed");
                    var target = agents.resolveTarget(owner, definition.agentId());
                    Instant next =
                            definition.kind().equals("cron")
                                    ? cadence.next(
                                            definition.expression(), definition.zone(), db.now())
                                    : null;
                    var rule =
                            new TriggerRule(
                                    id,
                                    owner.tenant(),
                                    owner.subject(),
                                    expected + 1,
                                    definition.enabled(),
                                    definition.kind(),
                                    definition.expression(),
                                    definition.zone(),
                                    definition.misfire(),
                                    definition.overlap(),
                                    target.agentId(),
                                    definition.versionPolicy(),
                                    definition.versionPolicy().equals("fixed")
                                            ? target.releaseHash()
                                            : null,
                                    definition.message(),
                                    definition.priority(),
                                    next,
                                    null,
                                    null);
                    if (previous == null) db.insert(rule);
                    else if (!db.replace(rule, expected))
                        throw new Problem(409, "schedule_revision_changed");
                    return rule;
                });
    }

    public TriggerRule get(Owner owner, String id) {
        TriggerRule.requireId(id);
        var rule = db.owned(owner, id, false);
        if (rule == null) throw new Problem(404, "not_found");
        return rule;
    }

    public List<TriggerRule> list(Owner owner, String after, int limit) {
        if (after == null || after.length() > 128 || limit < 1 || limit > 100)
            throw new Problem(422, "invalid_pagination");
        return db.list(owner, after, limit);
    }

    public List<TriggerFiring> history(Owner owner, String id, Instant after, int limit) {
        return history(owner, id, after, "", limit);
    }

    public List<TriggerFiring> history(
            Owner owner, String id, Instant after, String afterKey, int limit) {
        get(owner, id);
        if (after == null
                || afterKey == null
                || afterKey.length() > 600
                || limit < 1
                || limit > 100) throw new Problem(422, "invalid_pagination");
        return db.history(id, after, afterKey, limit);
    }

    /** 先回放相同事件的持久回执，再检查启停状态；禁用规则不能把已经受理的重试变成新工作。 */
    public TriggerFiring webhook(Owner owner, String id, String key) {
        TriggerRule.requireId(id);
        new IdempotencyKey(key);
        return tx.execute(
                s -> {
                    var rule = db.owned(owner, id, true);
                    if (rule == null) throw new Problem(404, "not_found");
                    String event = "webhook:" + key;
                    var replay = db.replay(id, event);
                    if (replay != null) return replay;
                    if (!rule.kind().equals("webhook"))
                        throw new Problem(409, "schedule_kind_mismatch");
                    if (!rule.enabled()) throw new Problem(409, "schedule_disabled");
                    return admit(rule, event, db.now(), null);
                });
    }

    /**
     * 每次 tick 只锁定一条到期规则，不把整批轮询包进事务；多个调度器不能重复受理同一时点。 受理异常后单独尝试延后原修订、原到期时点，避免坏目标长期占据队首；
     * 提交结果不确定时不能推断已经回滚，条件更新也不能覆盖已推进的规则。
     */
    public boolean tick() {
        var selected = new java.util.concurrent.atomic.AtomicReference<TriggerRule>();
        try {
            return Boolean.TRUE.equals(
                    tx.execute(
                            s -> {
                                var rule = db.nextDue();
                                if (rule == null) return false;
                                selected.set(rule);
                                Instant now = db.now();
                                String key = "cron:" + rule.nextAt();
                                Instant next =
                                        cadence.next(
                                                rule.expression(),
                                                rule.zone(),
                                                rule.misfire().equals("catch_up")
                                                        ? rule.nextAt()
                                                        : now);
                                if (db.replay(rule.id(), key) == null) {
                                    boolean late =
                                            Duration.between(rule.nextAt(), now).getSeconds()
                                                    > graceSeconds;
                                    if (late && rule.misfire().equals("skip"))
                                        db.record(
                                                receipt(
                                                        rule,
                                                        key,
                                                        rule.nextAt(),
                                                        "skipped_misfire",
                                                        null,
                                                        null,
                                                        null,
                                                        now));
                                    else
                                        admit(
                                                rule,
                                                key,
                                                rule.nextAt(),
                                                rule.misfire().equals("catch_up") ? null : now);
                                }
                                db.advance(rule.id(), rule.revision(), next);
                                return true;
                            }));
        } catch (RuntimeException failure) {
            // The failed admission transaction has already rolled back. Defer only its unchanged
            // revision so an unavailable target cannot monopolize every later due rule.
            var rule = selected.get();
            if (rule != null) {
                try {
                    tx.executeWithoutResult(
                            s ->
                                    db.defer(
                                            rule.id(),
                                            rule.revision(),
                                            rule.nextAt(),
                                            "admission_failed"));
                } catch (RuntimeException unavailable) {
                    failure.addSuppressed(unavailable);
                }
            }
            throw failure;
        }
    }

    private TriggerFiring admit(
            TriggerRule rule, String key, Instant scheduledAt, Instant coalesced) {
        if (rule.overlap().equals("skip") && db.active(rule.id())) {
            var receipt =
                    receipt(rule, key, scheduledAt, "skipped_overlap", null, null, null, coalesced);
            db.record(receipt);
            return receipt;
        }
        // 复用外层规则事务：新会话、固定发布的 Turn、优先级和触发回执必须一起提交或回滚。
        String conversation =
                rule.versionPolicy().equals("fixed")
                        ? submission.createPinnedConversation(
                                rule.owner(), rule.agentId(), rule.releaseHash())
                        : submission.createConversation(rule.owner(), rule.agentId());
        var accepted =
                submission.submit(
                        rule.owner(), conversation, "trigger:" + UUID.randomUUID(), rule.message());
        db.priority(accepted.turnId(), rule.priority());
        var receipt =
                receipt(
                        rule,
                        key,
                        scheduledAt,
                        "accepted",
                        conversation,
                        accepted.turnId(),
                        db.release(accepted.turnId()),
                        coalesced);
        db.record(receipt);
        return receipt;
    }

    private static TriggerFiring receipt(
            TriggerRule r,
            String key,
            Instant at,
            String status,
            String conversation,
            String turn,
            String hash,
            Instant until) {
        return new TriggerFiring(
                r.id(), r.revision(), key, at, status, conversation, turn, hash, until);
    }
}
