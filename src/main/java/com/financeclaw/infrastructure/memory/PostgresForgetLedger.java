package com.financeclaw.infrastructure.memory;

import com.financeclaw.app.memory.port.ForgetLedger;
import com.financeclaw.domain.Task.Owner;
import com.financeclaw.domain.Task.Problem;
import com.financeclaw.domain.memory.ForgetFact;
import java.util.ArrayList;
import java.util.Comparator;
import java.util.HashMap;
import java.util.HashSet;
import java.util.List;
import java.util.Map;
import java.util.UUID;
import java.util.function.Supplier;
import javax.sql.DataSource;
import org.springframework.dao.DataAccessException;
import org.springframework.dao.DataIntegrityViolationException;
import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.jdbc.core.RowMapper;
import org.springframework.jdbc.support.JdbcTransactionManager;
import org.springframework.transaction.TransactionDefinition;
import org.springframework.transaction.TransactionException;
import org.springframework.transaction.support.TransactionTemplate;

/**
 * 独立 PostgreSQL 遗忘事实账本，作为主库备份之外的恢复权威。
 *
 * <p>追加按 lineage、owner epoch 和请求身份校验幂等性；未知提交结果保留在主库的 屏障，不能靠重新生成请求消除。初始化前先校验完整历史连续性，恢复时按权威事实
 * 重放，禁止从旧业务状态反向覆盖已确认遗忘。
 */
public final class PostgresForgetLedger implements ForgetLedger {
    public static final int MAX_FACTS = 100_000;
    private static final String COLUMNS =
            "event_id,lineage,tenant,subject,from_epoch,idempotency_key,request_hash";
    private static final Comparator<ForgetFact> ORDER =
            Comparator.comparing((ForgetFact f) -> f.owner().tenant())
                    .thenComparing(f -> f.owner().subject())
                    .thenComparingLong(ForgetFact::fromEpoch);
    private static final RowMapper<ForgetFact> FACT =
            (row, index) ->
                    new ForgetFact(
                            row.getString("event_id"),
                            row.getString("lineage"),
                            new Owner(row.getString("tenant"), row.getString("subject")),
                            row.getLong("from_epoch"),
                            row.getString("idempotency_key"),
                            row.getString("request_hash"));

    private final JdbcTemplate sql;
    private final TransactionTemplate write;
    private final TransactionTemplate read;
    private final DataSource source;
    private final boolean closeSource;

    public PostgresForgetLedger(DataSource source) {
        this(source, false);
    }

    public PostgresForgetLedger(DataSource source, boolean closeSource) {
        this.source = source;
        this.closeSource = closeSource;
        sql = new JdbcTemplate(source);
        var manager = new JdbcTransactionManager(source);
        write = new TransactionTemplate(manager);
        write.setPropagationBehavior(TransactionDefinition.PROPAGATION_REQUIRES_NEW);
        read = new TransactionTemplate(manager);
        read.setPropagationBehavior(TransactionDefinition.PROPAGATION_REQUIRES_NEW);
        read.setIsolationLevel(TransactionDefinition.ISOLATION_REPEATABLE_READ);
        read.setReadOnly(true);
    }

    @Override
    public boolean enabled() {
        return true;
    }

    @Override
    public void initialize(String lineage, List<ForgetFact> history) {
        var expected = validate(lineage, history);
        safe(
                () ->
                        write.execute(
                                status -> {
                                    prepareWrite();
                                    int created =
                                            sql.update(
                                                    "INSERT INTO privacy_ledger.lineages(lineage)"
                                                            + " VALUES (?) ON CONFLICT DO NOTHING",
                                                    uuid(lineage));
                                    lock(lineage);
                                    if (created == 1) {
                                        sql.batchUpdate(
                                                "INSERT INTO privacy_ledger.forget_facts ("
                                                        + COLUMNS
                                                        + ") VALUES (?,?,?,?,?,?,?)",
                                                expected,
                                                500,
                                                (statement, fact) -> {
                                                    statement.setObject(1, uuid(fact.eventId()));
                                                    statement.setObject(2, uuid(fact.lineage()));
                                                    statement.setString(3, fact.owner().tenant());
                                                    statement.setString(4, fact.owner().subject());
                                                    statement.setLong(5, fact.fromEpoch());
                                                    statement.setString(6, fact.key());
                                                    statement.setString(7, fact.requestHash());
                                                });
                                    } else {
                                        var existing = load(lineage);
                                        if (expected.isEmpty() && !existing.isEmpty()) {
                                            fail("conflict");
                                        }
                                        var byId = new HashMap<String, ForgetFact>();
                                        for (var fact : existing) {
                                            byId.put(fact.eventId(), fact);
                                        }
                                        for (var fact : expected) {
                                            if (!fact.equals(byId.get(fact.eventId()))) {
                                                fail("conflict");
                                            }
                                        }
                                    }
                                    return null;
                                }));
    }

    @Override
    public void append(ForgetFact fact) {
        validate(fact.lineage(), List.of());
        safe(
                () ->
                        write.execute(
                                status -> {
                                    prepareWrite();
                                    lock(fact.lineage());
                                    var prior =
                                            sql.query(
                                                    "SELECT "
                                                            + COLUMNS
                                                            + " FROM privacy_ledger.forget_facts"
                                                            + " WHERE lineage=? AND tenant=? AND"
                                                            + " subject=? ORDER BY from_epoch LIMIT"
                                                            + " ?",
                                                    FACT,
                                                    uuid(fact.lineage()),
                                                    fact.owner().tenant(),
                                                    fact.owner().subject(),
                                                    MAX_FACTS + 1);
                                    validate(fact.lineage(), prior);
                                    var collisions =
                                            sql.query(
                                                    "SELECT "
                                                            + COLUMNS
                                                            + " FROM privacy_ledger.forget_facts"
                                                            + " WHERE event_id=? OR (lineage=? AND"
                                                            + " tenant=? AND subject=? AND"
                                                            + " (idempotency_key=? OR"
                                                            + " from_epoch=?))",
                                                    FACT,
                                                    uuid(fact.eventId()),
                                                    uuid(fact.lineage()),
                                                    fact.owner().tenant(),
                                                    fact.owner().subject(),
                                                    fact.key(),
                                                    fact.fromEpoch());
                                    if (!collisions.isEmpty()) {
                                        if (collisions.size() != 1
                                                || !fact.equals(collisions.getFirst())) {
                                            fail("conflict");
                                        }
                                        return null;
                                    }
                                    Long size =
                                            sql.queryForObject(
                                                    "SELECT count(*) FROM"
                                                            + " privacy_ledger.forget_facts WHERE"
                                                            + " lineage=?",
                                                    Long.class,
                                                    uuid(fact.lineage()));
                                    if (size == null || size >= MAX_FACTS) {
                                        fail("limit");
                                    }
                                    if (fact.fromEpoch() != prior.size() + 1L) {
                                        fail("conflict");
                                    }
                                    insert(fact);
                                    return null;
                                }));
    }

    @Override
    public List<ForgetFact> snapshot(String lineage) {
        uuid(lineage);
        return safe(
                () ->
                        read.execute(
                                status -> {
                                    requireIndependentDatabase();
                                    if (sql.queryForList(
                                                    "SELECT lineage FROM privacy_ledger.lineages"
                                                            + " WHERE lineage=?",
                                                    UUID.class,
                                                    uuid(lineage))
                                            .isEmpty()) {
                                        fail("uninitialized");
                                    }
                                    return load(lineage);
                                }));
    }

    @Override
    public void close() {
        if (closeSource && source instanceof AutoCloseable closeable) {
            try {
                closeable.close();
            } catch (Exception unavailable) {
                throw new IllegalStateException("forget_ledger_unavailable");
            }
        }
    }

    private List<ForgetFact> load(String lineage) {
        return validate(
                lineage,
                sql.query(
                        "SELECT "
                                + COLUMNS
                                + " FROM privacy_ledger.forget_facts WHERE lineage=?"
                                + " ORDER BY tenant,subject,from_epoch LIMIT ?",
                        FACT,
                        uuid(lineage),
                        MAX_FACTS + 1));
    }

    private void lock(String lineage) {
        if (sql.queryForList(
                        "SELECT lineage FROM privacy_ledger.lineages WHERE lineage=? FOR UPDATE",
                        UUID.class,
                        uuid(lineage))
                .isEmpty()) {
            fail("uninitialized");
        }
    }

    private void prepareWrite() {
        requireIndependentDatabase();
        sql.execute("SET LOCAL synchronous_commit=on");
    }

    private void requireIndependentDatabase() {
        Boolean mainDatabase =
                sql.queryForObject(
                        "SELECT EXISTS (SELECT 1 FROM pg_catalog.pg_class c JOIN"
                                + " pg_catalog.pg_namespace n ON n.oid=c.relnamespace WHERE"
                                + " n.nspname='memory' AND c.relname='privacy_ledger_binding')",
                        Boolean.class);
        if (!Boolean.FALSE.equals(mainDatabase)) {
            fail("same_database");
        }
    }

    private void insert(ForgetFact fact) {
        sql.update(
                "INSERT INTO privacy_ledger.forget_facts (" + COLUMNS + ") VALUES (?,?,?,?,?,?,?)",
                uuid(fact.eventId()),
                uuid(fact.lineage()),
                fact.owner().tenant(),
                fact.owner().subject(),
                fact.fromEpoch(),
                fact.key(),
                fact.requestHash());
    }

    private static List<ForgetFact> validate(String lineage, List<ForgetFact> history) {
        uuid(lineage);
        if (history == null) {
            fail("invalid_history");
        }
        if (history.size() > MAX_FACTS) {
            fail("limit");
        }
        var sorted = new ArrayList<>(history);
        if (sorted.stream().anyMatch(f -> f == null || !lineage.equals(f.lineage()))) {
            fail("invalid_history");
        }
        sorted.sort(ORDER);
        var eventIds = new HashSet<String>();
        Map<Owner, Long> epochs = new HashMap<>();
        Map<Owner, HashSet<String>> keys = new HashMap<>();
        for (var fact : sorted) {
            uuid(fact.eventId());
            long expected = epochs.getOrDefault(fact.owner(), 0L) + 1;
            if (fact.fromEpoch() != expected
                    || !eventIds.add(fact.eventId())
                    || !keys.computeIfAbsent(fact.owner(), ignored -> new HashSet<>())
                            .add(fact.key())) {
                fail("invalid_history");
            }
            epochs.put(fact.owner(), fact.fromEpoch());
        }
        return List.copyOf(sorted);
    }

    private static UUID uuid(String value) {
        try {
            var id = UUID.fromString(value);
            if (!id.toString().equals(value)) {
                fail("invalid_history");
            }
            return id;
        } catch (IllegalArgumentException | NullPointerException invalid) {
            throw new IllegalStateException("forget_ledger_invalid_history");
        }
    }

    private static <T> T safe(Supplier<T> operation) {
        try {
            return operation.get();
        } catch (DataIntegrityViolationException conflict) {
            throw new IllegalStateException("forget_ledger_conflict");
        } catch (DataAccessException | TransactionException unavailable) {
            // Database exceptions can contain URLs, usernames and SQL parameters. Do not retain a
            // cause.
            throw new IllegalStateException("forget_ledger_unavailable");
        } catch (IllegalArgumentException | Problem malformed) {
            throw new IllegalStateException("forget_ledger_invalid_history");
        }
    }

    private static void fail(String reason) {
        throw new IllegalStateException("forget_ledger_" + reason);
    }
}
