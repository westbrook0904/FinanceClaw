package com.financeclaw.p3;

import static org.assertj.core.api.Assertions.*;

import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import com.financeclaw.app.memory.MemoryService;
import com.financeclaw.app.memory.port.ForgetRecoveryRepository;
import com.financeclaw.bootstrap.FinanceClawApplication;
import com.financeclaw.domain.ExecutionScope;
import com.financeclaw.domain.Task.*;
import com.financeclaw.domain.memory.ForgetFact;
import com.financeclaw.domain.research.ResearchRun;
import com.financeclaw.infrastructure.state.MyBatisAgentStateStore;
import com.financeclaw.infrastructure.state.RuntimeMapper;
import io.agentscope.core.state.AgentState;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.ArrayList;
import java.util.List;
import java.util.UUID;
import javax.sql.DataSource;
import org.junit.jupiter.api.*;
import org.junit.jupiter.params.ParameterizedTest;
import org.junit.jupiter.params.provider.ValueSource;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.boot.test.context.SpringBootTest;
import org.springframework.jdbc.core.ConnectionCallback;
import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.test.context.DynamicPropertyRegistry;
import org.springframework.test.context.DynamicPropertySource;
import org.springframework.transaction.support.TransactionTemplate;
import org.testcontainers.containers.PostgreSQLContainer;

/** 真实 PostgreSQL 遗忘和隔离恢复：执行正文擦除、所有尝试的检查点墓碑及跨语言 scope 身份。 */
@SpringBootTest(
        classes = FinanceClawApplication.class,
        properties = {
            "financeclaw.worker.enabled=false",
            "financeclaw.integrations.enabled=false",
            "financeclaw.memory.enabled=false",
            "financeclaw.memory.profile-enabled=true",
            "financeclaw.memory.profile-directory=",
            "financeclaw.memory.forget-ledger.enabled=false"
        })
class ExecutionPrivacyIT {
    private static final PostgreSQLContainer<?> PG =
            new PostgreSQLContainer<>("postgres:16.10-alpine");
    private static final Owner ALICE = new Owner("租户-甲", "用户-甲😀");
    private static final String CALL = "研究:行情😀";
    private static final String LEGACY_CALL = "legacy:unbound:call";
    private static final String INPUT_HASH = "a".repeat(64);
    private static final String PRIVATE = "execution-private-sentinel";

    static {
        PG.start();
    }

    @DynamicPropertySource
    static void database(DynamicPropertyRegistry registry) {
        registry.add("spring.datasource.url", PG::getJdbcUrl);
        registry.add("spring.datasource.username", PG::getUsername);
        registry.add("spring.datasource.password", PG::getPassword);
    }

    @Autowired MemoryService memory;
    @Autowired ForgetRecoveryRepository recovery;
    @Autowired RuntimeMapper states;
    @Autowired TransactionTemplate tx;
    @Autowired DataSource source;
    private JdbcTemplate sql;

    @AfterAll
    static void stopDatabase() {
        PG.stop();
    }

    @BeforeEach
    void reset() {
        sql = new JdbcTemplate(source);
        // SDK scope hashes have no business FK. Reset them explicitly before truncating authority.
        sql.execute(
                "TRUNCATE agent_runtime.states,business.conversations,memory.owners,memory.jobs,memory.operations,memory.audit CASCADE");
        sql.update(
                "UPDATE memory.privacy_ledger_binding SET lineage=?,active=false",
                UUID.randomUUID().toString());
    }

    @Test
    void sqlScopeHashMatchesJavaUtf8LengthPrefixesInsteadOfAmbiguousConcatenation() {
        for (var parts :
                List.of(
                        List.<String>of(),
                        List.of(""),
                        List.of("a", "bc"),
                        List.of("ab", "c"),
                        List.of("租户-甲", "用户😀", "分隔:符|与空格", ""),
                        List.of("financeclaw-state-v1", "tenant", "subject", "agent", "release"))) {
            String actual =
                    sql.execute(
                            (ConnectionCallback<String>)
                                    connection -> {
                                        var array =
                                                connection.createArrayOf(
                                                        "text", parts.toArray(String[]::new));
                                        try (var statement =
                                                connection.prepareStatement(
                                                        "SELECT agent_runtime.scope_hash(?)")) {
                                            statement.setArray(1, array);
                                            try (var result = statement.executeQuery()) {
                                                assertThat(result.next()).isTrue();
                                                return result.getString(1);
                                            }
                                        } finally {
                                            array.free();
                                        }
                                    });
            assertThat(actual)
                    .as("UTF-8 length-prefixed scope digest")
                    .isEqualTo(ExecutionScope.hash(parts));
        }
        assertThat(ExecutionScope.hash(List.of("a", "bc")))
                .isNotEqualTo(ExecutionScope.hash(List.of("ab", "c")));
    }

    @Test
    void executionScopesIncludeEveryAttemptAndUnboundLegacyChildWithoutInventingBudget() {
        var seeded = seed(ALICE, 1, 2);
        var actual =
                sql.queryForList(
                        "SELECT namespace||':'||user_id||':'||session_id FROM agent_runtime.execution_scopes() WHERE root_turn_id=?",
                        String.class,
                        seeded.turn());
        assertThat(actual)
                .containsExactlyInAnyOrderElementsOf(
                        seeded.scopes().stream().map(ExecutionPrivacyIT::scopeKey).toList());
        assertThat(
                        sql.queryForObject(
                                "SELECT count(*) FROM agent_runtime.execution_scopes() WHERE root_turn_id=? AND NOT invocation_bound",
                                Integer.class,
                                seeded.turn()))
                .isEqualTo(2);
        assertThat(count("business.child_invocations", seeded.turn())).isOne();
        assertThat(recovery.unsafeRows())
                .as("unbound child metadata must not certify a runnable invocation")
                .isPositive();

        assertThat(memory.forget(ALICE, 1, "forget-all-attempts")).isEqualTo(2);
        assertErased(seeded);
        assertThat(
                        sql.queryForObject(
                                "SELECT count(*) FROM agent_runtime.states WHERE NOT deleted",
                                Integer.class))
                .isZero();
        assertThat(recovery.unsafeRows()).isZero();
    }

    @Test
    void localForgetErasesOnlyTargetOwnerRuntimeReceiptsAndPrivacyBoundFiles() {
        var old = seed(ALICE, 1, 1);
        var sameTenant = seed(new Owner(ALICE.tenant(), "another-subject"), 1, 1);
        var sameSubject = seed(new Owner("another-tenant", ALICE.subject()), 1, 1);
        var preexistingTombstone =
                new RuntimeMapper.StateKey(
                        old.scopes().getFirst().namespace(),
                        old.scopes().getFirst().userId(),
                        old.scopes().getFirst().sessionId(),
                        "already-deleted");
        sql.update(
                "INSERT INTO agent_runtime.states(namespace,user_id,session_id,state_key,version,payload,deleted) VALUES(?,?,?,'already-deleted',19,NULL,true)",
                preexistingTombstone.namespace(),
                preexistingTombstone.user(),
                preexistingTombstone.session());

        assertThat(memory.forget(ALICE, 1, "erase-local-runtime")).isEqualTo(2);

        assertErased(old);
        assertRetained(sameTenant);
        assertRetained(sameSubject);
        assertThat(states.getState(preexistingTombstone).version()).isEqualTo(19);
        assertThat(states.getState(preexistingTombstone).deleted()).isTrue();
        assertThat(memory.forget(ALICE, 1, "erase-local-runtime")).isEqualTo(2);
        assertErased(old);
    }

    @Test
    void authoritativeForgetReplayPreservesNewEpochReceiptsAndExistingTombstoneVersions() {
        var old = seed(ALICE, 1, 2);
        var fact = fact(ALICE, 1);
        locked(
                ALICE,
                () -> {
                    recovery.prepare(fact);
                    recovery.apply(fact);
                    recovery.complete(ALICE);
                });
        assertErased(old);
        var current = seed(ALICE, 2, 1, false);

        locked(
                ALICE,
                () -> {
                    recovery.apply(fact);
                    recovery.complete(ALICE);
                });

        assertErased(old);
        assertRetained(current);
        assertThat(memory.privacy(ALICE).epoch()).isEqualTo(2);
        assertThat(sql.queryForObject("SELECT count(*) FROM memory.forget_applied", Integer.class))
                .isOne();
        assertThat(sql.queryForObject("SELECT count(*) FROM memory.operations", Integer.class))
                .isOne();
        // Current-epoch bound receipts remain safe after replaying the older fact.
        assertThat(recovery.unsafeRows()).isZero();
    }

    @ParameterizedTest
    @ValueSource(strings = {"local", "ledger-replay"})
    void erasureFailureRollsBackCheckpointsReceiptsMetadataFilesAndEpochTogether(String entry) {
        var seeded = seed(ALICE, 1, 1);
        var fact = fact(ALICE, 1);
        if (entry.equals("ledger-replay")) locked(ALICE, () -> recovery.prepare(fact));
        sql.execute(
                "CREATE FUNCTION public.reject_runtime_erasure() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN RAISE EXCEPTION 'synthetic erasure failure'; END $$");
        sql.execute(
                "CREATE TRIGGER reject_runtime_erasure BEFORE DELETE ON business.capability_steps FOR EACH ROW EXECUTE FUNCTION public.reject_runtime_erasure()");
        try {
            assertThatThrownBy(
                            () -> {
                                if (entry.equals("local"))
                                    memory.forget(ALICE, 1, "rollback-runtime");
                                else locked(ALICE, () -> recovery.apply(fact));
                            })
                    .hasStackTraceContaining("synthetic erasure failure");
        } finally {
            sql.execute("DROP TRIGGER reject_runtime_erasure ON business.capability_steps");
            sql.execute("DROP FUNCTION public.reject_runtime_erasure()");
        }
        assertRetained(seeded);
        assertThat(memory.privacy(ALICE).epoch()).isEqualTo(1);
        assertThat(
                        sql.queryForObject(
                                "SELECT content FROM memory.sources WHERE id=?",
                                String.class,
                                seeded.turn()))
                .isEqualTo(PRIVATE);
        assertThat(sql.queryForObject("SELECT count(*) FROM memory.operations", Integer.class))
                .isZero();
        assertThat(sql.queryForObject("SELECT count(*) FROM memory.forget_applied", Integer.class))
                .isZero();
        assertThat(sql.queryForObject("SELECT count(*) FROM memory.audit", Integer.class)).isZero();
        if (entry.equals("ledger-replay")) {
            assertThat(recovery.pending(ALICE)).isEqualTo(fact);
            assertThat(memory.privacy(ALICE).purgePending()).isTrue();
        } else {
            assertThat(recovery.pending(ALICE)).isNull();
            assertThat(memory.privacy(ALICE).purgePending()).isFalse();
        }
    }

    @ParameterizedTest
    @ValueSource(
            strings = {
                "missing-source",
                "wrong-source-owner",
                "future-source",
                "forgotten-source",
                "missing-owner"
            })
    void restoreCannotCertifyRuntimeResiduesWithMissingOrInconsistentAuthority(String corruption)
            throws Exception {
        var seeded = seed(ALICE, 1, 1);
        // Isolate the authority fault from the separately covered unbound legacy-child fault.
        sql.update(
                "INSERT INTO business.child_invocations(root_turn_id,call_id,input_hash) VALUES(?,?,?)",
                seeded.turn(),
                LEGACY_CALL,
                INPUT_HASH);
        assertThat(recovery.unsafeRows()).isZero();
        switch (corruption) {
            case "missing-source" ->
                    sql.update("DELETE FROM memory.sources WHERE id=?", seeded.turn());
            case "wrong-source-owner" ->
                    sql.update(
                            "UPDATE memory.sources SET subject='wrong-owner' WHERE id=?",
                            seeded.turn());
            case "future-source" ->
                    sql.update("UPDATE memory.sources SET epoch=2 WHERE id=?", seeded.turn());
            case "forgotten-source" ->
                    sql.update(
                            "UPDATE memory.sources SET status='forgotten',content=NULL WHERE id=?",
                            seeded.turn());
            case "missing-owner" ->
                    sql.update(
                            "DELETE FROM memory.owners WHERE tenant=? AND subject=?",
                            ALICE.tenant(),
                            ALICE.subject());
            default -> throw new AssertionError();
        }
        assertThat(
                        sql.queryForList(
                                "SELECT DISTINCT kind FROM agent_runtime.unsafe_execution_data()",
                                String.class))
                .contains("sdk_states", "capability_steps", "child_invocations", "tool_artifacts");
        assertThat(recovery.unsafeRows()).isPositive();
        var exported = snapshot();
        assertThat(exported.path("schema_version").asInt()).isEqualTo(3);
        assertThat(exported.toString()).doesNotContain(PRIVATE, INPUT_HASH);
        if (corruption.equals("missing-owner")) {
            assertThat(exported.path("orphan_owners").asLong()).isPositive();
        } else {
            var owner = exported.path("owners").get(0);
            for (String counter :
                    List.of(
                            "unsafe_sdk_states",
                            "unsafe_capability_steps",
                            "unsafe_child_invocations",
                            "unsafe_tool_artifacts"))
                assertThat(owner.path(counter).asLong()).as(counter).isPositive();
        }
    }

    @Test
    void unknownLiveSdkStateBlocksRestoreButAnUnknownTombstoneDoesNot() throws Exception {
        sql.update(
                "INSERT INTO agent_runtime.states(namespace,user_id,session_id,state_key,version,payload) VALUES('unknown-namespace','unknown-user','unknown-session','agent_state',3,CAST(? AS jsonb))",
                "{\"private\":\"" + PRIVATE + "\"}");
        assertThat(recovery.unsafeRows()).isPositive();
        var exported = snapshot();
        assertThat(exported.path("orphan_runtime_rows").asLong()).isOne();
        assertThat(exported.toString()).doesNotContain(PRIVATE, "unknown-namespace");
        sql.update("UPDATE agent_runtime.states SET deleted=true,payload=NULL,version=version+1");
        assertThat(recovery.unsafeRows()).isZero();
        assertThat(snapshot().path("orphan_runtime_rows").asLong()).isZero();
    }

    @Test
    void unknownLiveScopeForTheForgettingOwnerKeepsTheWholeForgetTransactionBlocked() {
        var seeded = seed(ALICE, 1, 1, false);
        sql.update(
                "INSERT INTO agent_runtime.states(namespace,user_id,session_id,state_key,version,payload) VALUES('unmapped-owner-scope',?,'unknown-session','agent_state',4,'{}')",
                ExecutionScope.hash(List.of(ALICE.tenant(), ALICE.subject())));

        assertThatThrownBy(() -> memory.forget(ALICE, 1, "unknown-owner-scope"))
                .hasStackTraceContaining("execution_privacy_residuals");
        assertRetained(seeded);
        assertThat(memory.privacy(ALICE).epoch()).isEqualTo(1);
        assertThat(sql.queryForObject("SELECT count(*) FROM memory.operations", Integer.class))
                .isZero();
        assertThat(sql.queryForObject("SELECT count(*) FROM memory.audit", Integer.class)).isZero();
        // Explicit administrative tombstoning removes this unknown body; a new authorized retry
        // then erases the remaining scopes without treating a tombstone as an unsafe live state.
        sql.update(
                "UPDATE agent_runtime.states SET deleted=true,payload=NULL,version=version+1 WHERE namespace='unmapped-owner-scope'");
        assertThat(memory.forget(ALICE, 1, "unknown-owner-scope")).isEqualTo(2);
        assertErased(seeded);
    }

    @Test
    void erasedSdkScopeRejectsBothStaleCasAndFreshCreationWithoutChangingItsTombstone() {
        var seeded = seed(ALICE, 1, 1);
        var scope = seeded.scopes().getFirst();
        var store = new MyBatisAgentStateStore(states, scope);
        assertThat(memory.forget(ALICE, 1, "no-state-resurrection")).isEqualTo(2);
        assertThatThrownBy(
                        () ->
                                store.getVersioned(
                                        scope.userId(),
                                        scope.sessionId(),
                                        "agent_state",
                                        AgentState.class))
                .hasMessageContaining("deleted");
        assertThat(
                        store.saveIfVersion(
                                scope.userId(),
                                scope.sessionId(),
                                "agent_state",
                                AgentState.builder().build(),
                                7))
                .isEqualTo(-1);
        assertThat(
                        store.saveIfVersion(
                                scope.userId(),
                                scope.sessionId(),
                                "agent_state",
                                AgentState.builder().build(),
                                0))
                .isEqualTo(-1);
        assertErased(seeded);
    }

    private Seed seed(Owner owner, long epoch, int attempts) {
        return seed(owner, epoch, attempts, true);
    }

    private Seed seed(Owner owner, long epoch, int attempts, boolean legacy) {
        String id = UUID.randomUUID().toString();
        String command = id + "-command";
        sql.update(
                "INSERT INTO memory.owners(tenant,subject,epoch) VALUES(?,?,?) ON CONFLICT(tenant,subject) DO NOTHING",
                owner.tenant(),
                owner.subject(),
                epoch);
        sql.update(
                "INSERT INTO business.conversations(id,tenant,subject) VALUES(?,?,?)",
                id,
                owner.tenant(),
                owner.subject());
        sql.update(
                "INSERT INTO business.turns(id,conversation_id,tenant,subject,idempotency_key,request_hash,message,release_hash,status) VALUES(?,?,?,?,?,'hash','request','release-版本😀','completed')",
                id,
                id,
                owner.tenant(),
                owner.subject(),
                id);
        sql.update(
                "INSERT INTO memory.sources(id,tenant,subject,epoch,content) VALUES(?,?,?,?,?)",
                id,
                owner.tenant(),
                owner.subject(),
                epoch,
                PRIVATE);
        sql.update(
                "INSERT INTO business.commands(id,turn_id,status) VALUES(?,?,'done')", command, id);
        sql.update(
                "INSERT INTO business.child_invocations(root_turn_id,call_id,input_hash,model_calls,tool_calls) VALUES(?,?,?,2,1)",
                id,
                CALL,
                INPUT_HASH);
        sql.update(
                "INSERT INTO business.capability_steps(turn_id,namespace,step,input_hash,result) VALUES(?,'portfolio','read','hash',?),(?,?,'tool:quote',?,?)",
                id,
                PRIVATE,
                id,
                "child:" + CALL,
                INPUT_HASH,
                PRIVATE);
        if (legacy)
            sql.update(
                    "INSERT INTO business.capability_steps(turn_id,namespace,step,input_hash,result) VALUES(?,?,'tool:legacy',?,?)",
                    id,
                    "child:" + LEGACY_CALL,
                    INPUT_HASH,
                    PRIVATE);
        for (boolean bound : List.of(false, true)) {
            sql.update(
                    "INSERT INTO business.artifacts(id,turn_id,conversation_id,tenant,subject,release_hash,hash,payload,bytes,privacy_bound) VALUES(?,?,?,?,?,'release-版本😀','hash',?,?,?)",
                    id + (bound ? "-private" : "-public"),
                    id,
                    id,
                    owner.tenant(),
                    owner.subject(),
                    PRIVATE,
                    PRIVATE.length(),
                    bound);
        }
        var scopes = new ArrayList<ExecutionScope>();
        for (int n = 1; n <= attempts; n++) {
            String attempt = id + "-attempt-" + n;
            sql.update(
                    "INSERT INTO business.attempts(id,command_id,fence) VALUES(?,?,?)",
                    attempt,
                    command,
                    n);
            sql.update(
                    "UPDATE business.commands SET attempt_id=?,fence=? WHERE id=?",
                    attempt,
                    n,
                    command);
            var turn =
                    new Turn(
                            id,
                            id,
                            owner.tenant(),
                            owner.subject(),
                            id,
                            "hash",
                            "request",
                            "release-版本😀",
                            "completed",
                            1,
                            null,
                            null);
            var root =
                    new Claim(new Command(command, id, "done", attempt, n, false, null), turn)
                            .scope();
            scopes.add(root);
            scopes.add(ResearchRun.scope(root, CALL));
            if (legacy) scopes.add(ResearchRun.scope(root, LEGACY_CALL));
        }
        for (var scope : scopes) {
            sql.update(
                    "INSERT INTO agent_runtime.states(namespace,user_id,session_id,state_key,version,payload) VALUES(?,?,?,'agent_state',7,CAST(? AS jsonb))",
                    scope.namespace(),
                    scope.userId(),
                    scope.sessionId(),
                    "{\"private\":\"" + PRIVATE + "\"}");
        }
        return new Seed(owner, epoch, id, List.copyOf(scopes), legacy);
    }

    private void assertErased(Seed seeded) {
        assertThat(count("business.capability_steps", seeded.turn())).isZero();
        assertThat(count("business.child_invocations", seeded.turn())).isZero();
        assertThat(
                        sql.queryForList(
                                "SELECT privacy_bound FROM business.artifacts WHERE turn_id=?",
                                Boolean.class,
                                seeded.turn()))
                .containsExactly(false);
        for (var scope : seeded.scopes()) {
            var row = states.getState(key(scope));
            assertThat(row)
                    .as("tombstone retained for every historical execution scope")
                    .isNotNull();
            assertThat(row.deleted()).isTrue();
            assertThat(row.payload()).isNull();
            assertThat(row.version()).isEqualTo(8);
        }
    }

    private void assertRetained(Seed seeded) {
        assertThat(count("business.capability_steps", seeded.turn()))
                .isEqualTo(seeded.legacy() ? 3 : 2);
        assertThat(count("business.child_invocations", seeded.turn())).isOne();
        assertThat(count("business.artifacts", seeded.turn())).isEqualTo(2);
        for (var scope : seeded.scopes()) {
            var row = states.getState(key(scope));
            assertThat(row.deleted()).isFalse();
            assertThat(row.payload()).contains(PRIVATE);
            assertThat(row.version()).isEqualTo(7);
        }
    }

    private long count(String table, String turn) {
        String key = table.equals("business.child_invocations") ? "root_turn_id" : "turn_id";
        return sql.queryForObject(
                "SELECT count(*) FROM " + table + " WHERE " + key + "=?", Long.class, turn);
    }

    private void locked(Owner owner, Runnable action) {
        tx.executeWithoutResult(
                status -> {
                    sql.queryForObject(
                            "SELECT epoch FROM memory.owners WHERE tenant=? AND subject=? FOR UPDATE",
                            Long.class,
                            owner.tenant(),
                            owner.subject());
                    action.run();
                });
    }

    private ForgetFact fact(Owner owner, long epoch) {
        return new ForgetFact(
                UUID.randomUUID().toString(),
                recovery.binding().lineage(),
                owner,
                epoch,
                UUID.randomUUID().toString(),
                ForgetFact.hash(epoch));
    }

    private static String scopeKey(ExecutionScope scope) {
        return scope.namespace() + ":" + scope.userId() + ":" + scope.sessionId();
    }

    private static RuntimeMapper.StateKey key(ExecutionScope scope) {
        return new RuntimeMapper.StateKey(
                scope.namespace(), scope.userId(), scope.sessionId(), "agent_state");
    }

    private static JsonNode snapshot() throws Exception {
        var result =
                PG.execInContainer(
                        "psql",
                        "-X",
                        "-qAt",
                        "-v",
                        "ON_ERROR_STOP=1",
                        "-U",
                        PG.getUsername(),
                        "-d",
                        PG.getDatabaseName(),
                        "-c",
                        Files.readString(Path.of("ops/restore-privacy-snapshot.sql")));
        assertThat(result.getExitCode()).as("read-only recovery snapshot export").isZero();
        return new ObjectMapper().readTree(result.getStdout());
    }

    private record Seed(
            Owner owner, long epoch, String turn, List<ExecutionScope> scopes, boolean legacy) {}
}
