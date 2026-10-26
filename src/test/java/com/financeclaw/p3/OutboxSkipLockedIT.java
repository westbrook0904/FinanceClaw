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
import com.financeclaw.support.PayloadDigest;
import com.financeclaw.support.PayloadJson;
import java.nio.file.Files;
import java.nio.file.Path;
import java.sql.Connection;
import java.sql.SQLException;
import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Locale;
import java.util.Map;
import java.util.UUID;
import java.util.concurrent.CopyOnWriteArrayList;
import java.util.concurrent.CountDownLatch;
import java.util.concurrent.Executors;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.atomic.AtomicLong;
import java.util.concurrent.atomic.AtomicReference;
import java.util.function.Supplier;
import javax.sql.DataSource;
import org.apache.ibatis.executor.Executor;
import org.apache.ibatis.mapping.MappedStatement;
import org.apache.ibatis.plugin.Interceptor;
import org.apache.ibatis.plugin.Intercepts;
import org.apache.ibatis.plugin.Invocation;
import org.apache.ibatis.plugin.Signature;
import org.apache.ibatis.scripting.defaults.DefaultParameterHandler;
import org.apache.ibatis.session.ResultHandler;
import org.apache.ibatis.session.RowBounds;
import org.apache.ibatis.session.SqlSessionFactory;
import org.junit.jupiter.api.*;
import org.junit.jupiter.api.parallel.Execution;
import org.junit.jupiter.api.parallel.ExecutionMode;
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
 * 定时通知的真实 PG SKIP LOCKED 验收：正常业务由生产受理/SDK/投影/领取完成，模型为 offline/固定脚本，末端 send 替身。 每个协调器使用真实独立短事务；显式
 * public project/claim 的阻塞维护合同由原 NoticeClaimQueryIT 保留。 合成规模只用于默认计划与锁前缀，绝不交给发送或恢复，不证明多进程部署或全局 owner
 * 公平性。
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
@Execution(ExecutionMode.SAME_THREAD)
class OutboxSkipLockedIT {
    private static final Owner ALICE = new Owner("outbox-skip-a", "alice");
    private static final Owner BOB = new Owner("outbox-skip-b", "bob");
    private static final String CHANNEL_TENANT = "outbox-skip-channel";
    private final ObjectMapper json = new ObjectMapper();
    private final PostgreSQLContainer<?> postgres =
            new PostgreSQLContainer<>("postgres:16.10-alpine");
    private final List<Map<String, Object>> measurements = new CopyOnWriteArrayList<>();
    private final List<Map<String, Object>> plans = new ArrayList<>();
    private final List<Send> sends = new CopyOnWriteArrayList<>();
    private final AtomicLong excludedObservations = new AtomicLong();
    private final AtomicReference<Gate> sendGate = new AtomicReference<>();
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
    private ClaimGateInterceptor claimGate;

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

    static final class Gate {
        final CountDownLatch entered;
        final CountDownLatch release = new CountDownLatch(1);

        Gate(int count) {
            entered = new CountDownLatch(count);
        }

        void block() throws InterruptedException {
            entered.countDown();
            if (!release.await(8, TimeUnit.SECONDS))
                throw new AssertionError("Gate not released within eight seconds");
        }
    }

    @Intercepts(
            @Signature(
                    type = Executor.class,
                    method = "query",
                    args = {
                        MappedStatement.class,
                        Object.class,
                        RowBounds.class,
                        ResultHandler.class
                    }))
    static final class ClaimGateInterceptor implements Interceptor {
        final AtomicReference<Gate> active = new AtomicReference<>();

        @Override
        public Object intercept(Invocation invocation) throws Throwable {
            Object result = invocation.proceed();
            if (((MappedStatement) invocation.getArgs()[0])
                            .getId()
                            .equals(
                                    FeishuMapper.class.getName()
                                            + ".getNextNoticeForUpdateSkipLocked")
                    && result instanceof List<?> values
                    && !values.isEmpty()
                    && values.getFirst() instanceof FeishuMapper.ScheduledNoticeRow row
                    && row.notice() != null) {
                var gate = active.getAndSet(null);
                if (gate != null) gate.block();
            }
            return result;
        }
    }

    static final class RawSource {
        private DataSource source;
    }

    @TestConfiguration(proxyBeanMethods = false)
    @Import({TaskFixture.class, GovernanceFixture.class})
    static class CountingConfiguration {
        @Bean
        ClaimGateInterceptor outboxClaimGate() {
            return new ClaimGateInterceptor();
        }

        @Bean
        static JdbcExecutionCounter outboxSkipCounter() {
            return new JdbcExecutionCounter();
        }

        @Bean
        static RawSource outboxSkipRawSource() {
            return new RawSource();
        }

        @Bean
        static BeanPostProcessor outboxSkipCountingSource(
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
            claimGate = application.getBean(ClaimGateInterceptor.class);
        } catch (RuntimeException | Error failure) {
            closeResources();
            throw failure;
        }
    }

    @BeforeEach
    void reset() {
        dropFailureTrigger();
        raw.execute(
                "TRUNCATE agent_runtime.states,business.conversations,business.admission,memory.jobs,memory.owners,memory.operations,memory.audit CASCADE");
        sends.clear();
        sendGate.set(null);
        claimGate.active.set(null);
        org.mockito.Mockito.reset(sender);
        doAnswer(
                        call -> {
                            assertThat(
                                            TransactionSynchronizationManager
                                                    .isActualTransactionActive())
                                    .isFalse();
                            sends.add(new Send(call.getArgument(0), call.getArgument(1)));
                            var gate = sendGate.get();
                            if (gate != null) gate.block();
                            return null;
                        })
                .when(sender)
                .send(anyString(), anyString());
    }

    @AfterAll
    void writeAndClose() throws Exception {
        try {
            var report = new LinkedHashMap<String, Object>();
            report.put("schema", "outbox-skip-locked/1");
            report.put("phase", "P3a.20");
            report.put(
                    "scope",
                    "real-pg-independent-coordinators-production-task-services-agentscope-offline-terminal-send-mock");
            report.put("background_pollers_enabled", false);
            report.put("no_global_fifo_fairness_or_multi_jvm_acceptance_claim", true);
            report.put(
                    "measurement_boundary",
                    "scheduled-only-fixture-lock-oracles-and-explain-excluded");
            report.put("counted_excluded_assertion_queries", excludedObservations.get());
            report.put(
                    "excluded_count_scope",
                    "explicit-assertion-helpers-not-all-setup-or-explain-statements");
            report.put("measurements", measurements);
            report.put("scale_experiments", plans);
            Path output = Path.of("target/p3a20/outbox-skip-locked.json");
            Files.createDirectories(output.getParent());
            json.writerWithDefaultPrettyPrinter().writeValue(output.toFile(), report);
        } finally {
            closeResources();
        }
    }

    @Test
    void pendingClaimSkipsTwentyFiveLockedBindingsWithoutLockingTheirNotices() throws Exception {
        var prefix = preparedPrefix(25, true);
        var tail = scenario(BOB, "completed");
        delivery().project(tail.binding().id());
        orderNotice(tail, 30);
        try (var held =
                holdMany("channel.bindings", prefix.stream().map(s -> s.binding().id()).toList())) {
            var gate = new Gate(1);
            claimGate.active.set(gate);
            var pool = Executors.newSingleThreadExecutor();
            var running =
                    pool.submit(
                            () ->
                                    observe(
                                            "claim-locked-binding-prefix",
                                            () -> {
                                                delivery().scheduled();
                                                return null;
                                            }));
            try {
                assertThat(gate.entered.await(8, TimeUnit.SECONDS)).isTrue();
                // 返回 tail 的同一事务尚未提交，直接检查其未在忙 binding 之前反向锁住 notice。
                for (var item : prefix) {
                    assertThat(canLockNowait("channel.notices", notice(item).id())).isTrue();
                    assertThat(notice(item).status()).isEqualTo("pending");
                }
                assertThat(canLockNowait("channel.bindings", tail.binding().id())).isFalse();
                assertThat(canLockNowait("channel.notices", notice(tail).id())).isFalse();
            } finally {
                gate.release.countDown();
                pool.shutdown();
                assertThat(pool.awaitTermination(8, TimeUnit.SECONDS)).isTrue();
            }
            var first = running.get(8, TimeUnit.SECONDS);
            first.evidence()
                    .put(
                            "business_facts",
                            Map.of(
                                    "locked_binding_prefix",
                                    25,
                                    "tail_owner_sent",
                                    sends.equals(List.of(expectedSend(tail))),
                                    "prefix_notices_still_lockable_while_tail_transaction_open",
                                    true,
                                    "send_attempts",
                                    sends.size()));
            successful(first);
            assertThat(sends).containsExactly(expectedSend(tail));
        }
        var resumed =
                observe(
                        "claim-prefix-after-unlock",
                        () -> {
                            delivery().scheduled();
                            return null;
                        });
        resumed.evidence()
                .put(
                        "business_facts",
                        Map.of(
                                "send_attempts",
                                sends.size(),
                                "oldest_prefix_sent",
                                sends.contains(expectedSend(prefix.getFirst()))));
        successful(resumed);
        assertThat(sends).containsExactly(expectedSend(tail), expectedSend(prefix.getFirst()));
    }

    @Test
    void projectionSkipsLockedBindingsAndRetainsTheTwentyItemIndependentTransactionBound()
            throws Exception {
        var prefix = preparedPrefix(25, false);
        var tail = scenario(BOB, "completed");
        orderTurn(tail, 30);
        try (var held =
                holdMany("channel.bindings", prefix.stream().map(s -> s.binding().id()).toList())) {
            var first =
                    observe(
                            "projection-locked-binding-prefix",
                            () -> {
                                delivery().scheduled();
                                return null;
                            });
            first.evidence()
                    .put(
                            "business_facts",
                            Map.of(
                                    "locked_binding_prefix",
                                    25,
                                    "tail_owner_sent",
                                    sends.equals(List.of(expectedSend(tail))),
                                    "prefix_notice_count",
                                    prefix.stream().mapToInt(this::noticeCount).sum()));
            successful(first);
            assertThat(sends).containsExactly(expectedSend(tail));
            for (var item : prefix) assertThat(noticeCount(item)).isZero();
        }
        var resumed =
                observe(
                        "projection-first-twenty-after-unlock",
                        () -> {
                            delivery().scheduled();
                            return null;
                        });
        resumed.evidence()
                .put(
                        "business_facts",
                        Map.of(
                                "prefix_projected",
                                prefix.stream().mapToInt(this::noticeCount).sum(),
                                "send_attempts",
                                sends.size()));
        successful(resumed);
        assertThat(prefix.stream().mapToInt(this::noticeCount).sum()).isEqualTo(20);
        assertThat(resumed.sql().count("notice_insert")).isEqualTo(20);
        assertThat(sends).hasSize(2);
    }

    @Test
    void allLockedNoticeRowsPreserveNullableVersionsAndConsumeOnlyTwentyBindingAttempts()
            throws Exception {
        for (int busyCount : List.of(2, 21)) {
            if (busyCount == 21) reset();
            var prefix = preparedPrefix(busyCount, true);
            var tail = scenario(BOB, "completed");
            delivery().project(tail.binding().id());
            orderNotice(tail, 30);
            try (var held =
                    holdMany(
                            "channel.notices", prefix.stream().map(s -> notice(s).id()).toList())) {
                application
                        .getBean(TransactionTemplate.class)
                        .executeWithoutResult(
                                tx -> {
                                    var selected = db.getNextNoticeForUpdateSkipLocked(List.of());
                                    assertThat(selected.bindingId())
                                            .isEqualTo(prefix.getFirst().binding().id());
                                    assertThat(selected.notice()).isNull();
                                    assertThat(selected.noticeId()).isNull();
                                    assertThat(selected.noticeRevision()).isNull();
                                    assertThat(selected.noticeBindingRevision()).isNull();
                                    try {
                                        assertThat(
                                                        canLockNowait(
                                                                "channel.bindings",
                                                                selected.bindingId()))
                                                .isFalse();
                                    } catch (SQLException failure) {
                                        throw new IllegalStateException(failure);
                                    }
                                });
                var observed =
                        observe(
                                "all-notices-locked-" + busyCount,
                                () -> {
                                    delivery().scheduled();
                                    return null;
                                });
                observed.evidence()
                        .put(
                                "business_facts",
                                Map.of(
                                        "busy_bindings",
                                        busyCount,
                                        "nullable_notice_preserved",
                                        true,
                                        "send_attempts",
                                        sends.size(),
                                        "tail_sent",
                                        sends.contains(expectedSend(tail)),
                                        "twenty_attempt_limit",
                                        true));
                successful(observed);
                assertThat(observed.sql().count("notice_scheduled_claim_locked_read"))
                        .isEqualTo(busyCount == 2 ? 3 : 20);
                if (busyCount == 2) assertThat(sends).containsExactly(expectedSend(tail));
                else assertThat(sends).isEmpty();
                for (var item : prefix) {
                    assertThat(notice(item).status()).isEqualTo("pending");
                    assertThat(notice(item).reason()).isNull();
                }
            }
            var unlocked =
                    observe(
                            "all-notices-unlocked-" + busyCount,
                            () -> {
                                delivery().scheduled();
                                return null;
                            });
            unlocked.evidence()
                    .put(
                            "business_facts",
                            Map.of(
                                    "send_attempts",
                                    sends.size(),
                                    "oldest_pending_sent",
                                    sends.contains(expectedSend(prefix.getFirst()))));
            successful(unlocked);
            assertThat(sends).contains(expectedSend(prefix.getFirst()));
        }
    }

    @Test
    void independentCoordinatorsCanOverlapDifferentOwnersWithoutDuplicatingOneNotice()
            throws Exception {
        var left = scenario(ALICE, "completed");
        var right = scenario(BOB, "completed");
        delivery().project(left.binding().id());
        delivery().project(right.binding().id());
        var gate = new Gate(2);
        sendGate.set(gate);
        var pool = Executors.newFixedThreadPool(2);
        var a =
                pool.submit(
                        () ->
                                observe(
                                        "coordinator-left",
                                        () -> {
                                            delivery().scheduled();
                                            return null;
                                        }));
        var b =
                pool.submit(
                        () ->
                                observe(
                                        "coordinator-right",
                                        () -> {
                                            delivery().scheduled();
                                            return null;
                                        }));
        try {
            assertThat(gate.entered.await(8, TimeUnit.SECONDS)).isTrue();
            assertThat(sends).containsExactlyInAnyOrder(expectedSend(left), expectedSend(right));
            assertThat(notice(left).status()).isEqualTo("sending");
            assertThat(notice(right).status()).isEqualTo("sending");
        } finally {
            gate.release.countDown();
            pool.shutdown();
            assertThat(pool.awaitTermination(8, TimeUnit.SECONDS)).isTrue();
            sendGate.set(null);
        }
        var first = a.get(8, TimeUnit.SECONDS);
        var second = b.get(8, TimeUnit.SECONDS);
        successful(first);
        successful(second);
        for (var row : List.of(first, second))
            row.evidence()
                    .put(
                            "business_facts",
                            Map.of(
                                    "send_attempts",
                                    1,
                                    "two_sends_overlapped",
                                    true,
                                    "owner_routes_match",
                                    true));
        assertThat(notice(left).status()).isEqualTo("submitted_unverified");
        assertThat(notice(right).status()).isEqualTo("submitted_unverified");
        reset();
        var only = scenario(ALICE, "completed");
        delivery().project(only.binding().id());
        var single = new Gate(1);
        sendGate.set(single);
        var owner = Executors.newSingleThreadExecutor();
        var own =
                owner.submit(
                        () ->
                                observe(
                                        "duplicate-owner",
                                        () -> {
                                            delivery().scheduled();
                                            return null;
                                        }));
        try {
            assertThat(single.entered.await(8, TimeUnit.SECONDS)).isTrue();
            var other =
                    observe(
                            "duplicate-other-coordinator",
                            () -> {
                                delivery().scheduled();
                                return null;
                            });
            other.evidence()
                    .put(
                            "business_facts",
                            Map.of(
                                    "new_send_attempts",
                                    sends.size() - 1,
                                    "notice_remains_sending",
                                    notice(only).status().equals("sending")));
            successful(other);
            assertThat(sends).containsExactly(expectedSend(only));
        } finally {
            single.release.countDown();
            owner.shutdown();
            assertThat(owner.awaitTermination(8, TimeUnit.SECONDS)).isTrue();
            sendGate.set(null);
        }
        var owned = own.get(8, TimeUnit.SECONDS);
        successful(owned);
        owned.evidence()
                .put(
                        "business_facts",
                        Map.of(
                                "send_attempts",
                                1,
                                "notice_submitted_unverified",
                                notice(only).status().equals("submitted_unverified")));
        assertThat(sends).containsExactly(expectedSend(only));
    }

    @Test
    void actualLockedRowsStillRequireTheLaterTurnAndCurrentBindingGates() throws Exception {
        var waiting = scenario(ALICE, "waiting");
        delivery().project(waiting.binding().id());
        var old = notice(waiting);
        var gate = new Gate(1);
        claimGate.active.set(gate);
        var pool = Executors.newSingleThreadExecutor();
        var result =
                pool.submit(
                        () ->
                                observe(
                                        "locked-row-fresh-turn",
                                        () -> {
                                            delivery().scheduled();
                                            return null;
                                        }));
        try {
            assertThat(gate.entered.await(8, TimeUnit.SECONDS)).isTrue();
            assertThat(canLockNowait("channel.bindings", waiting.binding().id())).isFalse();
            assertThat(canLockNowait("channel.notices", old.id())).isFalse();
            var cancelled =
                    tasks.cancel(
                            ALICE,
                            waiting.binding().conversationId(),
                            waiting.turn().id(),
                            "skip-locked-cancel");
            assertThat(cancelled.status()).isEqualTo("cancelled");
            assertThat(cancelled.revision()).isGreaterThan(old.revision());
        } finally {
            gate.release.countDown();
            pool.shutdown();
            assertThat(pool.awaitTermination(8, TimeUnit.SECONDS)).isTrue();
        }
        var observed = result.get(8, TimeUnit.SECONDS);
        successful(observed);
        observed.evidence()
                .put(
                        "business_facts",
                        Map.of(
                                "both_row_locks_observed",
                                true,
                                "new_turn_revision_suppressed",
                                notice(waiting).status().equals("suppressed"),
                                "send_attempts",
                                sends.size()));
        assertThat(notice(waiting).reason()).isEqualTo("revision_changed");
        assertThat(notice(waiting).payload()).isEqualTo(old.payload());
        assertThat(sends).isEmpty();
        for (String change : List.of("/new", "/mute")) {
            reset();
            var sample = scenario(ALICE, "completed");
            delivery().project(sample.binding().id());
            var pending = notice(sample);
            inbound.receive(message(sample.binding(), change));
            var changed =
                    observe(
                            "binding-gate-" + change.substring(1),
                            () -> {
                                delivery().scheduled();
                                return null;
                            });
            successful(changed);
            changed.evidence()
                    .put(
                            "business_facts",
                            Map.of(
                                    "binding_change_suppressed",
                                    notice(sample).status().equals("suppressed"),
                                    "send_attempts",
                                    sends.size()));
            assertThat(notice(sample).reason()).isEqualTo("binding_changed");
            assertThat(notice(sample).payload()).isEqualTo(pending.payload());
            assertThat(sends).isEmpty();
        }
    }

    @Test
    void secondProjectionFailurePreservesTheFirstIndependentCommitAndRetriesSafely()
            throws Exception {
        var first = scenario(ALICE, "completed");
        var second = scenario(BOB, "completed");
        orderTurn(first, 0);
        orderTurn(second, 1);
        installFailureTrigger(second.binding().id());
        var failed =
                observe(
                        "projection-second-fault",
                        () -> {
                            delivery().scheduled();
                            return null;
                        });
        failed.evidence()
                .put(
                        "business_facts",
                        Map.of(
                                "first_projection_committed",
                                noticeCount(first) == 1,
                                "second_projection_rolled_back",
                                noticeCount(second) == 0,
                                "send_attempts",
                                sends.size()));
        assertThat(failed.failure()).hasStackTraceContaining("outbox_skip_query_injected_failure");
        assertThat(failed.sql().commits()).isOne();
        assertThat(failed.sql().rollbacks()).isOne();
        assertThat(sends).isEmpty();
        assertThat(noticeCount(first)).isOne();
        assertThat(noticeCount(second)).isZero();
        dropFailureTrigger();
        var retry =
                observe(
                        "projection-fault-retry",
                        () -> {
                            delivery().scheduled();
                            return null;
                        });
        successful(retry);
        retry.evidence()
                .put(
                        "business_facts",
                        Map.of(
                                "both_notices_present",
                                noticeCount(first) == 1 && noticeCount(second) == 1,
                                "first_route_sent",
                                sends.equals(List.of(expectedSend(first)))));
        assertThat(sends).containsExactly(expectedSend(first));
        var tail =
                observe(
                        "projection-fault-tail",
                        () -> {
                            delivery().scheduled();
                            return null;
                        });
        successful(tail);
        tail.evidence()
                .put(
                        "business_facts",
                        Map.of(
                                "send_attempts",
                                sends.size(),
                                "routes_match",
                                sends.equals(List.of(expectedSend(first), expectedSend(second)))));
        assertThat(sends).containsExactly(expectedSend(first), expectedSend(second));
    }

    @Test
    void defaultPlannerUsesTheLocalPendingIndexWithoutCountingSyntheticRowsAsRealSends()
            throws Exception {
        var seed = scenario(BOB, "completed");
        delivery().scheduled();
        var template = notice(seed);
        assertThat(template.status()).isEqualTo("submitted_unverified");
        createSyntheticPending(seed);
        var tail = scenario(ALICE, "completed");
        delivery().project(tail.binding().id());
        orderNotice(tail, 30000);
        raw.execute("ANALYZE channel.notices");
        raw.execute("ANALYZE channel.bindings");
        try (var held = holdSyntheticBindings()) {
            explainScheduledClaim("scale-without-local-index", false);
            explainScheduledClaim("scale-with-local-index", true);
            assertThat(notice(tail).status()).isEqualTo("pending");
        }
        assertThat(
                        integer(
                                "SELECT count(*)::int FROM channel.notices WHERE id LIKE 'synthetic-outbox-notice-%' AND status='pending'"))
                .isEqualTo(20000);
        assertThat(sends).containsExactly(expectedSend(seed));
        assertThat(
                        Boolean.TRUE.equals(
                                raw.queryForObject(
                                        "SELECT to_regclass('channel.channel_pending_notice_binding') IS NOT NULL",
                                        Boolean.class)))
                .isTrue();
    }

    private List<Scenario> preparedPrefix(int count, boolean projected) throws Exception {
        var result = new ArrayList<Scenario>();
        for (int i = 0; i < count; i++) {
            var item = scenario(ALICE, "completed");
            orderTurn(item, i);
            if (projected) {
                delivery().project(item.binding().id());
                orderNotice(item, i);
            }
            result.add(item);
        }
        return result;
    }

    private void orderNotice(Scenario sample, int ordinal) {
        raw.update(
                "UPDATE channel.notices SET created_at=TIMESTAMPTZ '2000-01-01 00:00:00+00'+?*interval '1 second' WHERE id=?",
                ordinal,
                notice(sample).id());
    }

    private HeldRow holdMany(String table, List<String> ids) throws SQLException {
        var connection = original.getConnection();
        connection.setAutoCommit(false);
        try {
            for (var id : ids)
                try (var statement =
                        connection.prepareStatement(
                                "SELECT id FROM " + lockTable(table) + " WHERE id=? FOR UPDATE")) {
                    statement.setString(1, id);
                    excludedObservations.incrementAndGet();
                    try (var rows = statement.executeQuery()) {
                        assertThat(rows.next()).isTrue();
                    }
                }
            try (var statement = connection.createStatement();
                    var rows = statement.executeQuery("SELECT pg_backend_pid()")) {
                assertThat(rows.next()).isTrue();
                return new HeldRow(connection, rows.getInt(1));
            }
        } catch (SQLException | RuntimeException | Error failure) {
            connection.close();
            throw failure;
        }
    }

    private HeldRow holdSyntheticBindings() throws SQLException {
        var connection = original.getConnection();
        connection.setAutoCommit(false);
        try {
            try (var statement = connection.createStatement();
                    var rows =
                            statement.executeQuery(
                                    "SELECT id FROM channel.bindings WHERE id LIKE 'synthetic-outbox-binding-%' ORDER BY id FOR UPDATE")) {
                int count = 0;
                while (rows.next()) count++;
                assertThat(count).isEqualTo(20000);
            }
            try (var statement = connection.createStatement();
                    var rows = statement.executeQuery("SELECT pg_backend_pid()")) {
                assertThat(rows.next()).isTrue();
                return new HeldRow(connection, rows.getInt(1));
            }
        } catch (SQLException | RuntimeException | Error failure) {
            connection.close();
            throw failure;
        }
    }

    private void createSyntheticPending(Scenario seed) {
        assertThat(
                        raw.update(
                                "INSERT INTO business.conversations(id,tenant,subject) SELECT 'synthetic-outbox-conversation-'||g,tenant,subject "
                                        + "FROM business.conversations CROSS JOIN generate_series(1,20000) g WHERE id=?",
                                seed.binding().conversationId()))
                .isEqualTo(20000);
        assertThat(
                        raw.update(
                                "INSERT INTO business.turns(id,conversation_id,tenant,subject,idempotency_key,request_hash,message,release_hash,status,revision,result,agent_id) "
                                        + "SELECT 'synthetic-outbox-turn-'||g,'synthetic-outbox-conversation-'||g,tenant,subject,'synthetic-key-'||g,request_hash,message,release_hash,'completed',revision,result,agent_id "
                                        + "FROM business.turns CROSS JOIN generate_series(1,20000) g WHERE id=?",
                                seed.turn().id()))
                .isEqualTo(20000);
        assertThat(
                        raw.update(
                                "INSERT INTO channel.bindings(id,tenant_key,open_id,chat_id,tenant,subject,conversation_id,revision,muted,active_turn) "
                                        + "SELECT 'synthetic-outbox-binding-'||g,tenant_key,'synthetic-open-'||g,'synthetic-chat-'||g,tenant,subject,'synthetic-outbox-conversation-'||g,revision,false,'synthetic-outbox-turn-'||g "
                                        + "FROM channel.bindings CROSS JOIN generate_series(1,20000) g WHERE id=?",
                                seed.binding().id()))
                .isEqualTo(20000);
        assertThat(
                        raw.update(
                                "INSERT INTO channel.notices(id,turn_id,binding_id,binding_revision,revision,payload,status,created_at) "
                                        + "SELECT 'synthetic-outbox-notice-'||g,'synthetic-outbox-turn-'||g,'synthetic-outbox-binding-'||g,binding_revision,revision,payload,'pending',TIMESTAMPTZ '2000-01-01 00:00:00+00'+g*interval '1 second' "
                                        + "FROM channel.notices CROSS JOIN generate_series(1,20000) g WHERE id=?",
                                notice(seed).id()))
                .isEqualTo(20000);
    }

    private void explainScheduledClaim(String phase, boolean localIndex) throws Exception {
        var mapped =
                application
                        .getBean(SqlSessionFactory.class)
                        .getConfiguration()
                        .getMappedStatement(
                                FeishuMapper.class.getName() + ".getNextNoticeForUpdateSkipLocked");
        var parameters = Map.of("excluded", List.<String>of());
        var bound = mapped.getBoundSql(parameters);
        JsonNode plan;
        int before = integer("SELECT count(*)::int FROM channel.notices WHERE status='pending'");
        try (var connection = original.getConnection()) {
            connection.setAutoCommit(false);
            try {
                // 仅诊断事务内移除候选索引作默认planner预览；rollback恢复原schema，其他holder只锁bindings。
                if (!localIndex)
                    try (var statement = connection.createStatement()) {
                        statement.execute("DROP INDEX channel.channel_pending_notice_binding");
                    }
                try (var statement =
                        connection.prepareStatement(
                                "EXPLAIN (ANALYZE,BUFFERS,VERBOSE,FORMAT JSON) "
                                        + bound.getSql())) {
                    new DefaultParameterHandler(mapped, parameters, bound).setParameters(statement);
                    try (var rows = statement.executeQuery()) {
                        assertThat(rows.next()).isTrue();
                        plan = json.readTree(rows.getString(1)).get(0).path("Plan");
                    }
                }
            } finally {
                connection.rollback();
            }
        }
        assertThat(plan.path("Actual Rows").asInt()).isOne();
        var safe = new ArrayList<Map<String, Object>>();
        collectSafePlan(plan, "outer", safe);
        var row = new LinkedHashMap<String, Object>();
        row.put("phase", phase);
        row.put("default_planner", true);
        row.put("production_statement", true);
        row.put("preview_without_candidate_index", !localIndex);
        row.put("candidate_local_index_present", localIndex);
        row.put(
                "template_sha256",
                PayloadDigest.sha256(
                        bound.getSql().replaceAll("\\s+", " ").trim().toLowerCase(Locale.ROOT)));
        row.put("synthetic_pending_bindings", 20000);
        row.put("foreign_locked_binding_prefix", 20000);
        row.put("actual_result_rows", 1);
        row.put("raw_transaction_rolled_back", true);
        row.put(
                "pending_rows_unchanged",
                integer("SELECT count(*)::int FROM channel.notices WHERE status='pending'")
                        == before);
        row.put("safe_plan_nodes", safe);
        row.put("root_buffers_include_subtrees", true);
        row.put("no_raw_condition_or_identity_export", true);
        plans.add(row);
        if (localIndex)
            assertThat(safe)
                    .anyMatch(
                            n ->
                                    n.get("index_name").equals("channel_pending_notice_binding")
                                            && Boolean.TRUE.equals(
                                                    n.get("index_condition_contains_binding_id")));
        else
            assertThat(safe)
                    .noneMatch(n -> n.get("index_name").equals("channel_pending_notice_binding"));
        assertThat(integer("SELECT count(*)::int FROM channel.notices WHERE status='pending'"))
                .isEqualTo(before);
    }

    private static void collectSafePlan(
            JsonNode node, String inherited, List<Map<String, Object>> target) {
        String subplan = node.path("Subplan Name").asText();
        String stage =
                subplan.contains("scheduled_notice")
                        ? "scheduled_notice"
                        : subplan.contains("scheduled_binding") ? "scheduled_binding" : inherited;
        var row = new LinkedHashMap<String, Object>();
        row.put("stage", stage);
        row.put("node_type", node.path("Node Type").asText());
        row.put("relation_name", node.path("Relation Name").asText(""));
        row.put("index_name", node.path("Index Name").asText(""));
        row.put("actual_rows", node.path("Actual Rows").asDouble());
        row.put("actual_loops", node.path("Actual Loops").asDouble());
        row.put("rows_removed_by_filter", node.path("Rows Removed by Filter").asDouble());
        row.put("shared_hit_blocks", node.path("Shared Hit Blocks").asLong());
        row.put("shared_read_blocks", node.path("Shared Read Blocks").asLong());
        row.put("shared_dirtied_blocks", node.path("Shared Dirtied Blocks").asLong());
        row.put("shared_written_blocks", node.path("Shared Written Blocks").asLong());
        row.put("heap_fetches", node.path("Heap Fetches").asLong());
        row.put(
                "index_condition_contains_binding_id",
                node.path("Index Cond").asText().contains("binding_id"));
        target.add(row);
        for (var child : node.path("Plans")) collectSafePlan(child, stage, target);
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

    private static void successful(Observed<?> observed) {
        assertThat(observed.failure()).isNull();
        assertThat(observed.sql().count("other")).isZero();
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

    private static Send expectedSend(Scenario scenario) {
        return new Send(scenario.binding().chatId(), scenario.turn().result());
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
                "CREATE FUNCTION channel.outbox_skip_query_fail() RETURNS trigger LANGUAGE plpgsql AS $$ "
                        + "BEGIN RAISE EXCEPTION 'outbox_skip_query_injected_failure'; END; $$");
        raw.execute(
                "CREATE TRIGGER outbox_skip_query_fail AFTER INSERT ON channel.notices FOR EACH ROW WHEN (NEW.binding_id='"
                        + binding
                        + "') EXECUTE FUNCTION channel.outbox_skip_query_fail()");
    }

    private void dropFailureTrigger() {
        if (raw != null)
            raw.execute("DROP FUNCTION IF EXISTS channel.outbox_skip_query_fail() CASCADE");
    }

    private void closeResources() {
        try {
            if (application != null) application.close();
        } finally {
            postgres.stop();
        }
    }
}
