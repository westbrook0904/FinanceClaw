package com.financeclaw.p1;

import static org.assertj.core.api.Assertions.*;

import com.financeclaw.app.release.ReleaseService;
import com.financeclaw.app.task.CancelTaskService;
import com.financeclaw.app.task.SubmitTaskService;
import com.financeclaw.bootstrap.FinanceClawApplication;
import com.financeclaw.domain.ExecutionScope;
import com.financeclaw.domain.Task.*;
import com.financeclaw.domain.release.ReleaseDefinition;
import com.financeclaw.infrastructure.release.BusinessConfigurationLoader;
import com.financeclaw.support.PayloadJson;
import java.util.*;
import java.util.concurrent.*;
import javax.sql.DataSource;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.boot.test.context.SpringBootTest;
import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.test.context.DynamicPropertyRegistry;
import org.springframework.test.context.DynamicPropertySource;
import org.testcontainers.containers.PostgreSQLContainer;

/** Real admission transactions keep a conversation's release fixed across publications. */
@SpringBootTest(
        classes = FinanceClawApplication.class,
        properties = {"financeclaw.worker.enabled=false", "financeclaw.integrations.enabled=false"})
class ConversationReleaseIT {
    private static final PostgreSQLContainer<?> PG =
            new PostgreSQLContainer<>("postgres:16.10-alpine");

    static {
        PG.start();
    }

    @DynamicPropertySource
    static void database(DynamicPropertyRegistry properties) {
        properties.add("spring.datasource.url", PG::getJdbcUrl);
        properties.add("spring.datasource.username", PG::getUsername);
        properties.add("spring.datasource.password", PG::getPassword);
    }

    @Autowired SubmitTaskService tasks;
    @Autowired CancelTaskService cancellations;
    @Autowired ReleaseService releases;
    @Autowired DataSource source;
    private final Owner owner = new Owner("p1-tenant", "p1-owner");
    private JdbcTemplate sql;

    @BeforeEach
    void resetAdmissions() {
        sql = new JdbcTemplate(source);
        sql.execute("TRUNCATE business.conversations,business.admission CASCADE");
        sql.execute("TRUNCATE memory.owners,memory.jobs,memory.operations,memory.audit CASCADE");
    }

    @Test
    void creationPinsFirstAndLaterTurnsWhileNewConversationsUseTheNewRelease() {
        String first = publish("first", ReleaseDefinition.SCOPES);
        String original = tasks.createConversation(owner);
        assertThat(binding(original)).isEqualTo(first);
        assertThat(grant(original)).containsExactlyElementsOf(ReleaseDefinition.SCOPES);

        String second = publish("second", List.of("catalog:read"));
        var firstTurn = tasks.submit(owner, original, "first-turn", "first request");
        assertThat(turnRelease(firstTurn)).isEqualTo(first);
        cancellations.cancel(owner, original, firstTurn.turnId(), "cancel-first");
        var nextTurn = tasks.submit(owner, original, "next-turn", "next request");
        assertThat(turnRelease(nextTurn)).isEqualTo(first);
        assertThat(binding(original)).isEqualTo(first);
        assertThat(policy(nextTurn)).containsExactlyElementsOf(ReleaseDefinition.SCOPES);

        String fresh = tasks.createConversation(owner);
        var freshTurn = tasks.submit(owner, fresh, "fresh-turn", "fresh request");
        assertThat(binding(fresh)).isEqualTo(second);
        assertThat(turnRelease(freshTurn)).isEqualTo(second);
        assertThat(grant(fresh)).containsExactly("catalog:read");
        assertThat(policy(freshTurn)).containsExactly("catalog:read");
    }

    @Test
    void legacyEmptyConversationBindsAtFirstAdmissionAndReplayNeverRebinds() {
        publish("before-empty", ReleaseDefinition.SCOPES);
        String conversation = legacyConversation(null, true);
        assertThat(binding(conversation)).isNull();
        String first = publish("first-use", ReleaseDefinition.SCOPES);
        var accepted = tasks.submit(owner, conversation, "same-key", "same request");
        assertThat(binding(conversation)).isEqualTo(first);
        assertThat(turnRelease(accepted)).isEqualTo(first);

        publish("after-first-use", List.of("catalog:read"));
        var before = admissionRows();
        var replay = tasks.submit(owner, conversation, "same-key", "same request");
        assertThat(replay.turnId()).isEqualTo(accepted.turnId());
        assertThat(replay.idempotentReplay()).isTrue();
        assertThat(binding(conversation)).isEqualTo(first);
        assertThat(admissionRows()).isEqualTo(before);
        assertThatThrownBy(() -> tasks.submit(owner, conversation, "same-key", "changed request"))
                .hasMessage("idempotency_conflict");
        assertThat(binding(conversation)).isEqualTo(first);
        assertThat(admissionRows()).isEqualTo(before);
    }

    @Test
    void foreignOwnerCannotBindAnUnusedConversationOrReadItsAdmissionReceipt() {
        publish("owner-boundary", ReleaseDefinition.SCOPES);
        String conversation = legacyConversation(null, true);
        var before = admissionRows();
        for (var other :
                List.of(
                        new Owner(owner.tenant(), "other-owner"),
                        new Owner("other-tenant", owner.subject()))) {
            assertThatThrownBy(() -> tasks.submit(other, conversation, "same-key", "request"))
                    .isInstanceOf(Problem.class)
                    .hasMessage("not_found");
            assertThat(binding(conversation)).isNull();
            assertThat(admissionRows()).isEqualTo(before);
        }

        tasks.submit(owner, conversation, "same-key", "request");
        String bound = binding(conversation);
        var acceptedRows = admissionRows();
        assertThatThrownBy(
                        () ->
                                tasks.submit(
                                        new Owner(owner.tenant(), "other-owner"),
                                        conversation,
                                        "same-key",
                                        "request"))
                .hasMessage("not_found");
        assertThat(binding(conversation)).isEqualTo(bound);
        assertThat(admissionRows()).isEqualTo(acceptedRows);
    }

    @Test
    void missingHistoricalReleaseBlocksNewWorkWithoutChangingTheBindingOrAdmissionRows() {
        publish("available-current", ReleaseDefinition.SCOPES);
        String missing = "e".repeat(64);
        assertThat(
                        sql.queryForObject(
                                "SELECT count(*) FROM control_plane.release_manifests WHERE release_hash=?",
                                Long.class,
                                missing))
                .isZero();
        String conversation = legacyConversation(missing, true);
        var before = admissionRows();

        assertThatThrownBy(() -> tasks.submit(owner, conversation, "new-key", "new request"))
                .hasMessage("release_unavailable");
        assertThat(binding(conversation)).isEqualTo(missing);
        assertThat(admissionRows()).isEqualTo(before);
    }

    @Test
    void unavailableHistoricalReleaseDoesNotPreventAnExistingIdempotentReceipt() {
        publish("replay-current", ReleaseDefinition.SCOPES);
        String missing = "f".repeat(64);
        assertThat(
                        sql.queryForObject(
                                "SELECT count(*) FROM control_plane.release_manifests WHERE release_hash=?",
                                Long.class,
                                missing))
                .isZero();
        String conversation = legacyConversation(missing, true);
        String turn = UUID.randomUUID().toString();
        sql.update(
                """
                INSERT INTO business.turns(
                  id,conversation_id,tenant,subject,idempotency_key,request_hash,message,
                  release_hash,status)
                VALUES(?,?,?,?,?,?,?,?,'completed')
                """,
                turn,
                conversation,
                owner.tenant(),
                owner.subject(),
                "old-key",
                ExecutionScope.hash(List.of("old request")),
                "old request",
                missing);
        var before = admissionRows();

        var replay = tasks.submit(owner, conversation, "old-key", "old request");
        assertThat(replay.turnId()).isEqualTo(turn);
        assertThat(replay.idempotentReplay()).isTrue();
        assertThat(binding(conversation)).isEqualTo(missing);
        assertThat(admissionRows()).isEqualTo(before);
        assertThatThrownBy(() -> tasks.submit(owner, conversation, "new-key", "new request"))
                .hasMessage("release_unavailable");
        assertThat(admissionRows()).isEqualTo(before);
    }

    @Test
    void failedFirstAdmissionRollsBackTheLegacyBindingAndEveryAdmissionWrite() {
        publish("rollback-first-use", ReleaseDefinition.SCOPES);
        String conversation = legacyConversation(null, false);
        var before = admissionRows();

        assertThatThrownBy(() -> tasks.submit(owner, conversation, "failed-key", "request"))
                .hasMessage("grant_unavailable");
        assertThat(binding(conversation)).isNull();
        assertThat(admissionRows()).isEqualTo(before);
    }

    @Test
    void concurrentFirstAdmissionBindsOnceAndReturnsOneDurableReceipt() throws Exception {
        String current = publish("concurrent-first-use", ReleaseDefinition.SCOPES);
        String conversation = legacyConversation(null, true);
        var barrier = new CyclicBarrier(2);
        try (var executor = Executors.newFixedThreadPool(2)) {
            Callable<Accepted> submit =
                    () -> {
                        barrier.await(10, TimeUnit.SECONDS);
                        return tasks.submit(owner, conversation, "same-key", "request");
                    };
            var left = executor.submit(submit);
            var right = executor.submit(submit);
            var receipts = List.of(left.get(15, TimeUnit.SECONDS), right.get(15, TimeUnit.SECONDS));
            assertThat(receipts)
                    .extracting(Accepted::turnId)
                    .containsOnly(receipts.getFirst().turnId());
            assertThat(receipts)
                    .extracting(Accepted::idempotentReplay)
                    .containsExactlyInAnyOrder(false, true);
        }
        assertThat(binding(conversation)).isEqualTo(current);
        assertThat(count("business.turns")).isOne();
        assertThat(count("business.commands")).isOne();
        assertThat(count("business.turn_policies")).isOne();
        assertThat(count("business.journal")).isOne();
        assertThat(count("business.events")).isOne();
    }

    private String publish(String version, List<String> scopes) {
        releases.publish(
                new ReleaseDefinition(
                        "p1-" + version + "-" + UUID.randomUUID(),
                        "finance_agent",
                        scopes,
                        ReleaseDefinition.OFFLINE,
                        null,
                        12,
                        32,
                        BusinessConfigurationLoader.defaults()));
        return releases.currentHash();
    }

    private String legacyConversation(String release, boolean withGrant) {
        String id = UUID.randomUUID().toString();
        sql.update(
                "INSERT INTO business.conversations(id,tenant,subject,release_hash) VALUES(?,?,?,?)",
                id,
                owner.tenant(),
                owner.subject(),
                release);
        if (withGrant) {
            sql.update(
                    "INSERT INTO business.grants(conversation_id,scopes) VALUES(?,?::jsonb)",
                    id,
                    PayloadJson.canonical(ReleaseDefinition.SCOPES));
        }
        return id;
    }

    private String binding(String conversation) {
        return sql.queryForObject(
                "SELECT release_hash FROM business.conversations WHERE id=?",
                String.class,
                conversation);
    }

    private String turnRelease(Accepted turn) {
        return sql.queryForObject(
                "SELECT release_hash FROM business.turns WHERE id=?", String.class, turn.turnId());
    }

    private List<String> grant(String conversation) {
        return PayloadJson.scopes(
                sql.queryForObject(
                        "SELECT scopes FROM business.grants WHERE conversation_id=?",
                        String.class,
                        conversation));
    }

    private List<String> policy(Accepted turn) {
        return PayloadJson.scopes(
                sql.queryForObject(
                        "SELECT scopes FROM business.turn_policies WHERE turn_id=?",
                        String.class,
                        turn.turnId()));
    }

    private long count(String table) {
        return sql.queryForObject("SELECT count(*) FROM " + table, Long.class);
    }

    private Map<String, Long> admissionRows() {
        var counts = new LinkedHashMap<String, Long>();
        for (String table :
                List.of(
                        "business.admission",
                        "business.turns",
                        "business.commands",
                        "business.attempts",
                        "business.turn_policies",
                        "business.journal",
                        "business.events",
                        "memory.owners",
                        "memory.sources",
                        "memory.turn_snapshots",
                        "memory.jobs")) {
            counts.put(table, count(table));
        }
        return counts;
    }
}
