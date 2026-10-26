package com.financeclaw.p3;

import static org.assertj.core.api.Assertions.*;
import static org.mockito.ArgumentMatchers.*;
import static org.mockito.Mockito.*;

import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import com.financeclaw.app.execution.port.ExecutionContextGateway;
import com.financeclaw.app.task.TaskQueryService;
import com.financeclaw.app.task.WorkSignals;
import com.financeclaw.bootstrap.FinanceClawApplication;
import com.financeclaw.channel.AgentScopeFeishu;
import com.financeclaw.channel.FeishuDelivery;
import com.financeclaw.channel.FeishuService;
import com.financeclaw.channel.FeishuSettings;
import com.financeclaw.domain.Feishu.Binding;
import com.financeclaw.domain.Feishu.Notice;
import com.financeclaw.domain.Task.Owner;
import com.financeclaw.domain.Task.Turn;
import com.financeclaw.fixture.GovernanceFixture;
import com.financeclaw.fixture.TaskFixture;
import com.financeclaw.infrastructure.agentscope.capability.BusinessCapabilities;
import com.financeclaw.infrastructure.agentscope.execution.TaskAgentRuntime;
import com.financeclaw.infrastructure.channel.FeishuMapper;
import com.financeclaw.infrastructure.state.RuntimeMapper;
import com.financeclaw.m1.TaskWorkerFixture;
import com.financeclaw.m2.ScriptedRuntime;
import com.financeclaw.support.PayloadJson;
import java.nio.file.Files;
import java.nio.file.Path;
import java.sql.Connection;
import java.sql.SQLException;
import java.time.Duration;
import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.UUID;
import java.util.concurrent.CopyOnWriteArrayList;
import java.util.concurrent.Executors;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.atomic.AtomicLong;
import java.util.concurrent.locks.LockSupport;
import java.util.function.Supplier;
import javax.sql.DataSource;
import org.junit.jupiter.api.*;
import org.junit.jupiter.api.parallel.Execution;
import org.junit.jupiter.api.parallel.ExecutionMode;
import org.junit.jupiter.params.ParameterizedTest;
import org.junit.jupiter.params.provider.ValueSource;
import org.springframework.beans.factory.config.BeanPostProcessor;
import org.springframework.boot.WebApplicationType;
import org.springframework.boot.builder.SpringApplicationBuilder;
import org.springframework.boot.test.context.TestConfiguration;
import org.springframework.context.ConfigurableApplicationContext;
import org.springframework.context.annotation.Bean;
import org.springframework.context.annotation.Import;
import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.transaction.support.TransactionSynchronizationManager;
import org.springframework.transaction.support.TransactionTemplate;
import org.testcontainers.containers.PostgreSQLContainer;

/**
 * 通知领取双锁读取的真实 PostgreSQL 基线。所有领取均经过生产 FeishuDelivery；只替换最末端发送边界， 不声称验证飞书原生
 * SDK。正常绑定、受理、执行和取消均使用生产服务，不伪造 Turn/Command/Attempt。
 *
 * <p>锁屏障使用独立原始连接持行锁、pg_blocking_pids 确认阻塞和 NOWAIT 验证另一行是否已被锁定。
 * 观察查询与锁持有者的修订变更不进入业务计数；不能把等待若干毫秒当作锁顺序证明。
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
@Execution(ExecutionMode.SAME_THREAD)
class NoticeClaimQueryIT {
    private static final Owner ALICE = new Owner("notice-query-tenant-a", "alice");
    private static final Owner BOB = new Owner("notice-query-tenant-b", "bob");
    private static final String CHANNEL_TENANT = "notice-query-channel";
    private final boolean baseline = Boolean.getBoolean("financeclaw.test.notice-locks-baseline");
    private final ObjectMapper json = new ObjectMapper();
    private final PostgreSQLContainer<?> postgres =
            new PostgreSQLContainer<>("postgres:16.10-alpine");
    private final List<Map<String, Object>> measurements = new CopyOnWriteArrayList<>();
    private final List<Send> sends = new CopyOnWriteArrayList<>();
    private final AtomicLong excludedObservations = new AtomicLong();
    private ConfigurableApplicationContext application;
    private DataSource original;
    private JdbcTemplate raw;
    private JdbcExecutionCounter counter;
    private TaskFixture tasks;
    private GovernanceFixture governance;
    private FeishuMapper db;
    private FeishuService inbound;
    private FeishuSettings settings;
    private AgentScopeFeishu sender;

    record Send(String chat, String text) {}

    record Scenario(Owner owner, Binding binding, Turn turn) {}

    record Observed<T>(
            T value,
            Throwable failure,
            JdbcExecutionCounter.Measurement sql,
            Map<String, Object> evidence) {}

    record HeldRow(Connection connection, int backend) implements AutoCloseable {
        @Override
        public void close() throws SQLException {
            try {
                connection.rollback();
            } finally {
                connection.close();
            }
        }
    }

    static final class RawSource {
        private DataSource source;
    }

    @TestConfiguration(proxyBeanMethods = false)
    @Import({TaskFixture.class, GovernanceFixture.class})
    static class CountingConfiguration {
        @Bean
        static JdbcExecutionCounter noticeClaimCounter() {
            return new JdbcExecutionCounter();
        }

        @Bean
        static RawSource noticeClaimRawSource() {
            return new RawSource();
        }

        @Bean
        static BeanPostProcessor noticeClaimCountingSource(
                JdbcExecutionCounter counter, RawSource raw) {
            return new BeanPostProcessor() {
                @Override
                public Object postProcessAfterInitialization(Object bean, String name) {
                    if (name.equals("dataSource") && bean instanceof DataSource source) {
                        raw.source = source;
                        return counter.wrap(source);
                    }
                    return bean;
                }
            };
        }
    }

    @BeforeAll
    void start() {
        if (Boolean.getBoolean("financeclaw.test.query-baseline") || baseline) {
            throw new IllegalStateException(
                    "Historical query/notice-locks baseline flags are not supported by the P3a.20 regression");
        }
        try {
            postgres.start();
            application =
                    new SpringApplicationBuilder(
                                    FinanceClawApplication.class, CountingConfiguration.class)
                            .web(WebApplicationType.SERVLET)
                            .run(
                                    "--spring.datasource.url=" + postgres.getJdbcUrl(),
                                    "--spring.datasource.username=" + postgres.getUsername(),
                                    "--spring.datasource.password=" + postgres.getPassword(),
                                    "--spring.datasource.hikari.maximum-pool-size=8",
                                    "--spring.config.import=",
                                    "--server.port=0",
                                    "--spring.main.banner-mode=off",
                                    "--financeclaw.worker.enabled=false",
                                    "--financeclaw.integrations.enabled=false",
                                    "--financeclaw.memory.enabled=false",
                                    "--financeclaw.memory.profile-enabled=false",
                                    "--financeclaw.memory.profile-directory=",
                                    "--financeclaw.memory.forget-ledger.enabled=false",
                                    "--financeclaw.model.name=offline",
                                    "--financeclaw.fallback.name=",
                                    "--financeclaw.auth.token=synthetic-notice-query-token",
                                    "--financeclaw.feishu.enabled=true",
                                    "--financeclaw.feishu.app-id=notice-query",
                                    "--financeclaw.feishu.app-secret=synthetic-notice-secret",
                                    "--financeclaw.feishu.verification-token=synthetic-notice-token",
                                    "--financeclaw.feishu.encrypt-key=synthetic-notice-key",
                                    "--financeclaw.feishu.tenant-key=" + CHANNEL_TENANT,
                                    "--financeclaw.business.ziwei.enabled=false");
            original = application.getBean(RawSource.class).source;
            raw = new JdbcTemplate(original);
            counter = application.getBean(JdbcExecutionCounter.class);
            tasks = application.getBean(TaskFixture.class);
            governance = application.getBean(GovernanceFixture.class);
            db = application.getBean(FeishuMapper.class);
            inbound = application.getBean(FeishuService.class);
            settings = application.getBean(FeishuSettings.class);
            sender = mock(AgentScopeFeishu.class);
        } catch (RuntimeException | Error failure) {
            closeResources();
            throw failure;
        }
    }

    @BeforeEach
    void reset() {
        dropFailureTrigger();
        raw.execute(
                "TRUNCATE agent_runtime.states,business.conversations,business.admission,"
                        + "memory.jobs,memory.owners,memory.operations,memory.audit CASCADE");
        sends.clear();
        org.mockito.Mockito.reset(sender);
        doAnswer(
                        call -> {
                            assertThat(
                                            TransactionSynchronizationManager
                                                    .isActualTransactionActive())
                                    .isFalse();
                            sends.add(new Send(call.getArgument(0), call.getArgument(1)));
                            return null;
                        })
                .when(sender)
                .send(anyString(), anyString());
    }

    @AfterEach
    void removeFault() {
        dropFailureTrigger();
    }

    @AfterAll
    void writeAndClose() throws Exception {
        try {
            Path output = Path.of("target/p3a20/notice-claim-sql.json");
            Files.createDirectories(output.getParent());
            var report = new LinkedHashMap<String, Object>();
            report.put("schema", "notice-claim-sql/2");
            report.put("phase", "P3a.20");
            report.put(
                    "scope",
                    "public-blocking-maintenance-lock-contract-plus-current-scheduled-batches");
            report.put("mode", baseline ? "before" : "after");
            report.put("background_pollers_enabled", false);
            report.put("measurement_boundary", "production-claim-or-scheduled-only");
            report.put(
                    "sql_semantics",
                    "jdbc-execute-attempts-fixture-and-raw-lock-observations-excluded");
            report.put("excluded_observation_queries", excludedObservations.get());
            report.put("measurements", List.copyOf(measurements));
            json.writerWithDefaultPrettyPrinter().writeValue(output.toFile(), report);
        } finally {
            closeResources();
        }
    }

    @ParameterizedTest
    @ValueSource(strings = {"unchanged", "status", "muted", "revision"})
    void bindingLockWaitNeverLocksNoticeEarlyAndRechecksCommittedRows(String change)
            throws Exception {
        var scenario = scenario(ALICE, "completed");
        var delivery = delivery();
        delivery.project(scenario.binding().id());
        var pending = notice(scenario);
        assertThat(pending.bindingRevision()).isNotEqualTo(pending.revision());
        Observed<Notice> observed;
        // 声明顺序保证异常退出时先释放 holder，再等待执行器结束，避免 close 相互等待。
        try (var worker = Executors.newSingleThreadExecutor();
                var held = hold("channel.bindings", scenario.binding().id())) {
            var future = worker.submit(() -> observe("binding-wait-" + change, delivery::claim));
            awaitBlockedBy(held.backend());
            assertThat(canLockNowait("channel.notices", pending.id()))
                    .as("notice remains unlocked while binding is held by another transaction")
                    .isTrue();
            switch (change) {
                case "status" -> {
                    // notice 在独立事务先提交；claim 的单条合并语句已经开始且仍在等 binding，不能沿用旧快照。
                    assertThat(
                                    raw.update(
                                            "UPDATE channel.notices SET status='unknown',reason='holder_status_changed' WHERE id=?",
                                            pending.id()))
                            .isOne();
                }
                case "muted" ->
                        update(
                                held.connection(),
                                "UPDATE channel.bindings SET muted=true WHERE id=?",
                                scenario.binding().id());
                case "revision" -> {
                    // 只推进 binding revision，保留 owner/conversation/activeTurn，从而独立命中 revision 门禁。
                    update(
                            held.connection(),
                            "UPDATE channel.bindings SET revision=revision+1 WHERE id=?",
                            scenario.binding().id());
                }
                case "unchanged" -> {}
                default -> throw new IllegalArgumentException("Unknown holder change");
            }
            held.connection().commit();
            observed = future.get(10, TimeUnit.SECONDS);
        }
        var current = notice(scenario);
        boolean claimed = change.equals("unchanged");
        String expectedStatus =
                claimed ? "sending" : change.equals("status") ? "unknown" : "suppressed";
        observed.evidence()
                .put(
                        "business_facts",
                        Map.of(
                                "binding_wait_confirmed",
                                true,
                                "notice_lock_available_during_binding_wait",
                                true,
                                "revision_columns_distinct",
                                true,
                                "claimed",
                                claimed,
                                "committed_row_change_observed",
                                current.status().equals(expectedStatus),
                                "external_sends",
                                sends.size()));
        assertClaimSql(observed, claimed ? 5 : change.equals("status") ? 3 : 4);
        assertThat(current.status()).isEqualTo(expectedStatus);
        assertThat(sends).isEmpty();
        if (claimed) {
            assertThat(observed.value()).isEqualTo(pending);
            assertThat(observed.sql().count("turn_read")).isOne();
            assertThat(observed.sql().count("notice_claim_update")).isOne();
            assertThat(observed.sql().count("notice_outcome_update")).isZero();
        } else {
            assertThat(observed.value()).isNull();
            assertThat(observed.sql().count("turn_read")).isZero();
            assertThat(observed.sql().count("notice_claim_update")).isZero();
            assertThat(observed.sql().count("notice_outcome_update"))
                    .isEqualTo(change.equals("status") ? 0 : 1);
            assertThat(current.reason())
                    .isEqualTo(
                            change.equals("status") ? "holder_status_changed" : "binding_changed");
        }
        assertNoticeIdentity(current, scenario);
    }

    @Test
    void noticeLockWaitHoldsBindingAndObservesTheUpdatedNoticeState() throws Exception {
        var scenario = scenario(ALICE, "completed");
        var delivery = delivery();
        delivery.project(scenario.binding().id());
        var pending = notice(scenario);
        Observed<Notice> observed;
        try (var worker = Executors.newSingleThreadExecutor();
                var held = hold("channel.notices", pending.id())) {
            var future = worker.submit(() -> observe("notice-wait-status", delivery::claim));
            awaitBlockedBy(held.backend());
            assertThat(canLockNowait("channel.bindings", scenario.binding().id()))
                    .as("binding has already been locked before waiting for notice")
                    .isFalse();
            update(
                    held.connection(),
                    "UPDATE channel.notices SET status='unknown',reason='holder_status_changed' WHERE id=?",
                    pending.id());
            held.connection().commit();
            observed = future.get(10, TimeUnit.SECONDS);
        }
        var current = notice(scenario);
        observed.evidence()
                .put(
                        "business_facts",
                        Map.of(
                                "notice_wait_confirmed",
                                true,
                                "binding_lock_held_during_notice_wait",
                                true,
                                "updated_status_observed",
                                current.status().equals("unknown"),
                                "external_sends",
                                sends.size()));
        assertClaimSql(observed, 3);
        assertThat(observed.value()).isNull();
        assertThat(current.status()).isEqualTo("unknown");
        assertThat(current.reason()).isEqualTo("holder_status_changed");
        assertThat(observed.sql().count("turn_read")).isZero();
        assertThat(observed.sql().count("notice_claim_update")).isZero();
        assertThat(observed.sql().count("notice_outcome_update")).isZero();
        assertThat(sends).isEmpty();
        assertNoticeIdentity(current, scenario);
    }

    @Test
    void turnChangedDuringBindingWaitIsReadInANewStatementAfterBothLocks() throws Exception {
        var scenario = scenario(ALICE, "waiting");
        var delivery = delivery();
        delivery.project(scenario.binding().id());
        var pending = notice(scenario);
        Observed<Notice> observed;
        try (var worker = Executors.newSingleThreadExecutor();
                var held = hold("channel.bindings", scenario.binding().id())) {
            var future =
                    worker.submit(() -> observe("binding-wait-turn-revision", delivery::claim));
            awaitBlockedBy(held.backend());
            assertThat(canLockNowait("channel.notices", pending.id())).isTrue();
            // 正常任务取消不需要飞书 binding 锁，可在该锁仍被 holder 持有时独立提交 Turn 新 revision。
            var cancelled =
                    tasks.cancel(
                            scenario.owner(),
                            scenario.binding().conversationId(),
                            scenario.turn().id(),
                            "notice-wait-cancel");
            assertThat(cancelled.status()).isEqualTo("cancelled");
            assertThat(cancelled.revision()).isGreaterThan(pending.revision());
            held.connection().commit();
            observed = future.get(10, TimeUnit.SECONDS);
        }
        var current = notice(scenario);
        var turn =
                tasks.get(
                        scenario.owner(),
                        scenario.binding().conversationId(),
                        scenario.turn().id());
        observed.evidence()
                .put(
                        "business_facts",
                        Map.of(
                                "binding_wait_confirmed",
                                true,
                                "turn_changed_while_binding_held",
                                turn.status().equals("cancelled"),
                                "new_turn_revision_rechecked",
                                "revision_changed".equals(current.reason()),
                                "external_sends",
                                sends.size()));
        assertClaimSql(observed, 5);
        assertThat(observed.value()).isNull();
        assertThat(current.status()).isEqualTo("suppressed");
        assertThat(current.reason()).isEqualTo("revision_changed");
        assertThat(observed.sql().count("turn_read")).isOne();
        assertThat(observed.sql().count("notice_outcome_update")).isOne();
        assertThat(observed.sql().count("notice_claim_update")).isZero();
        assertThat(observed.sql().executions().get(baseline ? 3 : 2).family())
                .isEqualTo("turn_read");
        assertThat(sends).isEmpty();
        assertNoticeIdentity(current, scenario);
    }

    @ParameterizedTest
    @ValueSource(ints = {1, 20})
    void boundedProjectionAndDeliveryKeepOwnerRoutesAndCompleteNoticeFields(int batch)
            throws Exception {
        var scenarios = new ArrayList<Scenario>();
        int total = batch == 20 ? 21 : 1;
        for (int i = 0; i < total; i++) {
            var scenario = scenario(i % 2 == 0 ? ALICE : BOB, "completed");
            orderTurn(scenario, i);
            scenarios.add(scenario);
        }
        var delivery = delivery();
        var first =
                observe(
                        "batch-" + batch + "-first",
                        () -> {
                            delivery.scheduled();
                            return null;
                        });
        int firstCount = integer("SELECT count(*) FROM channel.notices");
        first.evidence()
                .put(
                        "business_facts",
                        Map.of(
                                "projected_notices",
                                firstCount,
                                "batch_limit_respected",
                                firstCount == batch,
                                "external_sends",
                                sends.size(),
                                "first_route_matches",
                                sends.equals(List.of(expectedSend(scenarios.getFirst())))));
        assertSql(first, 3 * batch + (batch == 20 ? 4 : 5));
        assertThat(first.sql().count("notice_insert")).isEqualTo(batch);
        assertThat(firstCount).isEqualTo(batch);
        assertThat(sends).containsExactly(expectedSend(scenarios.getFirst()));
        assertThat(first.sql().commits()).isEqualTo(batch + (batch == 20 ? 1 : 2));
        assertThat(first.sql().rollbacks()).isZero();
        if (batch == 20) {
            assertThat(noticeCount(scenarios.get(20))).isZero();
            var next =
                    observe(
                            "batch-20-remaining-one",
                            () -> {
                                delivery.scheduled();
                                return null;
                            });
            next.evidence()
                    .put(
                            "business_facts",
                            Map.of(
                                    "projected_notices",
                                    integer("SELECT count(*) FROM channel.notices"),
                                    "external_sends",
                                    sends.size(),
                                    "one_remaining_projection",
                                    next.sql().count("notice_insert") == 1));
            assertSql(next, 8);
            assertThat(next.sql().count("notice_insert")).isOne();
            assertThat(integer("SELECT count(*) FROM channel.notices")).isEqualTo(21);
            assertThat(sends).hasSize(2);
            var tail =
                    observe(
                            "batch-20-finite-owner-drain",
                            () -> {
                                for (int i = 0; i < 19; i++) {
                                    int before = sends.size();
                                    delivery.scheduled();
                                    assertThat(sends.size() - before).isEqualTo(1);
                                }
                                return null;
                            });
            assertSql(tail, 19 * 5);
            assertThat(delivery.list(ALICE)).hasSize(11);
            assertThat(delivery.list(BOB)).hasSize(10);
            tail.evidence()
                    .put(
                            "business_facts",
                            Map.of(
                                    "all_routes_match",
                                    allRoutesMatch(scenarios),
                                    "finite_two_owner_backlog_drained",
                                    sends.size() == 21,
                                    "external_sends",
                                    sends.size()));
        }
        assertThat(sends).hasSize(total);
        assertThat(allRoutesMatch(scenarios)).isTrue();
        for (var scenario : scenarios) {
            var current = notice(scenario);
            assertNoticeIdentity(current, scenario);
            assertThat(current.payload()).isEqualTo(scenario.turn().result());
            assertThat(current.status()).isEqualTo("submitted_unverified");
            assertThat(current.reason()).isEqualTo("native_sdk_no_receipt");
        }
        var empty =
                observe(
                        "batch-" + batch + "-empty-repeat",
                        () -> {
                            delivery.scheduled();
                            return null;
                        });
        empty.evidence()
                .put(
                        "business_facts",
                        Map.of(
                                "external_sends",
                                sends.size(),
                                "no_duplicate_send",
                                sends.size() == total));
        assertSql(empty, 2);
        assertThat(empty.sql().commits()).isEqualTo(2);
        assertThat(empty.sql().rollbacks()).isZero();
        assertThat(sends).hasSize(total);
    }

    @Test
    void secondProjectionFaultKeepsTheFirstCommitAndRetryPreservesBothOwners() throws Exception {
        var firstScenario = scenario(ALICE, "completed");
        var secondScenario = scenario(BOB, "completed");
        orderTurn(firstScenario, 0);
        orderTurn(secondScenario, 1);
        var delivery = delivery();
        installFailureTrigger(secondScenario.binding().id());
        var failed =
                observe(
                        "projection-second-fault",
                        () -> {
                            delivery.scheduled();
                            return null;
                        });
        int firstCount = noticeCount(firstScenario);
        int secondCount = noticeCount(secondScenario);
        failed.evidence()
                .put(
                        "business_facts",
                        Map.of(
                                "first_projection_committed",
                                firstCount == 1,
                                "second_projection_rolled_back",
                                secondCount == 0,
                                "external_sends",
                                sends.size()));
        assertThat(failed.failure())
                .isNotNull()
                .hasStackTraceContaining("notice_claim_query_injected_failure");
        assertThat(failed.sql().statements()).isEqualTo(6);
        assertThat(failed.sql().count("notice_projection_scan")).isZero();
        assertThat(failed.sql().count("notice_projection_try_lock")).isEqualTo(2);
        assertThat(failed.sql().count("notice_insert")).isEqualTo(2);
        assertThat(failed.sql().count("notice_pending_read")).isZero();
        assertThat(failed.sql().commits()).isOne();
        assertThat(failed.sql().rollbacks()).isOne();
        assertThat(
                        failed.sql().executions().stream()
                                .map(JdbcExecutionCounter.SqlExecution::connection)
                                .distinct())
                .hasSize(2);
        assertThat(firstCount).isOne();
        assertThat(secondCount).isZero();
        assertThat(notice(firstScenario).status()).isEqualTo("pending");
        assertThat(sends).isEmpty();
        assertThat(
                        tasks.get(
                                ALICE,
                                firstScenario.binding().conversationId(),
                                firstScenario.turn().id()))
                .isEqualTo(firstScenario.turn());
        assertThat(
                        tasks.get(
                                BOB,
                                secondScenario.binding().conversationId(),
                                secondScenario.turn().id()))
                .isEqualTo(secondScenario.turn());
        dropFailureTrigger();
        // 新协调器避免把前一轮异常退避混入数据恢复语义；notice 本身仍来自同一数据库。
        var restarted = delivery();
        var retry =
                observe(
                        "projection-second-fault-retry",
                        () -> {
                            restarted.scheduled();
                            return null;
                        });
        retry.evidence()
                .put(
                        "business_facts",
                        Map.of(
                                "both_projection_rows_present",
                                noticeCount(firstScenario) == 1 && noticeCount(secondScenario) == 1,
                                "first_route_matches",
                                sends.equals(List.of(expectedSend(firstScenario))),
                                "external_sends",
                                sends.size()));
        assertSql(retry, 8);
        assertThat(retry.sql().count("notice_insert")).isOne();
        assertThat(sends).containsExactly(expectedSend(firstScenario));
        var tail =
                observe(
                        "projection-second-fault-remaining-send",
                        () -> {
                            restarted.scheduled();
                            return null;
                        });
        tail.evidence()
                .put(
                        "business_facts",
                        Map.of(
                                "all_routes_match",
                                allRoutesMatch(List.of(firstScenario, secondScenario)),
                                "external_sends",
                                sends.size()));
        assertSql(tail, 5);
        assertThat(sends)
                .containsExactly(expectedSend(firstScenario), expectedSend(secondScenario));
        assertThat(notice(firstScenario).status()).isEqualTo("submitted_unverified");
        assertThat(notice(secondScenario).status()).isEqualTo("submitted_unverified");
        var empty =
                observe(
                        "projection-second-fault-empty-repeat",
                        () -> {
                            restarted.scheduled();
                            return null;
                        });
        empty.evidence()
                .put(
                        "business_facts",
                        Map.of(
                                "no_duplicate_send",
                                sends.size() == 2,
                                "external_sends",
                                sends.size()));
        assertSql(empty, 2);
        assertThat(sends).hasSize(2);
    }

    private Scenario scenario(Owner owner, String state) throws Exception {
        String conversation = tasks.createConversation(owner);
        String channelId = UUID.randomUUID().toString();
        var identity =
                new Binding(
                        "unused",
                        CHANNEL_TENANT,
                        "open-" + channelId,
                        "chat-" + channelId,
                        owner.tenant(),
                        owner.subject(),
                        conversation,
                        1,
                        false,
                        null);
        inbound.receive(message(identity, "/bind " + inbound.link(owner, conversation)));
        inbound.receive(
                message(identity, state.equals("waiting") ? "city" : "notice-" + channelId));
        var bound =
                db.getBindingByChannelIdentityForUpdate(
                        CHANNEL_TENANT, identity.openId(), identity.chatId());
        try (var worker =
                TaskWorkerFixture.create(
                        application,
                        state.equals("waiting")
                                ? new ScriptedRuntime(
                                        tasks,
                                        governance,
                                        application.getBean(RuntimeMapper.class),
                                        application.getBean(BusinessCapabilities.class))
                                : application.getBean(TaskAgentRuntime.class),
                        false,
                        1,
                        60000,
                        2000)) {
            var claim = tasks.claim("notice-query-worker", 60000);
            assertThat(claim).isNotNull();
            assertThat(claim.turn().id()).isEqualTo(bound.activeTurn());
            worker.execute(claim);
        }
        var turn = tasks.get(owner, conversation, bound.activeTurn());
        assertThat(turn.status())
                .as("normal fixture completion: %s", turn.reason())
                .isEqualTo(state);
        assertThat(bound.revision()).isNotEqualTo(turn.revision());
        return new Scenario(owner, bound, turn);
    }

    private FeishuDelivery delivery() {
        return new FeishuDelivery(
                db,
                application.getBean(TaskQueryService.class),
                application.getBean(ExecutionContextGateway.class),
                application.getBean(TransactionTemplate.class),
                sender,
                application.getBean(WorkSignals.class),
                settings,
                true,
                10000,
                10000);
    }

    private JsonNode message(Binding identity, String text) {
        return json.valueToTree(
                Map.of(
                        "schema",
                        "2.0",
                        "header",
                        Map.of(
                                "event_id",
                                UUID.randomUUID().toString(),
                                "event_type",
                                "im.message.receive_v1",
                                "app_id",
                                settings.appId,
                                "tenant_key",
                                CHANNEL_TENANT,
                                "token",
                                settings.token),
                        "event",
                        Map.of(
                                "sender",
                                Map.of(
                                        "sender_id",
                                        Map.of("open_id", identity.openId()),
                                        "sender_type",
                                        "user"),
                                "message",
                                Map.of(
                                        "chat_id",
                                        identity.chatId(),
                                        "chat_type",
                                        "p2p",
                                        "message_id",
                                        "message-" + UUID.randomUUID(),
                                        "message_type",
                                        "text",
                                        "content",
                                        PayloadJson.canonical(Map.of("text", text))))));
    }

    private <T> Observed<T> observe(String phase, Supplier<T> operation) {
        counter.begin(phase);
        T value = null;
        Throwable failure = null;
        try {
            value = operation.get();
        } catch (Throwable caught) {
            failure = caught;
        }
        var measured = counter.finish();
        var evidence = new LinkedHashMap<String, Object>();
        evidence.put("phase", phase);
        evidence.put("sql", measured);
        evidence.put("completed_without_exception", failure == null);
        if (failure != null) evidence.put("failure_type", failure.getClass().getSimpleName());
        measurements.add(evidence);
        return new Observed<>(value, failure, measured, evidence);
    }

    private void assertClaimSql(Observed<?> observed, int oldCount) {
        assertSql(observed, baseline ? oldCount : oldCount - 1);
        assertThat(observed.sql().count("notice_pending_read")).isOne();
        assertThat(observed.sql().count("notice_binding_lock")).isEqualTo(baseline ? 1 : 0);
        assertThat(observed.sql().count("notice_lock")).isEqualTo(baseline ? 1 : 0);
        assertThat(observed.sql().count("notice_claim_locked_read")).isEqualTo(baseline ? 0 : 1);
        assertThat(observed.sql().executions().getFirst().family())
                .isEqualTo("notice_pending_read");
        assertThat(observed.sql().commits()).isOne();
        assertThat(observed.sql().rollbacks()).isZero();
        assertThat(
                        observed.sql().executions().stream()
                                .map(JdbcExecutionCounter.SqlExecution::connection)
                                .distinct())
                .hasSize(2);
        var executions = observed.sql().executions();
        assertThat(executions.get(0).connection()).isNotEqualTo(executions.get(1).connection());
        assertThat(
                        executions.subList(1, executions.size()).stream()
                                .map(JdbcExecutionCounter.SqlExecution::connection)
                                .distinct())
                .hasSize(1);
    }

    private static void assertSql(Observed<?> observed, int expected) {
        assertThat(observed.failure()).isNull();
        assertThat(observed.sql().statements()).as(observed.sql().phase()).isEqualTo(expected);
        assertThat(observed.sql().count("other")).isZero();
        assertThat(observed.sql().executions())
                .allMatch(e -> e.templateSha256().matches("[0-9a-f]{64}"));
    }

    private HeldRow hold(String table, String id) throws SQLException {
        var connection = original.getConnection();
        connection.setAutoCommit(false);
        try {
            try (var statement =
                    connection.prepareStatement(
                            "SELECT id FROM " + lockTable(table) + " WHERE id=? FOR UPDATE")) {
                statement.setString(1, id);
                excludedObservations.incrementAndGet();
                try (var result = statement.executeQuery()) {
                    assertThat(result.next()).isTrue();
                }
            }
            try (var statement = connection.createStatement()) {
                excludedObservations.incrementAndGet();
                try (var result = statement.executeQuery("SELECT pg_backend_pid()")) {
                    assertThat(result.next()).isTrue();
                    return new HeldRow(connection, result.getInt(1));
                }
            }
        } catch (SQLException | RuntimeException | Error failure) {
            connection.close();
            throw failure;
        }
    }

    private boolean canLockNowait(String table, String id) throws SQLException {
        try (var connection = original.getConnection()) {
            connection.setAutoCommit(false);
            try (var statement =
                    connection.prepareStatement(
                            "SELECT id FROM "
                                    + lockTable(table)
                                    + " WHERE id=? FOR UPDATE NOWAIT")) {
                statement.setString(1, id);
                excludedObservations.incrementAndGet();
                try (var result = statement.executeQuery()) {
                    assertThat(result.next()).isTrue();
                }
                return true;
            } catch (SQLException failure) {
                if ("55P03".equals(failure.getSQLState())) return false;
                throw failure;
            } finally {
                connection.rollback();
            }
        }
    }

    private static String lockTable(String table) {
        if (!List.of("channel.bindings", "channel.notices").contains(table))
            throw new IllegalArgumentException("Unknown fixture table");
        return table;
    }

    private void awaitBlockedBy(int holder) {
        long deadline = System.nanoTime() + Duration.ofSeconds(5).toNanos();
        do {
            if (integer(
                            "SELECT count(*)::int FROM pg_stat_activity a WHERE a.datname=current_database() "
                                    + "AND a.pid<>pg_backend_pid() AND ?=ANY(pg_blocking_pids(a.pid))",
                            holder)
                    > 0) return;
            // 只对有界的观察查询退让；是否已阻塞始终由 pg_blocking_pids 的事实决定。
            LockSupport.parkNanos(TimeUnit.MILLISECONDS.toNanos(5));
        } while (System.nanoTime() < deadline);
        throw new AssertionError("Production notice claim did not block on the held row");
    }

    private static void update(Connection connection, String query, String id) throws SQLException {
        try (var statement = connection.prepareStatement(query)) {
            statement.setString(1, id);
            assertThat(statement.executeUpdate()).isOne();
        }
    }

    private Notice notice(Scenario scenario) {
        excludedObservations.incrementAndGet();
        return raw.queryForObject(
                "SELECT id,turn_id,binding_id,binding_revision,revision,payload,status,reason "
                        + "FROM channel.notices WHERE turn_id=? AND revision=?",
                (rs, row) ->
                        new Notice(
                                rs.getString("id"),
                                rs.getString("turn_id"),
                                rs.getString("binding_id"),
                                rs.getLong("binding_revision"),
                                rs.getLong("revision"),
                                rs.getString("payload"),
                                rs.getString("status"),
                                rs.getString("reason")),
                scenario.turn().id(),
                scenario.turn().revision());
    }

    private static void assertNoticeIdentity(Notice notice, Scenario scenario) {
        assertThat(notice.turnId()).isEqualTo(scenario.turn().id());
        assertThat(notice.bindingId()).isEqualTo(scenario.binding().id());
        assertThat(notice.bindingRevision()).isEqualTo(scenario.binding().revision());
        assertThat(notice.revision()).isEqualTo(scenario.turn().revision());
        assertThat(notice.bindingRevision()).isNotEqualTo(notice.revision());
    }

    private static Send expectedSend(Scenario scenario) {
        return new Send(scenario.binding().chatId(), scenario.turn().result());
    }

    private boolean allRoutesMatch(List<Scenario> scenarios) {
        return sends.size() == scenarios.size()
                && scenarios.stream()
                        .allMatch(
                                s ->
                                        sends.stream()
                                                .filter(
                                                        sent ->
                                                                sent.chat()
                                                                        .equals(
                                                                                s.binding()
                                                                                        .chatId()))
                                                .toList()
                                                .equals(List.of(expectedSend(s))));
    }

    private void orderTurn(Scenario scenario, int order) {
        raw.update(
                "UPDATE business.turns SET created_at=timestamptz '2000-01-01 00:00:00+00' + ? * interval '1 second' WHERE id=?",
                order,
                scenario.turn().id());
    }

    private int noticeCount(Scenario scenario) {
        return integer(
                "SELECT count(*)::int FROM channel.notices WHERE turn_id=?", scenario.turn().id());
    }

    private int integer(String query, Object... arguments) {
        excludedObservations.incrementAndGet();
        return raw.queryForObject(query, Integer.class, arguments);
    }

    private void installFailureTrigger(String binding) {
        assertThat(binding).matches("[0-9a-f-]{36}");
        raw.execute(
                "CREATE FUNCTION channel.notice_claim_query_fail() RETURNS trigger LANGUAGE plpgsql AS $$ "
                        + "BEGIN RAISE EXCEPTION 'notice_claim_query_injected_failure'; END; $$");
        raw.execute(
                "CREATE TRIGGER notice_claim_query_fail AFTER INSERT ON channel.notices FOR EACH ROW WHEN (NEW.binding_id='"
                        + binding
                        + "') EXECUTE FUNCTION channel.notice_claim_query_fail()");
    }

    private void dropFailureTrigger() {
        if (raw != null)
            raw.execute("DROP FUNCTION IF EXISTS channel.notice_claim_query_fail() CASCADE");
    }

    private void closeResources() {
        try {
            if (application != null) application.close();
        } finally {
            postgres.stop();
        }
    }
}
