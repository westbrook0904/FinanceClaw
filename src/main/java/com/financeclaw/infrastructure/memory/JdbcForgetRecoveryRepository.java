package com.financeclaw.infrastructure.memory;

import com.financeclaw.app.memory.port.ForgetRecoveryRepository;
import com.financeclaw.domain.ExecutionScope;
import com.financeclaw.domain.Memory.Privacy;
import com.financeclaw.domain.Task.Owner;
import com.financeclaw.domain.memory.ForgetFact;
import java.nio.charset.StandardCharsets;
import java.sql.ResultSet;
import java.sql.SQLException;
import java.util.ArrayList;
import java.util.HashMap;
import java.util.List;
import java.util.Map;
import java.util.UUID;
import javax.sql.DataSource;
import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.transaction.support.TransactionSynchronizationManager;

/**
 * 主库中遗忘请求、应用回执与恢复屏障的 JDBC 实现。
 *
 * <p>独立 forget ledger 保存不可撤销的遗忘事实；本仓库将事实重放到主库并擦除残留， 不能仅凭 epoch 相等判断备份安全。owner、正文、索引、画像和任务均参与检查，确认
 * 安全后才能清除 purge_pending。完整审计历史用于当前恢复核验，不能作为普通迁移兼容代码删除。
 */
public final class JdbcForgetRecoveryRepository implements ForgetRecoveryRepository {
    private static final int MAX_FACTS = 100_000;
    private static final String FACT_COLUMNS =
            "event_id,lineage,tenant,subject,from_epoch,operation_key,request_hash";
    private final JdbcTemplate db;

    public JdbcForgetRecoveryRepository(DataSource source) {
        db = new JdbcTemplate(source);
    }

    @Override
    public Binding binding() {
        return readBinding("");
    }

    @Override
    public Binding lockBinding() {
        transaction();
        return readBinding(" FOR UPDATE");
    }

    private Binding readBinding(String lock) {
        var rows =
                db.query(
                        "SELECT lineage,active FROM memory.privacy_ledger_binding WHERE singleton"
                                + lock,
                        (rs, n) -> new Binding(rs.getString(1), rs.getBoolean(2)));
        if (rows.size() != 1) {
            throw invalid("forget_binding_invalid");
        }
        try {
            UUID.fromString(rows.getFirst().lineage());
        } catch (RuntimeException error) {
            throw invalid("forget_binding_invalid");
        }
        return rows.getFirst();
    }

    @Override
    public void activate() {
        transaction();
        db.execute("SET LOCAL synchronous_commit=on");
        if (db.update("UPDATE memory.privacy_ledger_binding SET active=true WHERE singleton")
                != 1) {
            throw invalid("forget_binding_invalid");
        }
    }

    @Override
    public ForgetFact pending(Owner owner) {
        var rows =
                db.query(
                        "SELECT "
                                + FACT_COLUMNS
                                + " FROM memory.forget_requests WHERE tenant=? AND subject=?",
                        this::fact,
                        owner.tenant(),
                        owner.subject());
        return rows.isEmpty() ? null : rows.getFirst();
    }

    @Override
    public List<ForgetFact> pending() {
        var rows =
                db.query(
                        "SELECT "
                                + FACT_COLUMNS
                                + " FROM memory.forget_requests ORDER BY tenant,subject LIMIT ?",
                        this::fact,
                        MAX_FACTS + 1);
        limit(rows.size());
        return List.copyOf(rows);
    }

    @Override
    public void prepare(ForgetFact fact) {
        transaction();
        db.execute("SET LOCAL synchronous_commit=on");
        lineage(fact);
        var owner = owner(fact.owner());
        if (owner.epoch() != fact.fromEpoch()) {
            throw invalid("forget_epoch_conflict");
        }
        var pending = pending(fact.owner());
        if (pending != null && !pending.equals(fact)) {
            throw invalid("forget_pending_conflict");
        }
        if (pending == null) {
            db.update(
                    "INSERT INTO memory.forget_requests("
                            + FACT_COLUMNS
                            + ") VALUES(?,?,?,?,?,?,?)",
                    values(fact));
        }
        db.update(
                "UPDATE memory.owners SET purge_pending=true WHERE tenant=? AND subject=?",
                fact.owner().tenant(),
                fact.owner().subject());
    }

    @Override
    public void apply(ForgetFact fact) {
        transaction();
        db.execute("SET LOCAL synchronous_commit=on");
        lineage(fact);
        var o = fact.owner();
        if (owner(o).epoch() < fact.fromEpoch()) {
            throw invalid("forget_epoch_gap");
        }
        var pending = pending(o);
        if (pending != null && !pending.equals(fact) && pending.fromEpoch() <= fact.fromEpoch()) {
            throw invalid("forget_pending_conflict");
        }
        var applied =
                db.query(
                        "SELECT "
                                + FACT_COLUMNS
                                + " FROM memory.forget_applied WHERE event_id=? OR (tenant=? AND"
                                + " subject=? AND from_epoch=?)",
                        this::fact,
                        fact.eventId(),
                        o.tenant(),
                        o.subject(),
                        fact.fromEpoch());
        if (applied.stream().anyMatch(old -> !old.equals(fact))) {
            throw invalid("forget_applied_conflict");
        }
        var receipts =
                db.query(
                        "SELECT request_hash,result FROM memory.operations WHERE tenant=? AND"
                                + " subject=? AND key=?",
                        (rs, n) -> List.of(rs.getString(1), rs.getString(2)),
                        o.tenant(),
                        o.subject(),
                        fact.key());
        if (!receipts.isEmpty()
                && !receipts.getFirst()
                        .equals(List.of(fact.requestHash(), Long.toString(fact.toEpoch())))) {
            throw invalid("forget_operation_conflict");
        }
        var audits = forgetAudits(o, fact.toEpoch());

        // 账本重放也清理执行副本。集合函数先建立 SDK 墓碑，再移除步骤/调用绑定，
        // 并按来源 epoch 保留此遗忘事实之后产生的新资料；任一写入失败仍由外层事务回滚。
        db.queryForObject(
                "SELECT agent_runtime.erase_execution_data(?,?,?)",
                Long.class,
                o.tenant(),
                o.subject(),
                fact.toEpoch());

        db.update(
                "DELETE FROM memory.user_profiles WHERE tenant=? AND subject=? AND epoch<?",
                o.tenant(),
                o.subject(),
                fact.toEpoch());
        db.update(
                "UPDATE memory.sources SET content=NULL,status='forgotten' WHERE tenant=? AND"
                        + " subject=? AND epoch<? AND (content IS NOT NULL OR status<>'forgotten')",
                o.tenant(),
                o.subject(),
                fact.toEpoch());
        db.update(
                "UPDATE memory.facts SET"
                        + " content=NULL,evidence=NULL,status='forgotten',revision=revision+1 WHERE"
                        + " tenant=? AND subject=? AND epoch<? AND (content IS NOT NULL OR evidence IS"
                        + " NOT NULL OR status<>'forgotten')",
                o.tenant(),
                o.subject(),
                fact.toEpoch());
        db.update(
                "DELETE FROM memory.projections WHERE tenant=? AND subject=? AND epoch<?",
                o.tenant(),
                o.subject(),
                fact.toEpoch());
        db.update(
                "DELETE FROM memory.turn_snapshots WHERE tenant=? AND subject=? AND epoch<?",
                o.tenant(),
                o.subject(),
                fact.toEpoch());
        // 工作上下文含当次问答和工具过程，与长期记忆正文一起跨越遗忘边界清除。
        db.update(
                "DELETE FROM agent_runtime.work_contexts WHERE tenant=? AND subject=? AND epoch<?",
                o.tenant(),
                o.subject(),
                fact.toEpoch());
        db.update(
                "UPDATE memory.jobs SET status='forgotten',lease_token=NULL,lease_until=NULL WHERE"
                        + " tenant=? AND subject=? AND epoch<? AND (status<>'forgotten' OR lease_token"
                        + " IS NOT NULL OR lease_until IS NOT NULL)",
                o.tenant(),
                o.subject(),
                fact.toEpoch());
        db.update(
                "UPDATE memory.owners SET profile_revision=CASE WHEN epoch<? THEN 0 ELSE"
                        + " profile_revision END,epoch=GREATEST(epoch,?),purge_pending=true WHERE"
                        + " tenant=? AND subject=?",
                fact.toEpoch(),
                fact.toEpoch(),
                o.tenant(),
                o.subject());
        if (audits.isEmpty()) {
            audit(o, fact.toEpoch(), "forget");
        }
        if (receipts.isEmpty()) {
            db.update(
                    "INSERT INTO memory.operations(tenant,subject,key,request_hash,result)"
                            + " VALUES(?,?,?,?,?)",
                    o.tenant(),
                    o.subject(),
                    fact.key(),
                    fact.requestHash(),
                    Long.toString(fact.toEpoch()));
        }
        if (applied.isEmpty()) {
            db.update(
                    "INSERT INTO memory.forget_applied(" + FACT_COLUMNS + ") VALUES(?,?,?,?,?,?,?)",
                    values(fact));
        }
        db.update(
                "DELETE FROM memory.forget_requests WHERE tenant=? AND subject=? AND event_id=?",
                o.tenant(),
                o.subject(),
                fact.eventId());
    }

    @Override
    public List<ForgetFact> history() {
        var lineage = binding().lineage();
        var owners = new HashMap<Owner, Long>();
        for (var p : owners()) {
            if (p.epoch() < 1) {
                throw invalid("forget_history_invalid");
            }
            owners.put(new Owner(p.tenant(), p.subject()), p.epoch());
        }
        var appliedRows =
                db.query(
                        "SELECT " + FACT_COLUMNS + " FROM memory.forget_applied LIMIT ?",
                        this::fact,
                        MAX_FACTS + 1);
        limit(appliedRows.size());
        Map<OwnerEpoch, ForgetFact> applied = new HashMap<>();
        for (var f : appliedRows) {
            if (!f.lineage().equals(lineage)
                    || applied.put(new OwnerEpoch(f.owner(), f.fromEpoch()), f) != null) {
                throw invalid("forget_history_invalid");
            }
        }
        var audits =
                db.query(
                        "SELECT tenant,subject,epoch,target FROM memory.audit WHERE action='forget'"
                                + " ORDER BY tenant,subject,id LIMIT ?",
                        (rs, n) ->
                                new HistoricalAudit(
                                        new Owner(rs.getString(1), rs.getString(2)),
                                        rs.getLong(3),
                                        rs.getString(4)),
                        MAX_FACTS + 1);
        limit(audits.size());
        var observed = new HashMap<Owner, Long>();
        var result = new ArrayList<ForgetFact>();
        for (var a : audits) {
            long from = observed.getOrDefault(a.owner(), 1L);
            if (!"all".equals(a.target())
                    || a.epoch() != from + 1
                    || a.epoch() > owners.getOrDefault(a.owner(), 0L)) {
                throw invalid("forget_history_invalid");
            }
            var keys =
                    db.query(
                            "SELECT key FROM memory.operations WHERE tenant=? AND subject=? AND"
                                    + " request_hash=? AND result=? LIMIT 2",
                            (rs, n) -> rs.getString(1),
                            a.owner().tenant(),
                            a.owner().subject(),
                            ForgetFact.hash(from),
                            Long.toString(a.epoch()));
            if (keys.size() > 1) {
                throw invalid("forget_history_ambiguous_receipt");
            }
            var existing = applied.remove(new OwnerEpoch(a.owner(), from));
            if (existing != null) {
                if (!keys.isEmpty() && !keys.getFirst().equals(existing.key())) {
                    throw invalid("forget_history_ambiguous_receipt");
                }
                result.add(existing);
            } else {
                if (keys.isEmpty()) {
                    throw invalid("forget_history_missing_receipt");
                }
                var identity =
                        ExecutionScope.hash(
                                List.of(
                                        "forget-bootstrap-v1",
                                        lineage,
                                        a.owner().tenant(),
                                        a.owner().subject(),
                                        Long.toString(from)));
                var eventId =
                        UUID.nameUUIDFromBytes(identity.getBytes(StandardCharsets.UTF_8))
                                .toString();
                result.add(
                        new ForgetFact(
                                eventId,
                                lineage,
                                a.owner(),
                                from,
                                keys.getFirst(),
                                ForgetFact.hash(from)));
            }
            observed.put(a.owner(), a.epoch());
        }
        if (!applied.isEmpty()
                || owners.entrySet().stream()
                        .anyMatch(
                                e -> !e.getValue().equals(observed.getOrDefault(e.getKey(), 1L)))) {
            throw invalid("forget_history_invalid");
        }
        return List.copyOf(result);
    }

    @Override
    public List<Privacy> owners() {
        return db.query(
                "SELECT tenant,subject,epoch,purge_pending,profile_revision FROM memory.owners"
                        + " ORDER BY tenant,subject",
                (rs, n) ->
                        new Privacy(
                                rs.getString(1),
                                rs.getString(2),
                                rs.getLong(3),
                                rs.getBoolean(4),
                                rs.getLong(5)));
    }

    @Override
    public void complete(Owner owner) {
        transaction();
        var p = owner(owner);
        if (pending(owner) != null) {
            throw invalid("forget_pending_conflict");
        }
        if (p.epoch() > 1) {
            var audits = forgetAudits(owner, p.epoch());
            if (audits.size() != 1) {
                throw invalid("forget_history_invalid");
            }
            boolean completed =
                    Boolean.TRUE.equals(
                            db.queryForObject(
                                    "SELECT EXISTS(SELECT 1 FROM memory.audit WHERE tenant=? AND"
                                            + " subject=? AND epoch=? AND action='purge_complete' AND"
                                            + " target='all' AND id>? AND created_at>=(SELECT"
                                            + " created_at FROM memory.audit WHERE id=?))",
                                    Boolean.class,
                                    owner.tenant(),
                                    owner.subject(),
                                    p.epoch(),
                                    audits.getFirst(),
                                    audits.getFirst()));
            if (!completed) {
                audit(owner, p.epoch(), "purge_complete");
            }
        } else if (p.epoch() != 1) {
            throw invalid("forget_epoch_invalid");
        }
        db.update(
                "UPDATE memory.owners SET purge_pending=false WHERE tenant=? AND subject=?",
                owner.tenant(),
                owner.subject());
    }

    @Override
    public long unsafeRows() {
        return db.queryForObject(
                """
SELECT
  (SELECT count(*) FROM agent_runtime.unsafe_execution_data())
  +
  (SELECT count(*) FROM memory.owners WHERE epoch<1 OR profile_revision<0 OR purge_pending)
  + (SELECT count(*) FROM memory.owners o WHERE o.epoch>1 AND NOT o.purge_pending
     AND NOT EXISTS(SELECT 1 FROM memory.audit f JOIN memory.audit p
       ON p.tenant=f.tenant AND p.subject=f.subject AND p.epoch=f.epoch
       AND p.action='purge_complete' AND p.target='all'
       AND p.id>f.id AND p.created_at>=f.created_at
       WHERE f.tenant=o.tenant AND f.subject=o.subject AND f.epoch=o.epoch
         AND f.action='forget' AND f.target='all'))
  + (SELECT count(*) FROM memory.forget_requests)
  + (SELECT count(*) FROM memory.user_profiles p LEFT JOIN memory.owners o USING(tenant,subject)
     WHERE o.tenant IS NULL OR p.epoch<>o.epoch OR p.epoch<1
       OR p.revision<1 OR p.revision<>o.profile_revision)
  + (SELECT count(*) FROM memory.owners o WHERE o.profile_revision>0 AND NOT EXISTS
     (SELECT 1 FROM memory.user_profiles p WHERE p.tenant=o.tenant AND p.subject=o.subject
       AND p.epoch=o.epoch AND p.revision=o.profile_revision))
  + (SELECT count(*) FROM memory.sources s LEFT JOIN memory.owners o USING(tenant,subject)
     WHERE o.tenant IS NULL OR s.epoch>o.epoch OR s.epoch<1
       OR ((s.epoch<o.epoch OR s.status='forgotten') AND (s.content IS NOT NULL OR s.status<>'forgotten')))
  + (SELECT count(*) FROM memory.facts f LEFT JOIN memory.owners o USING(tenant,subject)
     WHERE o.tenant IS NULL OR f.epoch>o.epoch OR f.epoch<1
       OR ((f.epoch<o.epoch OR f.status='forgotten') AND (f.content IS NOT NULL OR f.evidence IS NOT NULL OR f.status<>'forgotten')))
  + (SELECT count(*) FROM memory.projections p LEFT JOIN memory.owners o USING(tenant,subject)
     LEFT JOIN memory.facts f ON f.id=p.fact_id
     WHERE o.tenant IS NULL OR p.epoch<>o.epoch OR f.id IS NULL
       OR f.tenant<>p.tenant OR f.subject<>p.subject OR f.epoch<>p.epoch OR f.status='forgotten')
  + (SELECT count(*) FROM memory.turn_snapshots p LEFT JOIN memory.owners o USING(tenant,subject)
     LEFT JOIN memory.sources s ON s.id=p.turn_id
     WHERE o.tenant IS NULL OR p.epoch<>o.epoch OR s.id IS NULL
       OR s.tenant<>p.tenant OR s.subject<>p.subject OR s.epoch<>p.epoch
       OR s.profile_revision<>p.profile_revision OR s.status='forgotten')
  + (SELECT count(*) FROM agent_runtime.work_contexts w LEFT JOIN memory.owners o USING(tenant,subject)
     LEFT JOIN memory.sources s ON s.id=w.turn_id
     WHERE o.tenant IS NULL OR w.epoch<>o.epoch OR s.id IS NULL
       OR s.tenant<>w.tenant OR s.subject<>w.subject OR s.epoch<>w.epoch OR s.status='forgotten')
  + (SELECT count(*) FROM memory.jobs j LEFT JOIN memory.owners o USING(tenant,subject)
     WHERE o.tenant IS NULL OR j.epoch>o.epoch OR j.epoch<1
       OR (j.epoch<o.epoch AND (j.status NOT IN ('forgotten','obsolete') OR j.lease_token IS NOT NULL OR j.lease_until IS NOT NULL)))
  + (SELECT count(*) FROM memory.audit a LEFT JOIN memory.owners o USING(tenant,subject)
     WHERE o.tenant IS NULL OR a.epoch<1 OR a.epoch>o.epoch
       OR (a.action IN ('forget','purge_complete') AND (a.target<>'all' OR a.epoch=1))
       OR (a.action='purge_complete' AND NOT EXISTS(SELECT 1 FROM memory.audit f
         WHERE f.tenant=a.tenant AND f.subject=a.subject AND f.epoch=a.epoch
           AND f.action='forget' AND f.target='all' AND f.id<a.id AND f.created_at<=a.created_at)))
  + (SELECT count(*) FROM memory.operations p LEFT JOIN memory.owners o USING(tenant,subject)
     WHERE o.tenant IS NULL)
  + (SELECT count(*) FROM memory.forget_applied a LEFT JOIN memory.owners o USING(tenant,subject)
     WHERE o.tenant IS NULL OR a.from_epoch>=o.epoch
       OR a.lineage<>(SELECT lineage FROM memory.privacy_ledger_binding WHERE singleton))
""",
                Long.class);
    }

    private Privacy owner(Owner o) {
        var rows =
                db.query(
                        "SELECT tenant,subject,epoch,purge_pending,profile_revision FROM"
                                + " memory.owners WHERE tenant=? AND subject=?",
                        (rs, n) ->
                                new Privacy(
                                        rs.getString(1),
                                        rs.getString(2),
                                        rs.getLong(3),
                                        rs.getBoolean(4),
                                        rs.getLong(5)),
                        o.tenant(),
                        o.subject());
        if (rows.size() != 1) {
            throw invalid("forget_owner_missing");
        }
        return rows.getFirst();
    }

    private List<Long> forgetAudits(Owner o, long epoch) {
        var rows =
                db.query(
                        "SELECT id,target FROM memory.audit WHERE tenant=? AND subject=? AND"
                                + " epoch=? AND action='forget' ORDER BY id",
                        (rs, n) -> {
                            if (!"all".equals(rs.getString(2))) {
                                throw invalid("forget_history_invalid");
                            }
                            return rs.getLong(1);
                        },
                        o.tenant(),
                        o.subject(),
                        epoch);
        if (rows.size() > 1) {
            throw invalid("forget_history_invalid");
        }
        return rows;
    }

    private void audit(Owner o, long epoch, String action) {
        db.update(
                "INSERT INTO memory.audit(tenant,subject,epoch,action,target)"
                        + " VALUES(?,?,?,?,'all')",
                o.tenant(),
                o.subject(),
                epoch,
                action);
    }

    private void lineage(ForgetFact fact) {
        if (!binding().lineage().equals(fact.lineage())) {
            throw invalid("forget_lineage_conflict");
        }
    }

    private ForgetFact fact(ResultSet rs, int row) throws SQLException {
        try {
            return new ForgetFact(
                    rs.getString("event_id"),
                    rs.getString("lineage"),
                    new Owner(rs.getString("tenant"), rs.getString("subject")),
                    rs.getLong("from_epoch"),
                    rs.getString("operation_key"),
                    rs.getString("request_hash"));
        } catch (RuntimeException error) {
            throw invalid("forget_fact_invalid");
        }
    }

    private static Object[] values(ForgetFact fact) {
        return new Object[] {
            fact.eventId(),
            fact.lineage(),
            fact.owner().tenant(),
            fact.owner().subject(),
            fact.fromEpoch(),
            fact.key(),
            fact.requestHash()
        };
    }

    private static void transaction() {
        if (!TransactionSynchronizationManager.isActualTransactionActive()) {
            throw invalid("forget_transaction_required");
        }
    }

    private static void limit(int count) {
        if (count > MAX_FACTS) {
            throw invalid("forget_history_limit");
        }
    }

    private static IllegalStateException invalid(String code) {
        return new IllegalStateException(code);
    }

    private record OwnerEpoch(Owner owner, long fromEpoch) {}

    private record HistoricalAudit(Owner owner, long epoch, String target) {}
}
