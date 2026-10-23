package com.financeclaw.m5;

import static org.assertj.core.api.Assertions.*;

import com.financeclaw.domain.Task.Owner;
import com.financeclaw.domain.memory.ForgetFact;
import com.financeclaw.infrastructure.memory.JdbcForgetRecoveryRepository;
import java.util.UUID;
import org.flywaydb.core.Flyway;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.params.ParameterizedTest;
import org.junit.jupiter.params.provider.ValueSource;
import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.jdbc.datasource.DriverManagerDataSource;
import org.springframework.jdbc.support.JdbcTransactionManager;
import org.springframework.transaction.support.TransactionTemplate;
import org.testcontainers.containers.PostgreSQLContainer;

class ForgetRecoveryRepositoryIT {
    static final PostgreSQLContainer<?> PG = new PostgreSQLContainer<>("postgres:16.10-alpine");
    static final Owner OWNER = new Owner("recovery-test", "owner");
    static JdbcTemplate sql;
    static JdbcForgetRecoveryRepository recovery;
    static TransactionTemplate tx;

    @BeforeAll
    static void database() {
        PG.start();
        var source =
                new DriverManagerDataSource(PG.getJdbcUrl(), PG.getUsername(), PG.getPassword());
        Flyway.configure().dataSource(source).load().migrate();
        sql = new JdbcTemplate(source);
        recovery = new JdbcForgetRecoveryRepository(source);
        tx = new TransactionTemplate(new JdbcTransactionManager(source));
    }

    @BeforeEach
    void reset() {
        sql.execute(
                "TRUNCATE business.conversations,memory.owners,memory.jobs,memory.operations,memory.audit CASCADE");
        sql.update("UPDATE memory.privacy_ledger_binding SET active=false");
        sql.update(
                "INSERT INTO memory.owners(tenant,subject) VALUES(?,?)",
                OWNER.tenant(),
                OWNER.subject());
    }

    @Test
    void repeatedReplayRetainsNewEpochDataAndRepairsMissingOperationReceipt() {
        seed("old", 1);
        profile(1, 4, "old-profile-private");
        var fact = fact(1);
        commit(
                () -> {
                    recovery.prepare(fact);
                    recovery.apply(fact);
                    recovery.complete(OWNER);
                });
        assertThat(count("memory.user_profiles")).isZero();
        assertThat(profileRevision()).isZero();
        seed("new", 2);
        profile(2, 1, "new-profile-private");
        // A superseded projection from the current epoch is legal and must not block recovery.
        sql.update("UPDATE memory.facts SET status='superseded' WHERE id='new'");
        sql.update("DELETE FROM memory.operations WHERE key=?", fact.key());

        commit(
                () -> {
                    recovery.apply(fact);
                    recovery.complete(OWNER);
                });

        assertThat(sql.queryForMap("SELECT content,status FROM memory.sources WHERE id='old'"))
                .containsEntry("content", null)
                .containsEntry("status", "forgotten");
        assertThat(
                        sql.queryForMap(
                                "SELECT content,evidence,status,revision FROM memory.facts WHERE id='old'"))
                .containsEntry("content", null)
                .containsEntry("evidence", null)
                .containsEntry("status", "forgotten")
                .containsEntry("revision", 2L);
        assertThat(
                        sql.queryForMap(
                                "SELECT content,evidence,status,revision FROM memory.facts WHERE id='new'"))
                .containsEntry("content", "private-new")
                .containsEntry("evidence", "quote-new")
                .containsEntry("status", "superseded")
                .containsEntry("revision", 1L);
        assertThat(sql.queryForList("SELECT fact_id FROM memory.projections", String.class))
                .containsExactly("new");
        assertThat(sql.queryForList("SELECT fact_id FROM memory.recalls", String.class))
                .containsExactly("new");
        assertThat(
                        sql.queryForMap(
                                "SELECT status,lease_token,lease_until FROM memory.jobs WHERE key_id='old'"))
                .containsEntry("status", "forgotten")
                .containsEntry("lease_token", null)
                .containsEntry("lease_until", null);
        assertThat(
                        sql.queryForObject(
                                "SELECT status FROM memory.jobs WHERE key_id='new'", String.class))
                .isEqualTo("running");
        assertThat(
                        sql.queryForObject(
                                "SELECT result FROM memory.operations WHERE key=?",
                                String.class,
                                fact.key()))
                .isEqualTo("2");
        assertThat(count("memory.audit WHERE action='forget'")).isEqualTo(1);
        assertThat(count("memory.audit WHERE action='purge_complete'")).isEqualTo(1);
        assertThat(recovery.history()).containsExactly(fact);
        assertThat(sql.queryForMap("SELECT epoch,revision,markdown FROM memory.user_profiles"))
                .containsEntry("epoch", 2L)
                .containsEntry("revision", 1L)
                .containsEntry("markdown", "new-profile-private");
        assertThat(profileRevision()).isEqualTo(1);
        assertThat(recovery.unsafeRows()).isZero();
    }

    @Test
    void barriersAndReplayRollBackTogetherWithCallerTransaction() {
        seed("old", 1);
        profile(1, 4, "old-profile-private");
        var fact = fact(1);
        assertThatThrownBy(
                        () ->
                                commit(
                                        () -> {
                                            recovery.prepare(fact);
                                            throw new IllegalStateException("rollback");
                                        }))
                .hasMessage("rollback");
        assertThat(recovery.pending(OWNER)).isNull();
        assertThat(recovery.owners().getFirst().purgePending()).isFalse();
        commit(() -> recovery.prepare(fact));

        assertThatThrownBy(
                        () ->
                                commit(
                                        () -> {
                                            recovery.apply(fact);
                                            throw new IllegalStateException("rollback");
                                        }))
                .hasMessage("rollback");

        assertThat(recovery.pending(OWNER)).isEqualTo(fact);
        assertThat(recovery.owners().getFirst().epoch()).isEqualTo(1);
        assertThat(recovery.owners().getFirst().purgePending()).isTrue();
        assertThat(
                        sql.queryForObject(
                                "SELECT content FROM memory.sources WHERE id='old'", String.class))
                .isEqualTo("private-old");
        assertThat(count("memory.forget_applied")).isZero();
        assertThat(count("memory.operations")).isZero();
        assertThat(count("memory.audit")).isZero();
        assertThat(sql.queryForObject("SELECT markdown FROM memory.user_profiles", String.class))
                .isEqualTo("old-profile-private");
        assertThat(profileRevision()).isEqualTo(4);
        assertThat(recovery.unsafeRows()).isGreaterThanOrEqualTo(2);
    }

    @Test
    void replayingEarlierFactLeavesLaterPendingRequestAndBarrierIntact() {
        var first = fact(1);
        commit(
                () -> {
                    recovery.apply(first);
                    recovery.complete(OWNER);
                });
        var later = fact(2);
        commit(() -> recovery.prepare(later));

        commit(() -> recovery.apply(first));

        assertThat(recovery.pending(OWNER)).isEqualTo(later);
        assertThat(recovery.owners().getFirst().purgePending()).isTrue();
        assertThatThrownBy(() -> commit(() -> recovery.complete(OWNER)))
                .hasMessage("forget_pending_conflict");
        commit(
                () -> {
                    recovery.apply(later);
                    recovery.complete(OWNER);
                });
        assertThat(recovery.pending(OWNER)).isNull();
        assertThat(recovery.owners().getFirst().epoch()).isEqualTo(3);
        assertThat(recovery.history()).containsExactly(first, later);
        assertThat(recovery.unsafeRows()).isZero();
    }

    @Test
    void conflictingRequestCannotConsumePendingIntent() {
        seed("old", 1);
        var first = fact(1);
        var another = fact(1);
        commit(() -> recovery.prepare(first));

        assertThatThrownBy(() -> commit(() -> recovery.apply(another)))
                .hasMessage("forget_pending_conflict");

        assertThat(recovery.pending(OWNER)).isEqualTo(first);
        assertThat(
                        sql.queryForObject(
                                "SELECT content FROM memory.sources WHERE id='old'", String.class))
                .isEqualTo("private-old");
        assertThat(count("memory.forget_applied")).isZero();
    }

    @Test
    void sameEpochWithDifferentEventIdentityIsRejected() {
        var first = fact(1);
        commit(
                () -> {
                    recovery.apply(first);
                    recovery.complete(OWNER);
                });

        assertThatThrownBy(() -> commit(() -> recovery.apply(fact(1))))
                .hasMessage("forget_applied_conflict");

        assertThat(recovery.history()).containsExactly(first);
        assertThat(recovery.unsafeRows()).isZero();
    }

    @Test
    void conflictingOperationReceiptIsRejectedBeforeErasure() {
        seed("old", 1);
        var fact = fact(1);
        sql.update(
                "INSERT INTO memory.operations(tenant,subject,key,request_hash,result) VALUES(?,?,?,?,?)",
                OWNER.tenant(),
                OWNER.subject(),
                fact.key(),
                "different-operation",
                "ok");

        assertThatThrownBy(() -> commit(() -> recovery.apply(fact)))
                .hasMessage("forget_operation_conflict");

        assertThat(
                        sql.queryForObject(
                                "SELECT content FROM memory.sources WHERE id='old'", String.class))
                .isEqualTo("private-old");
        assertThat(recovery.owners().getFirst().epoch()).isEqualTo(1);
        assertThat(count("memory.forget_applied")).isZero();
    }

    @Test
    void lineageAndEpochGapAreRejectedBeforeErasure() {
        seed("old", 1);
        var wrongLineage =
                new ForgetFact(
                        UUID.randomUUID().toString(),
                        UUID.randomUUID().toString(),
                        OWNER,
                        1,
                        "wrong",
                        ForgetFact.hash(1));
        assertThatThrownBy(() -> commit(() -> recovery.apply(wrongLineage)))
                .hasMessage("forget_lineage_conflict");
        assertThatThrownBy(() -> commit(() -> recovery.apply(fact(2))))
                .hasMessage("forget_epoch_gap");
        assertThat(
                        sql.queryForObject(
                                "SELECT content FROM memory.sources WHERE id='old'", String.class))
                .isEqualTo("private-old");
    }

    @Test
    void legacyHistoryUsesOriginalKeysAndStableImportedIdentity() {
        sql.update("UPDATE memory.owners SET epoch=3");
        audit(2, "forget", "all");
        audit(3, "forget", "all");
        audit(3, "purge_complete", "all");
        sql.update(
                "INSERT INTO memory.operations(tenant,subject,key,request_hash,result) VALUES(?,?,?,?,?)",
                OWNER.tenant(),
                OWNER.subject(),
                "original-key",
                ForgetFact.hash(1),
                "2");
        sql.update(
                "INSERT INTO memory.operations(tenant,subject,key,request_hash,result) VALUES(?,?,?,?,?)",
                OWNER.tenant(),
                OWNER.subject(),
                "later-key",
                ForgetFact.hash(2),
                "3");

        var history = recovery.history();

        assertThat(history).hasSize(2);
        assertThat(history.getFirst().key()).isEqualTo("original-key");
        assertThat(history.getLast().key()).isEqualTo("later-key");
        assertThat(recovery.history()).isEqualTo(history);
        commit(
                () -> {
                    history.forEach(recovery::apply);
                    recovery.complete(OWNER);
                });
        sql.update("DELETE FROM memory.operations WHERE key='original-key'");
        assertThat(recovery.history()).isEqualTo(history);
        commit(
                () -> {
                    history.forEach(recovery::apply);
                    recovery.complete(OWNER);
                });
        assertThat(
                        sql.queryForObject(
                                "SELECT result FROM memory.operations WHERE key='original-key'",
                                String.class))
                .isEqualTo("2");
        assertThat(recovery.unsafeRows()).isZero();
    }

    @ParameterizedTest
    @ValueSource(
            strings = {
                "duplicate",
                "gap",
                "invalid-target",
                "ambiguous-receipt",
                "missing-receipt"
            })
    void legacyHistoryRejectsIncompleteOrAmbiguousAuthority(String corruption) {
        sql.update("UPDATE memory.owners SET epoch=2");
        audit(2, "forget", "all");
        if (!corruption.equals("missing-receipt"))
            sql.update(
                    "INSERT INTO memory.operations(tenant,subject,key,request_hash,result) VALUES(?,?,?,?,?)",
                    OWNER.tenant(),
                    OWNER.subject(),
                    "first-key",
                    ForgetFact.hash(1),
                    "2");
        switch (corruption) {
            case "duplicate" -> audit(2, "forget", "all");
            case "gap" -> sql.update("DELETE FROM memory.audit");
            case "invalid-target" -> sql.update("UPDATE memory.audit SET target='partial'");
            case "ambiguous-receipt" ->
                    sql.update(
                            "INSERT INTO memory.operations(tenant,subject,key,request_hash,result) VALUES(?,?,?,?,?)",
                            OWNER.tenant(),
                            OWNER.subject(),
                            "second-key",
                            ForgetFact.hash(1),
                            "2");
            case "missing-receipt" -> {}
            default -> throw new AssertionError();
        }

        assertThatThrownBy(recovery::history)
                .isInstanceOf(IllegalStateException.class)
                .hasMessageStartingWith("forget_history_");
    }

    @ParameterizedTest
    @ValueSource(strings = {"missing", "earlier-id", "earlier-time"})
    void completedOwnerRequiresLatestPurgeEvidenceAfterForget(String corruption) {
        sql.update("UPDATE memory.owners SET epoch=2");
        if (corruption.equals("earlier-id")) audit(2, "purge_complete", "all");
        audit(2, "forget", "all");
        if (corruption.equals("earlier-time")) {
            audit(2, "purge_complete", "all");
            sql.update(
                    "UPDATE memory.audit SET created_at=created_at-interval '1 hour' WHERE action='purge_complete'");
        }

        assertThat(recovery.unsafeRows()).isPositive();

        commit(() -> recovery.complete(OWNER));
        if (corruption.equals("missing")) assertThat(recovery.unsafeRows()).isZero();
        else assertThat(recovery.unsafeRows()).isPositive();
    }

    @ParameterizedTest
    @ValueSource(
            strings = {
                "source",
                "fact",
                "projection",
                "recall",
                "job",
                "audit-target",
                "audit-owner",
                "operation-owner"
            })
    void unsafeRowsDetectsFutureDataAndOrphanAuthority(String corruption) {
        seed("current", 1);
        switch (corruption) {
            case "source" -> sql.update("UPDATE memory.sources SET epoch=2");
            case "fact" -> sql.update("UPDATE memory.facts SET epoch=2");
            case "projection" -> sql.update("UPDATE memory.projections SET epoch=2");
            case "recall" -> sql.update("UPDATE memory.recalls SET epoch=2");
            case "job" -> sql.update("UPDATE memory.jobs SET epoch=2");
            case "audit-target" -> audit(1, "forget", "partial");
            case "audit-owner" ->
                    sql.update(
                            "INSERT INTO memory.audit(tenant,subject,epoch,action,target) VALUES('orphan','owner',1,'confirm','fact')");
            case "operation-owner" ->
                    sql.update(
                            "INSERT INTO memory.operations(tenant,subject,key,request_hash,result) VALUES('orphan','owner','key','hash','ok')");
            default -> throw new AssertionError();
        }

        assertThat(recovery.unsafeRows()).isPositive();
    }

    @Test
    void replayDoesNotEraseUnexpectedFutureDataAndSafetyCheckBlocksIt() {
        seed("future", 3);
        profile(3, 4, "future-private-profile");
        var fact = fact(1);

        commit(
                () -> {
                    recovery.apply(fact);
                    recovery.complete(OWNER);
                });

        assertThat(
                        sql.queryForObject(
                                "SELECT content FROM memory.sources WHERE id='future'",
                                String.class))
                .isEqualTo("private-future");
        assertThat(
                        sql.queryForObject(
                                "SELECT content FROM memory.facts WHERE id='future'", String.class))
                .isEqualTo("private-future");
        assertThat(count("memory.projections")).isEqualTo(1);
        assertThat(sql.queryForObject("SELECT markdown FROM memory.user_profiles", String.class))
                .isEqualTo("future-private-profile");
        assertThat(recovery.unsafeRows()).isPositive();
    }

    @Test
    void writesRequireCallerTransaction() {
        assertThatThrownBy(() -> recovery.prepare(fact(1)))
                .hasMessage("forget_transaction_required");
        assertThatThrownBy(() -> recovery.apply(fact(1))).hasMessage("forget_transaction_required");
        assertThatThrownBy(() -> recovery.complete(OWNER))
                .hasMessage("forget_transaction_required");
        assertThatThrownBy(recovery::activate).hasMessage("forget_transaction_required");
        assertThatThrownBy(recovery::lockBinding).hasMessage("forget_transaction_required");
        assertThat(recovery.pending()).isEmpty();
    }

    private static ForgetFact fact(long epoch) {
        return new ForgetFact(
                UUID.randomUUID().toString(),
                recovery.binding().lineage(),
                OWNER,
                epoch,
                UUID.randomUUID().toString(),
                ForgetFact.hash(epoch));
    }

    private static void commit(Runnable action) {
        tx.executeWithoutResult(
                status -> {
                    sql.queryForObject(
                            "SELECT epoch FROM memory.owners WHERE tenant=? AND subject=? FOR UPDATE",
                            Long.class,
                            OWNER.tenant(),
                            OWNER.subject());
                    action.run();
                });
    }

    private static long count(String tableAndPredicate) {
        return sql.queryForObject("SELECT count(*) FROM " + tableAndPredicate, Long.class);
    }

    private static void audit(long epoch, String action, String target) {
        sql.update(
                "INSERT INTO memory.audit(tenant,subject,epoch,action,target) VALUES(?,?,?,?,?)",
                OWNER.tenant(),
                OWNER.subject(),
                epoch,
                action,
                target);
    }

    private static long profileRevision() {
        return sql.queryForObject(
                "SELECT profile_revision FROM memory.owners WHERE tenant=? AND subject=?",
                Long.class,
                OWNER.tenant(),
                OWNER.subject());
    }

    private static void profile(long epoch, long revision, String markdown) {
        sql.update(
                "UPDATE memory.owners SET profile_revision=? WHERE tenant=? AND subject=?",
                revision,
                OWNER.tenant(),
                OWNER.subject());
        sql.update(
                "INSERT INTO memory.user_profiles(tenant,subject,epoch,revision,markdown) VALUES(?,?,?,?,?)",
                OWNER.tenant(),
                OWNER.subject(),
                epoch,
                revision,
                markdown);
    }

    private static void seed(String id, long epoch) {
        sql.update(
                "INSERT INTO business.conversations(id,tenant,subject) VALUES(?,?,?)",
                id,
                OWNER.tenant(),
                OWNER.subject());
        sql.update(
                "INSERT INTO business.turns(id,conversation_id,tenant,subject,idempotency_key,request_hash,message,release_hash,status) VALUES(?,?,?,?,?,'hash','message','release','completed')",
                id,
                id,
                OWNER.tenant(),
                OWNER.subject(),
                id);
        sql.update(
                "INSERT INTO memory.sources(id,tenant,subject,epoch,content,status) VALUES(?,?,?,?,?,'pending')",
                id,
                OWNER.tenant(),
                OWNER.subject(),
                epoch,
                "private-" + id);
        sql.update(
                "INSERT INTO memory.facts(id,source_id,tenant,subject,epoch,key,content,evidence,status) VALUES(?,?,?,?,?,?,?,?,'active')",
                id,
                id,
                OWNER.tenant(),
                OWNER.subject(),
                epoch,
                id,
                "private-" + id,
                "quote-" + id);
        sql.update(
                "INSERT INTO memory.projections(fact_id,tenant,subject,epoch,revision,model_revision,dimensions,embedding) VALUES(?,?,?,?,1,'test',1024,array_fill(1::real,ARRAY[1024]))",
                id,
                OWNER.tenant(),
                OWNER.subject(),
                epoch);
        sql.update(
                "INSERT INTO memory.recalls(turn_id,fact_id,revision,epoch) VALUES(?,?,1,?)",
                id,
                id,
                epoch);
        sql.update(
                "INSERT INTO memory.jobs(kind,key_id,tenant,subject,epoch,status,lease_token,lease_until) VALUES('index',?,?,?,?,'running','test-lease',clock_timestamp()+interval '1 hour')",
                id,
                OWNER.tenant(),
                OWNER.subject(),
                epoch);
    }
}
