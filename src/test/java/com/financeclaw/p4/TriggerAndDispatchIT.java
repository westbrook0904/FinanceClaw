package com.financeclaw.p4;

import static org.assertj.core.api.Assertions.*;

import com.financeclaw.app.execution.ExecutionLeaseService;
import com.financeclaw.app.release.ReleaseService;
import com.financeclaw.app.scheduling.DispatchPolicyService;
import com.financeclaw.app.scheduling.TriggerService;
import com.financeclaw.app.task.SubmitTaskService;
import com.financeclaw.bootstrap.FinanceClawApplication;
import com.financeclaw.domain.Task.Owner;
import com.financeclaw.domain.Task.Problem;
import com.financeclaw.domain.release.ReleaseDefinition;
import com.financeclaw.domain.scheduling.ConcurrencyLimit;
import com.financeclaw.domain.scheduling.DispatchPolicy;
import com.financeclaw.domain.scheduling.TriggerRule;
import com.financeclaw.infrastructure.agentscope.RuntimeArtifacts;
import java.time.Instant;
import java.util.List;
import java.util.UUID;
import java.util.concurrent.CountDownLatch;
import java.util.concurrent.Executors;
import java.util.concurrent.TimeUnit;
import javax.sql.DataSource;
import org.junit.jupiter.api.*;
import org.springframework.boot.WebApplicationType;
import org.springframework.boot.builder.SpringApplicationBuilder;
import org.springframework.context.ConfigurableApplicationContext;
import org.springframework.jdbc.core.JdbcTemplate;
import org.testcontainers.containers.PostgreSQLContainer;

/**
 * Real PostgreSQL and normal admission/claim services; scheduler threads are deliberately disabled.
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class TriggerAndDispatchIT {
    private final PostgreSQLContainer<?> pg = new PostgreSQLContainer<>("postgres:16.10-alpine");
    private final Owner alice = new Owner("scheduler-it", "alice");
    private final Owner bob = new Owner("scheduler-it", "bob");
    private ConfigurableApplicationContext app;
    private JdbcTemplate sql;
    private TriggerService triggers;
    private DispatchPolicyService policies;
    private SubmitTaskService submission;
    private ExecutionLeaseService leases;
    private ReleaseService releases;

    @BeforeAll
    void start() {
        pg.start();
        app =
                new SpringApplicationBuilder(FinanceClawApplication.class)
                        .web(WebApplicationType.NONE)
                        .run(
                                "--spring.datasource.url=" + pg.getJdbcUrl(),
                                "--spring.datasource.username=" + pg.getUsername(),
                                "--spring.datasource.password=" + pg.getPassword(),
                                "--spring.config.import=",
                                "--financeclaw.worker.enabled=false",
                                "--financeclaw.scheduler.enabled=false",
                                "--financeclaw.scheduler.dispatch-enabled=true",
                                "--financeclaw.feishu.enabled=false",
                                "--financeclaw.integrations.enabled=false",
                                "--financeclaw.memory.enabled=false",
                                "--financeclaw.business.hotel.enabled=false",
                                "--financeclaw.business.taibu.enabled=false");
        sql = new JdbcTemplate(app.getBean(DataSource.class));
        triggers = app.getBean(TriggerService.class);
        policies = app.getBean(DispatchPolicyService.class);
        submission = app.getBean(SubmitTaskService.class);
        leases = app.getBean(ExecutionLeaseService.class);
        releases = app.getBean(ReleaseService.class);
    }

    @AfterAll
    void stop() {
        if (app != null) app.close();
        pg.stop();
    }

    @BeforeEach
    void clear() {
        sql.execute(
                "TRUNCATE control_plane.trigger_rules,business.conversations,business.admission,business.dispatch_policies,business.dispatch_tenant_limits,business.dispatch_agent_limits,business.runtime_workers CASCADE");
        worker("worker-a", 1, "active", RuntimeArtifacts.hash());
        worker("worker-b", 1, "active", RuntimeArtifacts.hash());
    }

    private void worker(String id, int protocol, String state, String hash) {
        sql.update(
                "INSERT INTO business.runtime_workers(worker_id,protocol_version,runtime_hash,capabilities,parallelism,active_attempts,state,heartbeat_at,expires_at) VALUES(?,?,?,ARRAY['agentscope'],4,0,?,clock_timestamp(),clock_timestamp()+interval '10 minutes')",
                id,
                protocol,
                hash,
                state);
    }

    private TriggerRule.Definition definition(
            String kind, String version, String overlap, String message) {
        return new TriggerRule.Definition(
                true,
                kind,
                kind.equals("cron") ? "*/5 * * * * *" : null,
                kind.equals("cron") ? "Asia/Shanghai" : null,
                "fire_once",
                overlap,
                "finance_agent",
                version,
                message,
                0);
    }

    private String queue(Owner owner, String text) {
        String conversation = submission.createConversation(owner);
        return submission.submit(owner, conversation, UUID.randomUUID().toString(), text).turnId();
    }

    @Test
    void duplicateWebhooksAcrossThreadsProduceOneTurnAndReplayAfterDisable() throws Exception {
        String id = UUID.randomUUID().toString();
        var d = definition("webhook", "fixed", "allow", "scheduled-input");
        triggers.put(alice, id, 0, d);
        var gate = new CountDownLatch(1);
        try (var pool = Executors.newFixedThreadPool(2)) {
            var a =
                    pool.submit(
                            () -> {
                                gate.await();
                                return triggers.webhook(alice, id, "event-1");
                            });
            var b =
                    pool.submit(
                            () -> {
                                gate.await();
                                return triggers.webhook(alice, id, "event-1");
                            });
            gate.countDown();
            var first = a.get(10, TimeUnit.SECONDS);
            var second = b.get(10, TimeUnit.SECONDS);
            assertThat(first).isEqualTo(second);
            assertThat(sql.queryForObject("SELECT count(*) FROM business.turns", Integer.class))
                    .isOne();
            assertThat(sql.queryForObject("SELECT count(*) FROM business.commands", Integer.class))
                    .isOne();
            triggers.put(
                    alice,
                    id,
                    1,
                    new TriggerRule.Definition(
                            false,
                            d.kind(),
                            null,
                            null,
                            d.misfire(),
                            d.overlap(),
                            d.agentId(),
                            d.versionPolicy(),
                            d.message(),
                            0));
            assertThat(triggers.webhook(alice, id, "event-1")).isEqualTo(first);
            assertThatThrownBy(() -> triggers.webhook(alice, id, "event-2"))
                    .isInstanceOf(Problem.class)
                    .hasMessage("schedule_disabled");
            assertThatThrownBy(() -> triggers.get(bob, id)).hasMessage("not_found");
        }
    }

    @Test
    void fixedAndCurrentVersionPoliciesResolveAtTheirDeclaredBoundary() {
        String fixed = UUID.randomUUID().toString(), current = UUID.randomUUID().toString();
        String before = releases.currentHash();
        triggers.put(alice, fixed, 0, definition("webhook", "fixed", "allow", "fixed"));
        triggers.put(alice, current, 0, definition("webhook", "current", "allow", "current"));
        var old = releases.require(before);
        var newer =
                new ReleaseDefinition(
                        "schedule-" + UUID.randomUUID(),
                        old.agent(),
                        old.scopes(),
                        old.primary(),
                        old.fallback(),
                        old.maxModelCalls(),
                        old.maxToolCalls(),
                        old.business());
        String after = releases.register(newer);
        releases.activate(after, releases.current().revision());
        assertThat(triggers.webhook(alice, fixed, "first").releaseHash()).isEqualTo(before);
        assertThat(triggers.webhook(alice, current, "first").releaseHash()).isEqualTo(after);
    }

    @Test
    void skippedOverlapAndMisfireHaveReceiptsAndNoPhantomTurns() {
        String hook = UUID.randomUUID().toString();
        triggers.put(alice, hook, 0, definition("webhook", "fixed", "skip", "work"));
        assertThat(triggers.webhook(alice, hook, "one").status()).isEqualTo("accepted");
        assertThat(triggers.webhook(alice, hook, "two").status()).isEqualTo("skipped_overlap");
        String cron = UUID.randomUUID().toString();
        var d = definition("cron", "fixed", "allow", "cron");
        triggers.put(
                alice,
                cron,
                0,
                new TriggerRule.Definition(
                        true,
                        d.kind(),
                        d.expression(),
                        d.zone(),
                        "skip",
                        d.overlap(),
                        d.agentId(),
                        d.versionPolicy(),
                        d.message(),
                        0));
        sql.update(
                "UPDATE control_plane.trigger_rules SET next_at=clock_timestamp()-interval '1 hour' WHERE id=?",
                cron);
        assertThat(triggers.tick()).isTrue();
        assertThat(triggers.history(alice, cron, Instant.EPOCH, 100))
                .singleElement()
                .extracting(x -> x.status())
                .isEqualTo("skipped_misfire");
        assertThat(sql.queryForObject("SELECT count(*) FROM business.turns", Integer.class))
                .isOne();
        assertThat(triggers.tick()).isFalse();
    }

    @Test
    void failedAdmissionRollsBackConversationTurnAndReceiptAndDefersOnlyFailedRule() {
        String cron = UUID.randomUUID().toString();
        triggers.put(alice, cron, 0, definition("cron", "fixed", "allow", "cron"));
        sql.update(
                "INSERT INTO business.admission(tenant,subject,paused) VALUES(?,?,true)",
                alice.tenant(),
                alice.subject());
        sql.update(
                "UPDATE control_plane.trigger_rules SET next_at=clock_timestamp()-interval '1 second' WHERE id=?",
                cron);
        assertThatThrownBy(triggers::tick).hasMessage("admission_paused");
        assertThat(sql.queryForObject("SELECT count(*) FROM business.conversations", Integer.class))
                .isZero();
        assertThat(sql.queryForObject("SELECT count(*) FROM business.turns", Integer.class))
                .isZero();
        assertThat(triggers.history(alice, cron, Instant.EPOCH, 100)).isEmpty();
        assertThat(triggers.get(alice, cron).lastError()).isEqualTo("admission_failed");
        assertThat(triggers.tick()).isFalse();
    }

    @Test
    void catchUpMovesOneOccurrenceAndCompetingSchedulersCannotDuplicateIt() throws Exception {
        String cron = UUID.randomUUID().toString();
        var d = definition("cron", "fixed", "allow", "catch-up");
        triggers.put(
                alice,
                cron,
                0,
                new TriggerRule.Definition(
                        true,
                        d.kind(),
                        d.expression(),
                        d.zone(),
                        "catch_up",
                        d.overlap(),
                        d.agentId(),
                        d.versionPolicy(),
                        d.message(),
                        0));
        sql.update(
                "UPDATE control_plane.trigger_rules SET next_at=clock_timestamp()-interval '30 seconds' WHERE id=?",
                cron);
        try (var pool = Executors.newFixedThreadPool(2)) {
            var a = pool.submit(triggers::tick);
            var b = pool.submit(triggers::tick);
            assertThat(List.of(a.get(10, TimeUnit.SECONDS), b.get(10, TimeUnit.SECONDS)))
                    .contains(true);
        }
        var history = triggers.history(alice, cron, Instant.EPOCH, 100);
        assertThat(history).hasSizeBetween(1, 2);
        assertThat(history.stream().map(x -> x.eventKey()).distinct().count())
                .isEqualTo(history.size());
        assertThat(sql.queryForObject("SELECT count(*) FROM business.turns", Integer.class))
                .isEqualTo(history.size());
    }

    @Test
    void fairOwnersAndConcurrentClaimsRespectOwnerAndAgentQuotas() throws Exception {
        policies.update(alice, new DispatchPolicy(0, 1, 1));
        policies.update(bob, new DispatchPolicy(0, 1, 1));
        queue(alice, "a1");
        queue(alice, "a2");
        queue(bob, "b1");
        try (var pool = Executors.newFixedThreadPool(2)) {
            for (int round = 0; round < 4; round++) {
                var a = pool.submit(() -> leases.claim("worker-a", 60000));
                var b = pool.submit(() -> leases.claim("worker-b", 60000));
                a.get(10, TimeUnit.SECONDS);
                b.get(10, TimeUnit.SECONDS);
            }
        }
        assertThat(
                        sql.queryForList(
                                "SELECT t.subject,count(*) AS count FROM business.commands c JOIN business.turns t ON t.id=c.turn_id WHERE c.status='running' GROUP BY t.subject ORDER BY t.subject"))
                .hasSize(2)
                .allSatisfy(row -> assertThat(((Number) row.get("count")).intValue()).isOne());
        assertThat(
                        sql.queryForObject(
                                "SELECT count(*) FROM business.commands WHERE status='queued'",
                                Integer.class))
                .isOne();
    }

    @Test
    void priorityAppliesWithinOwnerAndAgingPreventsPermanentPriorityStarvation() {
        policies.update(alice, new DispatchPolicy(0, 4, 4));
        String low = queue(alice, "old-low"), high = queue(alice, "new-high");
        policies.prioritize(alice, low, -100);
        policies.prioritize(alice, high, 100);
        assertThat(leases.claim("worker-a", 60000).turn().id()).isEqualTo(high);
        String newer = queue(alice, "newer-high");
        policies.prioritize(alice, newer, 100);
        sql.update(
                "UPDATE business.commands SET created_at=clock_timestamp()-interval '3 hours' WHERE turn_id=?",
                low);
        assertThat(leases.claim("worker-a", 60000).turn().id()).isEqualTo(low);
        assertThatThrownBy(() -> policies.prioritize(bob, newer, 10))
                .hasMessage("queued_turn_not_found");
        assertThatThrownBy(() -> policies.update(alice, new DispatchPolicy(0, 4, 4)))
                .hasMessage("dispatch_revision_changed");
    }

    @Test
    void expiredOrWrongProtocolWorkersCannotClaimAndUnknownWorkersAreRejected() {
        queue(alice, "work");
        sql.update(
                "UPDATE business.runtime_workers SET expires_at=clock_timestamp()-interval '1 second' WHERE worker_id='worker-a'");
        sql.update(
                "UPDATE business.runtime_workers SET protocol_version=2 WHERE worker_id='worker-b'");
        assertThat(leases.claim("worker-a", 60000)).isNull();
        assertThat(leases.claim("worker-b", 60000)).isNull();
        assertThat(leases.claim("unknown", 60000)).isNull();
        assertThat(sql.queryForObject("SELECT count(*) FROM business.attempts", Integer.class))
                .isZero();
    }

    @Test
    void frozenAgentRuntimeRoutesOnlyToACompatibleWorker() {
        String agent = UUID.randomUUID().toString();
        var agents = app.getBean(com.financeclaw.app.agent.AgentService.class);
        agents.create(
                alice,
                agent,
                "runtime route",
                "Answer directly.",
                new com.financeclaw.domain.agent.AgentComposition("primary", List.of()));
        String hash = agents.publish(alice, agent, 1, 0).releaseHash();
        String conversation = submission.createConversation(alice, agent);
        String turn = submission.submit(alice, conversation, "runtime-route", "work").turnId();
        sql.update(
                "UPDATE business.runtime_workers SET runtime_hash=? WHERE worker_id='worker-a'",
                "0".repeat(64));
        assertThat(leases.claim("worker-a", 60000)).isNull();
        var accepted = leases.claim("worker-b", 60000);
        assertThat(accepted).isNotNull();
        assertThat(accepted.turn().id()).isEqualTo(turn);
        assertThat(accepted.turn().releaseHash()).isEqualTo(hash);
        assertThat(accepted.turn().agentId()).isEqualTo(agent);
    }

    @Test
    void repeatedEnqueueDoesNotLetOneOwnerSkipAnotherEligibleOwner() {
        policies.update(alice, new DispatchPolicy(0, 8, 8));
        policies.update(bob, new DispatchPolicy(0, 8, 8));
        queue(alice, "a1");
        queue(alice, "a2");
        queue(bob, "b1");
        queue(bob, "b2");
        var observed = new java.util.ArrayList<String>();
        for (int round = 0; round < 4; round++) {
            observed.add(leases.claim("worker-a", 60000).turn().subject());
            queue(alice, "continuing-a-" + round);
        }
        assertThat(observed).containsExactly("alice", "bob", "alice", "bob");
    }

    @Test
    void tenantAndAgentQuotasCountAllSubjectsAndNarrowOwnerDefaults() {
        policies.update(alice, new DispatchPolicy(0, 4, 4));
        policies.update(bob, new DispatchPolicy(0, 4, 4));
        policies.updateTenant(alice, new ConcurrencyLimit(0, 1));
        queue(alice, "alice");
        queue(bob, "bob");
        var first = leases.claim("worker-a", 60000);
        assertThat(first).isNotNull();
        assertThat(leases.claim("worker-b", 60000)).isNull();
        // The existing attempt remains counted even after its lease expires, until normal recovery.
        sql.update(
                "UPDATE business.commands SET lease_until=clock_timestamp()-interval '1 second' WHERE id=?",
                first.command().id());
        assertThat(leases.claim("worker-b", 60000)).isNull();
        policies.updateTenant(alice, new ConcurrencyLimit(1, 10));
        policies.updateAgent(alice, "finance_agent", new ConcurrencyLimit(0, 1));
        queue(alice, "alice-second");
        assertThat(policies.agent(bob, "finance_agent").maximumRunning()).isEqualTo(1);
        assertThat(leases.claim("worker-b", 60000)).isNull();
        policies.updateAgent(alice, "finance_agent", new ConcurrencyLimit(1, 2));
        assertThat(leases.claim("worker-b", 60000).turn().subject()).isEqualTo("bob");
        assertThat(leases.claim("worker-b", 60000)).isNull();
        assertThatThrownBy(() -> policies.updateTenant(alice, new ConcurrencyLimit(0, 10)))
                .hasMessage("dispatch_revision_changed");
    }

    @Test
    void registrationExpiringDuringTurnLockWaitRollsBackEntireClaim() throws Exception {
        String turn = queue(alice, "wait-for-lock");
        try (var blocker = app.getBean(DataSource.class).getConnection();
                var pool = Executors.newSingleThreadExecutor()) {
            blocker.setAutoCommit(false);
            try (var statement =
                    blocker.prepareStatement(
                            "SELECT id FROM business.turns WHERE id=? FOR UPDATE")) {
                statement.setString(1, turn);
                statement.executeQuery().close();
            }
            var pending = pool.submit(() -> leases.claim("worker-a", 60000));
            long end = System.nanoTime() + TimeUnit.SECONDS.toNanos(5);
            boolean waiting = false;
            while (System.nanoTime() < end) {
                waiting =
                        Boolean.TRUE.equals(
                                sql.queryForObject(
                                        "SELECT EXISTS(SELECT 1 FROM pg_stat_activity WHERE datname=current_database() AND wait_event_type='Lock' AND query LIKE '%FROM business.turns WHERE id=%FOR UPDATE%')",
                                        Boolean.class));
                if (waiting) break;
                Thread.sleep(10);
            }
            assertThat(waiting).isTrue();
            sql.update(
                    "UPDATE business.runtime_workers SET expires_at=clock_timestamp()-interval '1 second' WHERE worker_id='worker-a'");
            blocker.rollback();
            assertThatThrownBy(() -> pending.get(10, TimeUnit.SECONDS))
                    .hasCauseInstanceOf(Problem.class)
                    .hasRootCauseMessage("worker_unavailable");
            assertThat(sql.queryForObject("SELECT count(*) FROM business.attempts", Integer.class))
                    .isZero();
            assertThat(
                            sql.queryForObject(
                                    "SELECT status FROM business.commands WHERE turn_id=?",
                                    String.class,
                                    turn))
                    .isEqualTo("queued");
            assertThat(
                            sql.queryForObject(
                                    "SELECT status FROM business.turns WHERE id=?",
                                    String.class,
                                    turn))
                    .isEqualTo("queued");
        }
    }

    @Test
    void staleFailedOccurrenceCannotDeferAConcurrentSuccessfulAdvance() {
        String id = UUID.randomUUID().toString();
        triggers.put(alice, id, 0, definition("cron", "fixed", "allow", "work"));
        sql.update(
                "UPDATE control_plane.trigger_rules SET next_at=clock_timestamp()-interval '1 second' WHERE id=?",
                id);
        var original = triggers.get(alice, id);
        assertThat(triggers.tick()).isTrue();
        var advanced = triggers.get(alice, id);
        assertThat(advanced.nextAt()).isAfter(original.nextAt());
        app.getBean(com.financeclaw.infrastructure.scheduling.JdbcTriggerRepository.class)
                .defer(id, original.revision(), original.nextAt(), "admission_failed");
        var observed = triggers.get(alice, id);
        assertThat(observed.nextAt()).isEqualTo(advanced.nextAt());
        assertThat(observed.lastError()).isNull();
        assertThat(observed.retryAfter()).isNull();
    }
}
