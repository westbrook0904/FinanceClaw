package com.financeclaw.p3;

import static org.assertj.core.api.Assertions.*;
import static org.mockito.ArgumentMatchers.*;
import static org.mockito.Mockito.*;

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
import com.financeclaw.domain.Task.Owner;
import com.financeclaw.domain.Task.Turn;
import com.financeclaw.fixture.GovernanceFixture;
import com.financeclaw.fixture.TaskFixture;
import com.financeclaw.infrastructure.agentscope.capability.BusinessCapabilities;
import com.financeclaw.infrastructure.agentscope.execution.TaskAgentRuntime;
import com.financeclaw.infrastructure.channel.FeishuMapper;
import com.financeclaw.infrastructure.governance.GovernanceMapper;
import com.financeclaw.infrastructure.state.RuntimeMapper;
import com.financeclaw.m1.TaskWorkerFixture;
import com.financeclaw.m2.ScriptedRuntime;
import com.financeclaw.support.PayloadDigest;
import com.financeclaw.support.PayloadJson;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Locale;
import java.util.Map;
import java.util.UUID;
import java.util.concurrent.atomic.AtomicBoolean;
import java.util.function.Supplier;
import javax.sql.DataSource;
import org.apache.ibatis.session.SqlSessionFactory;
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
 * 飞书通知的真实 PG 查询基线：正常绑定/任务/交互/投影/领取/落库均使用生产服务。 仅最末端 AgentScopeFeishu.send 是协议边界替身，本类不声称验证原生飞书 HTTP
 * SDK；该契约由 FeishuIT 保留。 所有受理与执行 fixture 均位于计数窗口之外，不伪造 Turn/Command/Attempt，也不运行后台轮询。
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
@Execution(ExecutionMode.SAME_THREAD)
class FeishuDeliveryQueryIT {
    private static final Owner ALICE = new Owner("feishu-query-test", "alice");
    private static final Owner BOB = new Owner("feishu-query-test", "bob");
    private static final String TENANT_KEY = "feishu-query-tenant";
    private final ObjectMapper json = new ObjectMapper();
    private final PostgreSQLContainer<?> postgres =
            new PostgreSQLContainer<>("postgres:16.10-alpine");
    private final List<Map<String, Object>> measurements = new ArrayList<>();
    private final List<Send> sends = new ArrayList<>();
    private final AtomicBoolean failSend = new AtomicBoolean();
    private ConfigurableApplicationContext application;
    private JdbcExecutionCounter counter;
    private JdbcTemplate raw;
    private TaskFixture tasks;
    private GovernanceFixture governance;
    private FeishuMapper db;
    private FeishuService inbound;
    private FeishuSettings settings;
    private AgentScopeFeishu sender;
    private String bindingTemplate;
    private String lockedClaimTemplate;
    private String interactionTemplate;

    record Send(String chat, String text) {}

    record Scenario(Owner owner, Binding binding, Turn turn) {}

    record Observed<T>(T value, Throwable failure, JdbcExecutionCounter.Measurement sql) {}

    static final class RawSource {
        private DataSource source;
    }

    @TestConfiguration(proxyBeanMethods = false)
    @Import({TaskFixture.class, GovernanceFixture.class})
    static class CountingConfiguration {
        @Bean
        static JdbcExecutionCounter feishuJdbcCounter() {
            return new JdbcExecutionCounter();
        }

        @Bean
        static RawSource feishuRawSource() {
            return new RawSource();
        }

        @Bean
        static BeanPostProcessor feishuCountingSource(JdbcExecutionCounter counter, RawSource raw) {
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
            if (Boolean.getBoolean("financeclaw.test.query-baseline")) {
                throw new IllegalArgumentException(
                        "P3a.13 delivery regression is after-only; use NoticeClaimQueryIT for baseline");
            }
            postgres.start();
            application =
                    new SpringApplicationBuilder(
                                    FinanceClawApplication.class, CountingConfiguration.class)
                            .web(WebApplicationType.SERVLET)
                            .run(
                                    "--spring.datasource.url=" + postgres.getJdbcUrl(),
                                    "--spring.datasource.username=" + postgres.getUsername(),
                                    "--spring.datasource.password=" + postgres.getPassword(),
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
                                    "--financeclaw.auth.token=synthetic-feishu-query-token",
                                    "--financeclaw.feishu.enabled=true",
                                    "--financeclaw.feishu.app-id=query-test",
                                    "--financeclaw.feishu.app-secret=synthetic-query-secret",
                                    "--financeclaw.feishu.verification-token=synthetic-query-token",
                                    "--financeclaw.feishu.encrypt-key=synthetic-query-key",
                                    "--financeclaw.feishu.tenant-key=" + TENANT_KEY,
                                    "--financeclaw.business.ziwei.enabled=false");
            counter = application.getBean(JdbcExecutionCounter.class);
            raw = new JdbcTemplate(application.getBean(RawSource.class).source);
            tasks = application.getBean(TaskFixture.class);
            governance = application.getBean(GovernanceFixture.class);
            db = application.getBean(FeishuMapper.class);
            inbound = application.getBean(FeishuService.class);
            settings = application.getBean(FeishuSettings.class);
            sender = mock(AgentScopeFeishu.class);
            // 从生产 MyBatis statement 得到同一模板摘要，校验标签之外的真实 SQL 形状。
            var configuration = application.getBean(SqlSessionFactory.class).getConfiguration();
            bindingTemplate =
                    digest(
                            configuration
                                    .getMappedStatement(
                                            FeishuMapper.class.getName() + ".getBindingForUpdate")
                                    .getBoundSql("binding")
                                    .getSql());
            lockedClaimTemplate =
                    digest(
                            configuration
                                    .getMappedStatement(
                                            FeishuMapper.class.getName()
                                                    + ".getBindingAndNoticeForUpdate")
                                    .getBoundSql(
                                            Map.of("bindingId", "binding", "noticeId", "notice"))
                                    .getSql());
            interactionTemplate =
                    digest(
                            configuration
                                    .getMappedStatement(
                                            GovernanceMapper.class.getName()
                                                    + ".listPendingInteractionsByTurnId")
                                    .getBoundSql("turn")
                                    .getSql());
        } catch (RuntimeException | Error failure) {
            closeResources();
            throw failure;
        }
    }

    @BeforeEach
    void reset() {
        raw.execute(
                "TRUNCATE agent_runtime.states,business.conversations,business.admission,"
                        + "memory.jobs,memory.owners,memory.operations,memory.audit CASCADE");
        sends.clear();
        failSend.set(false);
        org.mockito.Mockito.reset(sender);
        doAnswer(
                        call -> {
                            // 发送事务边界也保留：拿到 sending 资格后必须先提交，再进入外部发送。
                            assertThat(
                                            TransactionSynchronizationManager
                                                    .isActualTransactionActive())
                                    .isFalse();
                            sends.add(new Send(call.getArgument(0), call.getArgument(1)));
                            if (failSend.get())
                                throw new IllegalStateException("synthetic_transport_failure");
                            return null;
                        })
                .when(sender)
                .send(anyString(), anyString());
    }

    @AfterAll
    void stopAndWriteMeasurements() throws Exception {
        try {
            Path output = Path.of("target/p3a13/feishu-delivery-sql.json");
            Files.createDirectories(output.getParent());
            var report = new LinkedHashMap<String, Object>();
            report.put("schema", "feishu-delivery-sql/1");
            report.put("scope", "real-pg-production-services-send-boundary-mock");
            report.put("phase", "P3a.13");
            report.put("mode", "after");
            report.put("background_pollers_enabled", false);
            report.put("sql_boundary", "jdbc-execute-only-fixture-and-assertions-excluded");
            report.put("measurements", measurements);
            json.writerWithDefaultPrettyPrinter().writeValue(output.toFile(), report);
        } finally {
            closeResources();
        }
    }

    @ParameterizedTest
    @ValueSource(strings = {"completed", "waiting"})
    void scheduledFirstProjectionReadsOnlyRequiredInteractionsAndSendsOnce(String state)
            throws Exception {
        var scenario = scenario(ALICE, state);
        var delivery = delivery();
        int expected = state.equals("waiting") ? 10 : 9;
        var first =
                observe(
                        "scheduled-first-" + state,
                        expected,
                        () -> {
                            delivery.scheduled();
                            return null;
                        });
        assertSql(first, expected);
        assertThat(templateCount(first, bindingTemplate)).isOne();
        assertThat(templateCount(first, lockedClaimTemplate)).isOne();
        assertThat(first.sql().count("notice_claim_locked_read")).isOne();
        assertThat(templateCount(first, interactionTemplate))
                .isEqualTo(state.equals("waiting") ? 1 : 0);
        assertThat(first.sql().count("notice_projection_scan")).isOne();
        assertThat(first.sql().count("notice_claim_update")).isOne();
        assertThat(first.sql().count("notice_expired_recovery")).isZero();
        assertThat(sends).hasSize(1);
        assertThat(sends.getFirst().chat()).isEqualTo(scenario.binding().chatId());
        if (state.equals("waiting")) assertThat(sends.getFirst().text()).contains("请输入城市");
        else assertThat(sends.getFirst().text()).isEqualTo(scenario.turn().result());
        assertNoticeStatus(scenario, "submitted_unverified", "native_sdk_no_receipt");
        var repeated =
                observe(
                        "scheduled-repeat-" + state,
                        2,
                        () -> {
                            delivery.scheduled();
                            return null;
                        });
        assertSql(repeated, 2);
        assertThat(sends).hasSize(1);
        assertThat(noticeCount(scenario)).isOne();
    }

    @Test
    void maintenancePollIncludesRecoveryInItsOwnExplicitBudget() throws Exception {
        var scenario = scenario(ALICE, "completed");
        var delivery = delivery();
        int expected = 10;
        var poll =
                observe(
                        "maintenance-first-completed",
                        expected,
                        () -> {
                            delivery.poll();
                            return null;
                        });
        assertSql(poll, expected);
        assertThat(poll.sql().count("notice_expired_recovery")).isOne();
        assertThat(sends).hasSize(1);
        assertNoticeStatus(scenario, "submitted_unverified", "native_sdk_no_receipt");
    }

    @Test
    void emptyScheduledTicksBackOffWithoutSuppressingIndependentRecoveryOrMaintenance() {
        var delivery = delivery();
        var first =
                observe(
                        "empty-scheduled-first",
                        2,
                        () -> {
                            delivery.scheduled();
                            return null;
                        });
        var skipped =
                observe(
                        "empty-scheduled-backed-off",
                        0,
                        () -> {
                            delivery.scheduled();
                            delivery.scheduled();
                            return null;
                        });
        var recovered =
                observe(
                        "empty-independent-recovery",
                        1,
                        () -> {
                            delivery.scheduledRecovery();
                            return null;
                        });
        var explicit =
                observe(
                        "empty-explicit-maintenance",
                        3,
                        () -> {
                            delivery.poll();
                            return null;
                        });
        // 所有样本先落盘，再断性能；回归变红仍保留完整观察。before 由独立 NoticeClaimQueryIT 记录。
        assertSql(first, 2);
        assertSql(skipped, 0);
        assertSql(recovered, 1);
        assertSql(explicit, 3);
        assertThat(recovered.sql().count("notice_expired_recovery")).isOne();
        assertThat(sends).isEmpty();
    }

    @Test
    void repeatedProjectionAndANewCoordinatorReuseTheSameDurableNotice() throws Exception {
        var scenario = scenario(ALICE, "completed");
        var original = delivery();
        int projectionCost = 3;
        var first =
                observe(
                        "project-first-completed",
                        projectionCost,
                        () -> {
                            original.project(scenario.binding().id());
                            return null;
                        });
        var repeated =
                observe(
                        "project-repeat-completed",
                        projectionCost,
                        () -> {
                            original.project(scenario.binding().id());
                            return null;
                        });
        assertSql(first, projectionCost);
        assertSql(repeated, projectionCost);
        assertThat(noticeCount(scenario)).isOne();
        var restarted = delivery();
        int pendingCost = 6;
        var pending =
                observe(
                        "restarted-pending-send",
                        pendingCost,
                        () -> {
                            restarted.scheduled();
                            return null;
                        });
        assertSql(pending, pendingCost);
        assertThat(pending.sql().count("notice_insert")).isZero();
        assertThat(sends).hasSize(1);
        var replay =
                observe(
                        "restarted-submitted-remains-sealed",
                        2,
                        () -> {
                            delivery().scheduled();
                            return null;
                        });
        assertSql(replay, 2);
        assertThat(sends).hasSize(1);
        assertNoticeStatus(scenario, "submitted_unverified", "native_sdk_no_receipt");
    }

    @ParameterizedTest
    @ValueSource(strings = {"transport-failure", "worker-lost"})
    void uncertainSendsRemainUnknownAcrossCoordinatorRestartWithoutAutomaticRetry(String cause)
            throws Exception {
        var scenario = scenario(ALICE, "completed");
        if (cause.equals("transport-failure")) {
            failSend.set(true);
            int expected = 9;
            var failed =
                    observe(
                            "transport-failure-becomes-unknown",
                            expected,
                            () -> {
                                delivery().scheduled();
                                return null;
                            });
            assertSql(failed, expected);
            assertThat(sends).hasSize(1);
            assertNoticeStatus(scenario, "unknown", "native_send_unconfirmed");
        } else {
            var previous = delivery();
            previous.project(scenario.binding().id());
            assertThat(previous.claim()).isNotNull();
            // 只推进通知的失联时刻；任务身份、命令和 attempt 均未被测试手工改写。
            assertThat(
                            raw.update(
                                    "UPDATE channel.notices SET started_at=clock_timestamp()-interval '61 seconds' "
                                            + "WHERE turn_id=? AND status='sending'",
                                    scenario.turn().id()))
                    .isOne();
            var recovery =
                    observe(
                            "restarted-lost-sender-recovery",
                            1,
                            () -> {
                                delivery().scheduledRecovery();
                                return null;
                            });
            assertSql(recovery, 1);
            assertNoticeStatus(scenario, "unknown", "worker_lost");
            assertThat(sends).isEmpty();
        }
        int attempts = sends.size();
        failSend.set(false);
        var replay =
                observe(
                        "unknown-not-requeued-" + cause,
                        3,
                        () -> {
                            delivery().poll();
                            return null;
                        });
        assertSql(replay, 3);
        assertThat(sends).hasSize(attempts);
        assertThat(
                        integer(
                                "SELECT count(*) FROM channel.notices WHERE turn_id=? AND status='pending'",
                                scenario.turn().id()))
                .isZero();
    }

    @Test
    void projectionBatchIsBoundedAndFiniteTwoOwnerBacklogProgressesWithoutCrossDelivery()
            throws Exception {
        var scenarios = new ArrayList<Scenario>();
        for (int i = 0; i < 22; i++) {
            scenarios.add(scenario(i == 21 ? BOB : ALICE, "completed"));
        }
        var delivery = delivery();
        int firstCost = 66;
        var first =
                observe(
                        "twenty-binding-projection-boundary",
                        firstCost,
                        () -> {
                            delivery.scheduled();
                            return null;
                        });
        assertSql(first, firstCost);
        assertThat(first.sql().count("notice_insert")).isEqualTo(20);
        assertThat(integer("SELECT count(*) FROM channel.notices")).isEqualTo(20);
        assertThat(sends).hasSize(1);
        int secondCost = 12;
        var second =
                observe(
                        "remaining-two-binding-projection",
                        secondCost,
                        () -> {
                            delivery.scheduled();
                            return null;
                        });
        assertSql(second, secondCost);
        assertThat(second.sql().count("notice_insert")).isEqualTo(2);
        assertThat(integer("SELECT count(*) FROM channel.notices")).isEqualTo(22);
        assertThat(delivery.list(BOB)).hasSize(1);
        assertThat(delivery.list(ALICE)).hasSize(21);
        // 全局 created_at 顺序不承诺 owner 轮询交替；验证有限积压最终可处理，且一轮最多发送一条。
        int tailCost = 120;
        var tail =
                observe(
                        "finite-two-owner-pending-drain",
                        tailCost,
                        () -> {
                            for (int i = 0; i < 20; i++) {
                                int previous = sends.size();
                                delivery.scheduled();
                                assertThat(sends.size() - previous).isEqualTo(1);
                            }
                            return null;
                        });
        assertSql(tail, tailCost);
        assertThat(sends).hasSize(22);
        for (var scenario : scenarios) {
            assertThat(
                            sends.stream()
                                    .filter(s -> s.chat().equals(scenario.binding().chatId()))
                                    .toList())
                    .containsExactly(
                            new Send(scenario.binding().chatId(), scenario.turn().result()));
            assertNoticeStatus(scenario, "submitted_unverified", "native_sdk_no_receipt");
        }
    }

    @Test
    void stalePendingCandidatesHaveABoundedTwentyCandidateMaintenanceBudget() throws Exception {
        var stale = new ArrayList<Scenario>();
        for (int i = 0; i < 21; i++) stale.add(staleBindingNotice(ALICE));
        var valid = scenario(BOB, "completed");
        var delivery = delivery();
        delivery.project(valid.binding().id());
        int firstCost = 62;
        var first =
                observe(
                        "stale-queue-maintenance-first",
                        firstCost,
                        () -> {
                            delivery.poll();
                            return null;
                        });
        assertSql(first, firstCost);
        assertThat(sends).isEmpty();
        assertThat(integer("SELECT count(*) FROM channel.notices WHERE status='suppressed'"))
                .isEqualTo(20);
        assertThat(first.sql().count("notice_pending_read")).isEqualTo(20);
        int secondCost = 10;
        var second =
                observe(
                        "stale-queue-maintenance-second",
                        secondCost,
                        () -> {
                            delivery.poll();
                            return null;
                        });
        assertSql(second, secondCost);
        assertThat(integer("SELECT count(*) FROM channel.notices WHERE status='suppressed'"))
                .isEqualTo(21);
        assertThat(sends).hasSize(1);
        int remainingCost = 0;
        var remaining =
                observe(
                        "stale-queue-remaining-progress",
                        remainingCost,
                        () -> {
                            return null;
                        });
        assertSql(remaining, remainingCost);
        assertThat(sends)
                .containsExactly(new Send(valid.binding().chatId(), valid.turn().result()));
        for (var old : stale) assertNoticeStatus(old, "suppressed", "binding_changed");
        var repeated =
                observe(
                        "stale-queue-no-uncertain-resend",
                        3,
                        () -> {
                            delivery.poll();
                            return null;
                        });
        assertSql(repeated, 3);
        assertThat(sends).hasSize(1);
    }

    @Test
    void publicClaimStillExaminesOneStaleCandidateAndReturnsNull() throws Exception {
        var stale = staleBindingNotice(ALICE);
        var valid = scenario(BOB, "completed");
        var delivery = delivery();
        delivery.project(valid.binding().id());
        var claimed = observe("public-claim-one-stale", 3, delivery::claim);
        assertSql(claimed, 3);
        assertThat(claimed.value()).isNull();
        assertThat(claimed.sql().count("notice_pending_read")).isOne();
        assertNoticeStatus(stale, "suppressed", "binding_changed");
        assertNoticeStatus(valid, "pending", null);
        assertThat(sends).isEmpty();
    }

    /** 单独创建启用的协调器；Spring 自带实例因 integrations=false 不会在背景与计数窗口竞争。 */
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

    private Scenario scenario(Owner owner, String state) throws Exception {
        String conversation = tasks.createConversation(owner);
        String channelId = UUID.randomUUID().toString();
        var identity =
                new Binding(
                        "unused",
                        TENANT_KEY,
                        "open-" + channelId,
                        "chat-" + channelId,
                        owner.tenant(),
                        owner.subject(),
                        conversation,
                        1,
                        false,
                        null);
        inbound.receive(message(identity, "/bind " + inbound.link(owner, conversation)));
        String question = state.equals("waiting") ? "city" : "hello " + UUID.randomUUID();
        inbound.receive(message(identity, question));
        var bound =
                db.getBindingByChannelIdentityForUpdate(
                        TENANT_KEY, identity.openId(), identity.chatId());
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
            var claim = tasks.claim("feishu-query-test", 60000);
            assertThat(claim).isNotNull();
            assertThat(claim.turn().id()).isEqualTo(bound.activeTurn());
            worker.execute(claim);
        }
        var turn = tasks.get(owner, conversation, bound.activeTurn());
        assertThat(turn.status())
                .as("normal fixture runtime result: %s", turn.reason())
                .isEqualTo(state);
        return new Scenario(owner, bound, turn);
    }

    private Scenario staleBindingNotice(Owner owner) throws Exception {
        var scenario = scenario(owner, "completed");
        delivery().project(scenario.binding().id());
        inbound.receive(message(scenario.binding(), "/new"));
        var changed = db.getBindingForUpdate(scenario.binding().id());
        assertThat(changed.revision()).isEqualTo(scenario.binding().revision() + 1);
        assertThat(changed.activeTurn()).isNull();
        assertThat(changed.conversationId()).isNotEqualTo(scenario.binding().conversationId());
        return scenario;
    }

    private com.fasterxml.jackson.databind.JsonNode message(Binding identity, String text) {
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
                                TENANT_KEY,
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

    private <T> Observed<T> observe(String phase, int expected, Supplier<T> operation) {
        counter.begin(phase);
        long started = System.nanoTime();
        T value = null;
        Throwable failure = null;
        try {
            value = operation.get();
        } catch (Throwable caught) {
            failure = caught;
        }
        var measured = counter.finish();
        var row = new LinkedHashMap<String, Object>();
        row.put("phase", phase);
        row.put("expected_statements", expected);
        row.put("elapsed_nanos", System.nanoTime() - started);
        row.put("measurement", measured);
        if (failure != null) row.put("failure_type", failure.getClass().getSimpleName());
        measurements.add(row);
        return new Observed<>(value, failure, measured);
    }

    private static void assertSql(Observed<?> measured, int expected) {
        assertThat(measured.failure()).isNull();
        assertThat(measured.sql().statements()).as(measured.sql().phase()).isEqualTo(expected);
        assertThat(measured.sql().executions())
                .allMatch(e -> e.templateSha256().matches("[0-9a-f]{64}"));
    }

    private static long templateCount(Observed<?> measured, String digest) {
        return measured.sql().executions().stream()
                .filter(e -> e.templateSha256().equals(digest))
                .count();
    }

    private static String digest(String sql) {
        return PayloadDigest.sha256(sql.replaceAll("\\s+", " ").trim().toLowerCase(Locale.ROOT));
    }

    private void assertNoticeStatus(Scenario scenario, String status, String reason) {
        var notice =
                raw.queryForMap(
                        "SELECT status,reason,binding_id,binding_revision FROM channel.notices "
                                + "WHERE turn_id=? AND revision=?",
                        scenario.turn().id(),
                        scenario.turn().revision());
        assertThat(notice.get("status")).isEqualTo(status);
        assertThat(notice.get("reason")).isEqualTo(reason);
        assertThat(notice.get("binding_id")).isEqualTo(scenario.binding().id());
        assertThat(notice.get("binding_revision")).isEqualTo(scenario.binding().revision());
    }

    private int noticeCount(Scenario scenario) {
        return integer(
                "SELECT count(*) FROM channel.notices WHERE turn_id=?", scenario.turn().id());
    }

    private int integer(String sql, Object... parameters) {
        return raw.queryForObject(sql, Integer.class, parameters);
    }

    private void closeResources() {
        try {
            if (application != null) application.close();
        } finally {
            postgres.stop();
        }
    }
}
