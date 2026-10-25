package com.financeclaw.p3;

import static org.assertj.core.api.Assertions.*;
import static org.mockito.ArgumentMatchers.*;
import static org.mockito.Mockito.*;

import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import com.financeclaw.app.execution.port.ExecutionContextGateway;
import com.financeclaw.app.operations.AdmissionService;
import com.financeclaw.app.operations.OperationsQueryService;
import com.financeclaw.app.task.TaskQueryService;
import com.financeclaw.app.task.WorkSignals;
import com.financeclaw.bootstrap.FinanceClawApplication;
import com.financeclaw.channel.AgentScopeFeishu;
import com.financeclaw.channel.FeishuDelivery;
import com.financeclaw.channel.FeishuService;
import com.financeclaw.channel.FeishuSettings;
import com.financeclaw.channel.FeishuText;
import com.financeclaw.domain.Feishu.Binding;
import com.financeclaw.domain.Feishu.Notice;
import com.financeclaw.domain.Governance.Answer;
import com.financeclaw.domain.Governance.Interaction;
import com.financeclaw.domain.Operations.TurnBacklog;
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
import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.UUID;
import java.util.concurrent.CopyOnWriteArrayList;
import java.util.concurrent.Executors;
import java.util.concurrent.TimeUnit;
import javax.sql.DataSource;
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
 * 通知发现过滤的真实 PG 基线。正常绑定、原生 SDK 暂停/审批/恢复、投影和领取均走生产服务； 只在最终 send 边界替换外部 SDK。背景轮询关闭，观察实际 scheduled/poll
 * 方法，不伪造 Turn/Notice 结果。
 *
 * <p>全过期 waiting 前缀的 before/after 进展刻意不同，不能把不同工作量叫作同业务结果优化。 发现集合仍不是授权或发送资格；扫描后的静音、重绑、时间和 Turn
 * revision 变化必须由原锁后投影再检查。 原 NoticeClaimQueryIT 继续独立验收逐项事务、第二项故障前项提交及重试，本类不合并投影事务。
 *
 * <p>规模计划是独立实验：20,000 条明确标记的合成 answered 历史只用于 EXPLAIN，引用真实已完成样本的外键， 不用来声称服务真实生成了这些调用，也不交给 SDK
 * 恢复。报告仅导出安全节点统计/索引名/派生布尔， 不导出 Filter/Index Cond 文本、正文、ID 或 owner，不宣称 expires_at 可作为 VOLATILE
 * 时钟范围索引条件。
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
@Execution(ExecutionMode.SAME_THREAD)
class ProjectionDiscoveryQueryIT {
    private static final Owner ALICE = new Owner("projection-discovery-a", "alice");
    private static final Owner BOB = new Owner("projection-discovery-b", "bob");
    private static final String CHANNEL_TENANT = "projection-discovery-channel";
    private static final String INDEX_NAME = "interaction_pending_turn_expiry";
    private static final int SYNTHETIC_ROWS = 20_000;
    private final boolean baseline =
            Boolean.getBoolean("financeclaw.test.projection-discovery-baseline");
    private final PostgreSQLContainer<?> postgres =
            new PostgreSQLContainer<>("postgres:16.10-alpine");
    private final ObjectMapper json = new ObjectMapper();
    private final List<Map<String, Object>> measurements = new CopyOnWriteArrayList<>();
    private final List<Map<String, Object>> operationsObservations = new ArrayList<>();
    private final List<Map<String, Object>> scaleExperiments = new ArrayList<>();
    private final List<Send> sends = new CopyOnWriteArrayList<>();
    private ConfigurableApplicationContext application;
    private JdbcTemplate raw;
    private JdbcExecutionCounter counter;
    private ProjectionDiscoveryRecorder recorder;
    private TaskFixture tasks;
    private GovernanceFixture governance;
    private FeishuMapper db;
    private FeishuService inbound;
    private FeishuSettings settings;
    private AgentScopeFeishu sender;

    enum Mode {
        CITY("city"),
        BATCH("batch"),
        COMPLETED("hello");
        final String message;

        Mode(String message) {
            this.message = message;
        }
    }

    record Send(String chat, String text) {}

    record Scenario(Owner owner, Binding binding, Turn turn, Mode mode) {}

    record Observed(
            Throwable failure,
            JdbcExecutionCounter.Measurement sql,
            ProjectionDiscoveryRecorder.Measurement discovery,
            Map<String, Object> evidence) {}

    static final class RawSource {
        private DataSource source;
    }

    @TestConfiguration(proxyBeanMethods = false)
    @Import({TaskFixture.class, GovernanceFixture.class})
    static class CountingConfiguration {
        @Bean
        static JdbcExecutionCounter discoveryCounter() {
            return new JdbcExecutionCounter();
        }

        @Bean
        static ProjectionDiscoveryRecorder discoveryRecorder() {
            return new ProjectionDiscoveryRecorder();
        }

        @Bean
        static RawSource discoveryRawSource() {
            return new RawSource();
        }

        @Bean
        static BeanPostProcessor discoverySource(JdbcExecutionCounter counter, RawSource raw) {
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
        if (Boolean.getBoolean("financeclaw.test.query-baseline"))
            throw new IllegalStateException("Use projection-discovery-baseline");
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
                                    "--financeclaw.auth.token=synthetic-discovery-token",
                                    "--financeclaw.feishu.enabled=true",
                                    "--financeclaw.feishu.app-id=discovery-query",
                                    "--financeclaw.feishu.app-secret=synthetic-discovery-secret",
                                    "--financeclaw.feishu.verification-token=synthetic-discovery-token",
                                    "--financeclaw.feishu.encrypt-key=synthetic-discovery-key",
                                    "--financeclaw.feishu.tenant-key=" + CHANNEL_TENANT,
                                    "--financeclaw.business.ziwei.enabled=false");
            raw = new JdbcTemplate(application.getBean(RawSource.class).source);
            counter = application.getBean(JdbcExecutionCounter.class);
            recorder = application.getBean(ProjectionDiscoveryRecorder.class);
            tasks = application.getBean(TaskFixture.class);
            governance = application.getBean(GovernanceFixture.class);
            db = application.getBean(FeishuMapper.class);
            inbound = application.getBean(FeishuService.class);
            settings = application.getBean(FeishuSettings.class);
            sender = mock(AgentScopeFeishu.class);
            var configuration = application.getBean(SqlSessionFactory.class).getConfiguration();
            assertThat(configuration.getInterceptors()).contains(recorder);
            assertThat(ProjectionDiscoveryRecorder.OLD_HASH)
                    .isEqualTo("d8847a0f5b8aa38bde15e6c4b21c55f9ca0c1359373afc0deb5b3d9cd676fb4d");
            assertThat(ProjectionDiscoveryRecorder.NEW_HASH)
                    .isEqualTo("c5cc80c1add98a6ec2f4f5a7f6df409f32344e865aa75e097934fa8afc07a492");
            String sql =
                    configuration
                            .getMappedStatement(ProjectionDiscoveryRecorder.STATEMENT)
                            .getBoundSql(null)
                            .getSql();
            assertThat(ProjectionDiscoveryRecorder.digest(sql))
                    .isEqualTo(
                            baseline
                                    ? ProjectionDiscoveryRecorder.OLD_HASH
                                    : ProjectionDiscoveryRecorder.NEW_HASH);
        } catch (RuntimeException | Error failure) {
            closeResources();
            throw failure;
        }
    }

    @BeforeEach
    void reset() {
        resetData();
    }

    /** 子场景也可在没有观察窗口/后台任务时清理整个隔离库；不清除已保存的测量。 */
    private void resetData() {
        raw.execute(
                "TRUNCATE agent_runtime.states,business.conversations,business.admission,memory.jobs,memory.owners,memory.operations,memory.audit CASCADE");
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

    @AfterAll
    void writeAndClose() throws Exception {
        try {
            Path output = Path.of("target/p3a17/projection-discovery-sql.json");
            Files.createDirectories(output.getParent());
            var report = new LinkedHashMap<String, Object>();
            report.put("schema", "projection-discovery-sql/1");
            report.put("phase", "P3a.17");
            report.put("mode", baseline ? "before" : "after");
            report.put("scope", "real-pg-native-waiting-production-discovery-terminal-send-mock");
            report.put("background_pollers_enabled", false);
            report.put(
                    "sql_boundary",
                    "scheduled-or-maintenance-only-fixture-readback-operations-and-explain-excluded");
            report.put(
                    "intentional_progress_differences",
                    List.of("expired-only-idle", "twenty-expired-prefix-other-owner"));
            report.put("no_wire_capacity_or_global_owner_fairness_claim", true);
            report.put("measurements", List.copyOf(measurements));
            report.put("operations_observations", List.copyOf(operationsObservations));
            report.put("scale_experiments", List.copyOf(scaleExperiments));
            json.writerWithDefaultPrettyPrinter().writeValue(output.toFile(), report);
        } finally {
            closeResources();
        }
    }

    @Test
    void oneThenTwentyFullyExpiredWaitingBindingsCanBecomeIdleWithoutHidingPendingBacklog()
            throws Exception {
        var expired = new ArrayList<Scenario>();
        var delivery = delivery();
        for (int target : List.of(1, 20)) {
            while (expired.size() < target) {
                var scenario = scenario(ALICE, Mode.CITY);
                order(scenario, expired.size());
                expireAll(scenario);
                expired.add(scenario);
            }
            String prefix = target == 1 ? "single-expired" : "twenty-expired";
            var first = observe(prefix + "-first", delivery::scheduled);
            first.evidence().put("business_facts", emptyFacts(expired));
            assertSql(
                    first,
                    baseline ? 3 * target + 2 : 2,
                    baseline ? target : 0,
                    baseline ? target : 0);
            var repeated = observe(prefix + "-repeat", delivery::scheduled);
            repeated.evidence().put("business_facts", emptyFacts(expired));
            assertSql(
                    repeated,
                    baseline ? 3 * target + 2 : 0,
                    baseline ? target : null,
                    baseline ? target : 0);
            var maintenance = observe(prefix + "-maintenance", delivery::poll);
            maintenance.evidence().put("business_facts", emptyFacts(expired));
            assertSql(
                    maintenance,
                    baseline ? 3 * target + 3 : 3,
                    baseline ? target : 0,
                    baseline ? target : 0);
            assertThat(maintenance.sql().count("notice_expired_recovery")).isOne();
            assertExpiredOperations(prefix, target);
        }
        var old = expired.getFirst();
        long generation =
                application.getBean(WorkSignals.class).generation(WorkSignals.Queue.NOTICE);
        inbound.receive(message(old.binding(), "/cancel"));
        inbound.receive(message(old.binding(), "city"));
        var fresh = execute(ALICE, binding(old), Mode.CITY);
        assertThat(fresh.turn().id()).isNotEqualTo(old.turn().id());
        assertThat(application.getBean(WorkSignals.class).generation(WorkSignals.Queue.NOTICE))
                .isGreaterThan(generation);
        var woke = observe("new-waiting-wakeup", delivery::scheduled);
        woke.evidence()
                .put(
                        "business_facts",
                        Map.of(
                                "same_coordinator_woke_for_real_waiting_commit",
                                sends.size() == 1,
                                "new_turn_notice_identity_matches",
                                notice(fresh).turnId().equals(fresh.turn().id()),
                                "route_matches",
                                sends.equals(List.of(expected(fresh)))));
        assertSql(woke, baseline ? 67 : 10, baseline ? 20 : 1, baseline ? 21 : 2);
        assertThat(sends).containsExactly(expected(fresh));
        assertThat(history(old)).allMatch(i -> i.status().equals("invalid"));
        assertThat(history(fresh))
                .hasSize(1)
                .allMatch(i -> i.status().equals("pending") && i.live());
    }

    @Test
    void twentyExpiredPrefixBindingsCannotKeepTheOtherOwnersLiveWaitingOutsideTheLimit()
            throws Exception {
        var expired = expiredPrefix();
        var valid = scenario(BOB, Mode.CITY);
        order(valid, 20);
        var delivery = delivery();
        var first = observe("expired-prefix-first", delivery::scheduled);
        first.evidence().put("business_facts", prefixFacts(expired, valid));
        assertSql(first, baseline ? 62 : 10, baseline ? 20 : 1, baseline ? 20 : 2);
        var second = observe("expired-prefix-second", delivery::scheduled);
        second.evidence().put("business_facts", prefixFacts(expired, valid));
        assertSql(second, baseline ? 62 : 2, baseline ? 20 : 0, baseline ? 20 : 0);
        var third = observe("expired-prefix-third", delivery::scheduled);
        third.evidence().put("business_facts", prefixFacts(expired, valid));
        assertSql(third, baseline ? 62 : 0, baseline ? 20 : null, baseline ? 20 : 0);
        assertThat(sends).hasSize(baseline ? 0 : 1);
        if (!baseline) assertThat(sends).containsExactly(expected(valid));
        assertThat(noticeCount(valid)).isEqualTo(baseline ? 0 : 1);
        for (var scenario : expired) assertThat(noticeCount(scenario)).isZero();
        assertThat(
                        application
                                .getBean(OperationsQueryService.class)
                                .backlog(ALICE, "notification_bindings", "", 100)
                                .items())
                .hasSize(baseline ? 20 : 0);
        assertThat(history(valid))
                .hasSize(1)
                .allMatch(i -> i.status().equals("pending") && i.live());
    }

    @Test
    void twentyLiveBindingsAndTheTwentyFirstKeepSeparateTransactionsAndBothOwnerRoutes()
            throws Exception {
        var scenarios = new ArrayList<Scenario>();
        for (int i = 0; i < 21; i++) {
            var scenario = scenario(i % 2 == 0 ? ALICE : BOB, Mode.CITY);
            order(scenario, i);
            scenarios.add(scenario);
        }
        var delivery = delivery();
        var first = observe("live-first-twenty", delivery::scheduled);
        first.evidence()
                .put(
                        "business_facts",
                        Map.of(
                                "projected_notices",
                                noticeTotal(),
                                "twenty_limit_held",
                                noticeTotal() == 20,
                                "twenty_first_deferred",
                                noticeCount(scenarios.get(20)) == 0,
                                "one_external_send",
                                sends.size() == 1));
        assertSql(first, 86, 20, 21);
        assertThat(first.sql().count("notice_insert")).isEqualTo(20);
        assertThat(noticeTotal()).isEqualTo(20);
        assertThat(noticeCount(scenarios.get(20))).isZero();
        assertThat(sends).containsExactly(expected(scenarios.getFirst()));
        var next = observe("live-remaining-one", delivery::scheduled);
        next.evidence()
                .put(
                        "business_facts",
                        Map.of(
                                "projected_notices",
                                noticeTotal(),
                                "remaining_one_projected",
                                noticeTotal() == 21,
                                "external_sends",
                                sends.size()));
        assertSql(next, 10, 1, 2);
        assertThat(next.sql().count("notice_insert")).isOne();
        var tail =
                observe(
                        "live-finite-drain",
                        () -> {
                            for (int i = 0; i < 19; i++) {
                                int before = sends.size();
                                delivery.scheduled();
                                assertThat(sends.size() - before).isOne();
                            }
                        });
        tail.evidence()
                .put(
                        "business_facts",
                        Map.of(
                                "all_owner_routes_match",
                                allRoutes(scenarios),
                                "external_sends",
                                sends.size(),
                                "all_notices_submitted_unverified",
                                scenarios.stream()
                                        .allMatch(
                                                s ->
                                                        notice(s)
                                                                .status()
                                                                .equals("submitted_unverified"))));
        assertSql(tail, 114, java.util.Collections.nCopies(19, 0), 19);
        assertThat(allRoutes(scenarios)).isTrue();
        assertThat(sends).hasSize(21);
        assertThat(delivery.list(ALICE)).hasSize(11);
        assertThat(delivery.list(BOB)).hasSize(10);
        var empty = observe("live-empty-repeat", delivery::scheduled);
        empty.evidence()
                .put(
                        "business_facts",
                        Map.of(
                                "external_sends",
                                sends.size(),
                                "no_duplicate_send",
                                sends.size() == 21));
        assertSql(empty, 2, 0, 0);
    }

    @Test
    void partialAnswersAndExpiryAfterDiscoveryStillUseTheLockedLiveProjection() throws Exception {
        var batch = scenario(ALICE, Mode.BATCH);
        var questions = history(batch);
        assertThat(questions).hasSize(2);
        approve(batch, questions.getFirst());
        assertThat(tasks.get(ALICE, batch.binding().conversationId(), batch.turn().id()))
                .isEqualTo(batch.turn());
        var current =
                history(batch).stream()
                        .filter(i -> i.status().equals("pending"))
                        .findFirst()
                        .orElseThrow();
        String currentToken = FeishuText.token(batch.binding(), batch.turn(), current);
        String answeredToken =
                FeishuText.token(batch.binding(), batch.turn(), questions.getFirst());
        var projected = observe("partial-live-projection", delivery()::scheduled);
        projected
                .evidence()
                .put(
                        "business_facts",
                        Map.of(
                                "one_live_pending_is_eligible",
                                sends.size() == 1,
                                "answered_token_not_rendered",
                                !notice(batch).payload().contains(answeredToken),
                                "current_token_rendered",
                                notice(batch).payload().contains(currentToken)));
        assertSql(projected, 10, 1, 2);
        assertThat(notice(batch).payload())
                .isEqualTo(render(batch))
                .contains(currentToken)
                .doesNotContain(answeredToken);
        assertThat(sends).containsExactly(expected(batch));
        resetData();
        var expiring = scenario(ALICE, Mode.CITY);
        var late =
                afterDiscovery(
                        "post-discovery-expiry", expiring, delivery(), () -> expireAll(expiring));
        late.evidence()
                .put(
                        "business_facts",
                        Map.of(
                                "discovery_saw_live_candidate",
                                true,
                                "locked_projection_rechecked_expiry",
                                noticeCount(expiring) == 0,
                                "no_external_send",
                                sends.isEmpty()));
        assertSql(late, 5, 1, 1);
        assertThat(noticeCount(expiring)).isZero();
        var question = history(expiring).getFirst();
        assertThat(question.live()).isFalse();
        assertThatThrownBy(
                        () ->
                                governance.answer(
                                        ALICE,
                                        expiring.binding().conversationId(),
                                        expiring.turn().id(),
                                        question.id(),
                                        UUID.randomUUID().toString(),
                                        new Answer(
                                                "input",
                                                question.revision(),
                                                null,
                                                null,
                                                Map.of("city", "上海"))))
                .hasMessage("stale_interaction");
    }

    @Test
    void muteRebindAndTurnRevisionChangesAfterScanningRemainIndependentLockedChecks()
            throws Exception {
        for (String change : List.of("mute", "rebind", "turn-revision")) {
            resetData();
            var original = scenario(ALICE, Mode.CITY);
            var delivery = delivery();
            Runnable mutation =
                    switch (change) {
                        case "mute" -> () -> inbound.receive(message(original.binding(), "/mute"));
                        case "rebind" -> () -> inbound.receive(message(original.binding(), "/new"));
                        default ->
                                () ->
                                        tasks.cancel(
                                                ALICE,
                                                original.binding().conversationId(),
                                                original.turn().id(),
                                                UUID.randomUUID().toString());
                    };
            String phase =
                    change.equals("turn-revision")
                            ? "post-discovery-turn-revision"
                            : "post-discovery-" + change;
            var changed = afterDiscovery(phase, original, delivery, mutation);
            if (change.equals("turn-revision")) {
                var currentTurn =
                        tasks.get(ALICE, original.binding().conversationId(), original.turn().id());
                changed.evidence()
                        .put(
                                "business_facts",
                                Map.of(
                                        "fresh_turn_revision_read",
                                        currentTurn.revision() > original.turn().revision(),
                                        "cancelled_notice_sent",
                                        sends.equals(
                                                List.of(
                                                        new Send(
                                                                original.binding().chatId(),
                                                                "任务已取消。"))),
                                        "waiting_text_not_sent",
                                        sends.stream().noneMatch(s -> s.text().contains("请输入城市"))));
                assertSql(changed, 9, 1, 2);
                assertThat(currentTurn.status()).isEqualTo("cancelled");
                var current =
                        new Scenario(
                                original.owner(), original.binding(), currentTurn, original.mode());
                assertThat(notice(current).payload()).isEqualTo("任务已取消。");
                assertThat(sends).containsExactly(new Send(original.binding().chatId(), "任务已取消。"));
                continue;
            }
            changed.evidence()
                    .put(
                            "business_facts",
                            Map.of(
                                    "locked_binding_change_seen",
                                    true,
                                    "old_turn_has_no_notice",
                                    noticeCount(original) == 0,
                                    "no_external_send",
                                    sends.isEmpty()));
            assertSql(changed, 3, 1, 1);
            assertThat(changed.sql().count("interaction_read")).isZero();
            assertThat(noticeCount(original)).isZero();
            assertThat(sends).isEmpty();
            if (change.equals("mute")) {
                inbound.receive(message(original.binding(), "/unmute"));
                var woke = observe("mute-reawaken", delivery::scheduled);
                woke.evidence()
                        .put(
                                "business_facts",
                                Map.of(
                                        "route_matches",
                                        sends.equals(List.of(expected(original))),
                                        "notice_created_once",
                                        noticeCount(original) == 1));
                assertSql(woke, 10, 1, 2);
                assertThat(sends).containsExactly(expected(original));
            } else {
                var rebound = binding(original);
                assertThat(rebound.revision()).isEqualTo(original.binding().revision() + 1);
                assertThat(rebound.activeTurn()).isNull();
                assertThat(rebound.conversationId())
                        .isNotEqualTo(original.binding().conversationId());
                inbound.receive(message(rebound, "batch"));
                var fresh = execute(ALICE, binding(original), Mode.BATCH);
                var woke = observe("rebind-new-waiting", delivery::scheduled);
                woke.evidence()
                        .put(
                                "business_facts",
                                Map.of(
                                        "new_binding_revision_matches",
                                        notice(fresh).bindingRevision() == rebound.revision(),
                                        "old_turn_has_no_notice",
                                        noticeCount(original) == 0,
                                        "new_turn_route_matches",
                                        sends.equals(List.of(expected(fresh)))));
                assertSql(woke, 10, 1, 2);
                assertThat(sends).containsExactly(expected(fresh));
                assertThat(notice(fresh).payload()).doesNotContain("请输入城市");
            }
        }
    }

    @Test
    void defaultPlannerUsesThePendingIndexForAnIsolatedSyntheticHistoryLoad() throws Exception {
        var source = scenario(ALICE, Mode.BATCH);
        for (var question : history(source)) approve(source, question);
        var completed = runExisting(source);
        assertThat(completed.turn().status()).isEqualTo("completed");
        var template = history(completed).getFirst();
        assertThat(template.status()).isEqualTo("answered");
        inbound.receive(message(completed.binding(), "/new"));
        assertThat(binding(completed).activeTurn()).isNull();
        // 独立计划负载：这些副本不是实际 SDK 调用，不作为业务/恢复验收数据，也不会被执行或恢复。
        assertThat(
                        raw.update(
                                "INSERT INTO business.interactions(id,turn_id,command_id,attempt_id,state_version,grant_revision,release_hash,kind,call_id,tool,action_hash,schema_json,question,revision,status,expires_at,response_hash,response_json) SELECT 'synthetic-plan-row-'||g,turn_id,command_id,attempt_id,state_version,grant_revision,release_hash,kind,'synthetic-plan-call-'||g,tool,action_hash,schema_json,question,revision,'answered',expires_at,response_hash,response_json FROM business.interactions CROSS JOIN generate_series(1,20000) g WHERE id=?",
                                template.id()))
                .isEqualTo(SYNTHETIC_ROWS);
        var expired = expiredPrefix();
        var valid = scenario(BOB, Mode.CITY);
        order(valid, 20);
        assertThat(
                        raw.queryForObject(
                                "SELECT count(*)::int FROM business.interactions WHERE id LIKE 'synthetic-plan-row-%'",
                                Integer.class))
                .isEqualTo(SYNTHETIC_ROWS);
        raw.execute("ANALYZE business.interactions");
        raw.execute("ANALYZE business.turns");
        raw.execute("ANALYZE channel.bindings");
        raw.execute("ANALYZE channel.notices");
        boolean indexPresent = indexPresent();
        assertThat(indexPresent).isEqualTo(!baseline);
        if (baseline) {
            explain(
                    "scale-before-production",
                    ProjectionDiscoveryRecorder.OLD_SQL,
                    true,
                    false,
                    false,
                    20);
            explain(
                    "scale-before-candidate-preview",
                    ProjectionDiscoveryRecorder.NEW_SQL,
                    false,
                    true,
                    false,
                    1);
        } else {
            var plan =
                    explain(
                            "scale-after-production",
                            ProjectionDiscoveryRecorder.NEW_SQL,
                            true,
                            false,
                            true,
                            1);
            var nodes = plan.path("Plan");
            var scans = new ArrayList<JsonNode>();
            collectNodes(nodes, scans);
            assertThat(
                            scans.stream()
                                    .filter(
                                            n ->
                                                    n.path("Relation Name")
                                                            .asText()
                                                            .equals("interactions")))
                    .noneMatch(n -> n.path("Node Type").asText().contains("Seq Scan"));
            assertThat(scans).anyMatch(n -> n.path("Index Name").asText().equals(INDEX_NAME));
            String definition =
                    raw.queryForObject(
                            "SELECT pg_get_indexdef('business.interaction_pending_turn_expiry'::regclass)",
                            String.class);
            assertThat(definition).contains("(turn_id, expires_at)", "status = 'pending'");
        }
        assertThat(sends).isEmpty();
        for (var scenario : expired) assertThat(noticeCount(scenario)).isZero();
        assertThat(noticeCount(valid)).isZero();
    }

    private JsonNode explain(
            String phase,
            String sql,
            boolean production,
            boolean preview,
            boolean indexPresent,
            int expectedRows)
            throws Exception {
        String encoded =
                raw.queryForObject("EXPLAIN (ANALYZE,BUFFERS,FORMAT JSON) " + sql, String.class);
        JsonNode plan = json.readTree(encoded).get(0);
        assertThat(plan.path("Plan").path("Actual Rows").asInt()).isEqualTo(expectedRows);
        var nodes = new ArrayList<JsonNode>();
        collectNodes(plan.path("Plan"), nodes);
        var safe = new ArrayList<Map<String, Object>>();
        for (var node : nodes) {
            var row = new LinkedHashMap<String, Object>();
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
                    "index_condition_contains_turn_id",
                    node.path("Index Cond").asText().contains("turn_id"));
            row.put(
                    "filter_contains_volatile_clock",
                    node.path("Filter").asText().contains("clock_timestamp"));
            safe.add(row);
        }
        scaleExperiments.add(
                Map.of(
                        "phase",
                        phase,
                        "production_sql",
                        production,
                        "preview_only",
                        preview,
                        "index_present",
                        indexPresent,
                        "template_sha256",
                        ProjectionDiscoveryRecorder.digest(sql),
                        "synthetic_answered_rows",
                        SYNTHETIC_ROWS,
                        "real_waiting_bindings",
                        21,
                        "actual_result_rows",
                        expectedRows,
                        "safe_plan_nodes",
                        safe,
                        "default_planner",
                        true));
        return plan;
    }

    private static void collectNodes(JsonNode node, List<JsonNode> result) {
        result.add(node);
        for (var child : node.path("Plans")) collectNodes(child, result);
    }

    private boolean indexPresent() {
        return Boolean.TRUE.equals(
                raw.queryForObject(
                        "SELECT to_regclass('business.interaction_pending_turn_expiry') IS NOT NULL",
                        Boolean.class));
    }

    private void assertExpiredOperations(String label, int expected) {
        var admissions = application.getBean(AdmissionService.class);
        var operations = application.getBean(OperationsQueryService.class);
        admissions.change(
                ALICE, UUID.randomUUID().toString(), true, admissions.admission(ALICE).revision());
        try {
            var drain = operations.drain(ALICE);
            var bindings = operations.backlog(ALICE, "notification_bindings", "", 100);
            var turns = operations.backlog(ALICE, "turns", "", 100);
            assertThat(drain.admission().paused()).isTrue();
            assertThat(drain.counts().get("waiting")).isEqualTo((long) expected);
            assertThat(drain.counts().get("pending_interactions")).isEqualTo((long) expected);
            assertThat(drain.counts().get("unprojected_notifications"))
                    .isEqualTo(baseline ? (long) expected : 0L);
            assertThat(bindings.items()).hasSize(baseline ? expected : 0);
            assertThat(turns.items())
                    .hasSize(expected)
                    .allMatch(
                            item ->
                                    item instanceof TurnBacklog t
                                            && t.status().equals("waiting")
                                            && t.pendingInteractions() == 1);
            assertThat(drain.drained()).isFalse();
            operationsObservations.add(
                    Map.of(
                            "label",
                            label,
                            "paused",
                            true,
                            "waiting",
                            drain.counts().get("waiting"),
                            "pending_interactions",
                            drain.counts().get("pending_interactions"),
                            "unprojected_notifications",
                            drain.counts().get("unprojected_notifications"),
                            "notification_bindings",
                            bindings.items().size(),
                            "turn_backlog",
                            turns.items().size(),
                            "drained",
                            drain.drained()));
        } finally {
            admissions.change(
                    ALICE,
                    UUID.randomUUID().toString(),
                    false,
                    admissions.admission(ALICE).revision());
        }
    }

    private List<Scenario> expiredPrefix() throws Exception {
        var result = new ArrayList<Scenario>();
        for (int i = 0; i < 20; i++) {
            var scenario = scenario(ALICE, Mode.CITY);
            expireAll(scenario);
            order(scenario, i);
            result.add(scenario);
        }
        return result;
    }

    private Scenario scenario(Owner owner, Mode mode) throws Exception {
        String conversation = tasks.createConversation(owner);
        String channel = UUID.randomUUID().toString();
        var identity =
                new Binding(
                        "unused",
                        CHANNEL_TENANT,
                        "open-" + channel,
                        "chat-" + channel,
                        owner.tenant(),
                        owner.subject(),
                        conversation,
                        1,
                        false,
                        null);
        inbound.receive(message(identity, "/bind " + inbound.link(owner, conversation)));
        inbound.receive(message(identity, mode.message));
        var bound =
                db.getBindingByChannelIdentityForUpdate(
                        CHANNEL_TENANT, identity.openId(), identity.chatId());
        return execute(owner, bound, mode);
    }

    private Scenario execute(Owner owner, Binding binding, Mode mode) throws Exception {
        var before = tasks.get(owner, binding.conversationId(), binding.activeTurn());
        var scenario = new Scenario(owner, binding, before, mode);
        return runExisting(scenario);
    }

    private Scenario runExisting(Scenario scenario) throws Exception {
        try (var worker =
                TaskWorkerFixture.create(
                        application,
                        scenario.mode() == Mode.COMPLETED
                                ? application.getBean(TaskAgentRuntime.class)
                                : new ScriptedRuntime(
                                        tasks,
                                        governance,
                                        application.getBean(RuntimeMapper.class),
                                        application.getBean(BusinessCapabilities.class)),
                        false,
                        1,
                        60000,
                        2000)) {
            var claim = tasks.claim("projection-discovery-worker", 60000);
            assertThat(claim).isNotNull();
            assertThat(claim.turn().id()).isEqualTo(scenario.turn().id());
            worker.execute(claim);
        }
        var current =
                tasks.get(
                        scenario.owner(),
                        scenario.binding().conversationId(),
                        scenario.turn().id());
        assertThat(current.status()).isIn("waiting", "completed");
        return new Scenario(scenario.owner(), scenario.binding(), current, scenario.mode());
    }

    private void approve(Scenario scenario, Interaction interaction) {
        governance.answer(
                scenario.owner(),
                scenario.binding().conversationId(),
                scenario.turn().id(),
                interaction.id(),
                UUID.randomUUID().toString(),
                new Answer(
                        "approval",
                        interaction.revision(),
                        "approve",
                        interaction.actionHash(),
                        null,
                        "已核对证据"));
    }

    private List<Interaction> history(Scenario scenario) {
        return governance.interactions(
                scenario.owner(), scenario.binding().conversationId(), scenario.turn().id());
    }

    private void expireAll(Scenario scenario) {
        var rows = history(scenario);
        assertThat(rows).isNotEmpty().allMatch(i -> i.status().equals("pending"));
        for (var interaction : rows)
            assertThat(
                            raw.update(
                                    "UPDATE business.interactions SET expires_at=clock_timestamp()-interval '1 second' WHERE id=?",
                                    interaction.id()))
                    .isOne();
        assertThat(history(scenario)).allMatch(i -> !i.live() && i.status().equals("pending"));
    }

    private void order(Scenario scenario, int order) {
        assertThat(
                        raw.update(
                                "UPDATE business.turns SET created_at=timestamptz '2000-01-01 00:00:00+00' + (? * interval '1 second') WHERE id=?",
                                order,
                                scenario.turn().id()))
                .isOne();
    }

    private Binding binding(Scenario scenario) {
        return db.getBindingByChannelIdentityForUpdate(
                CHANNEL_TENANT, scenario.binding().openId(), scenario.binding().chatId());
    }

    private String render(Scenario scenario) {
        return FeishuText.render(scenario.binding(), scenario.turn(), history(scenario));
    }

    private Send expected(Scenario scenario) {
        return new Send(scenario.binding().chatId(), render(scenario));
    }

    private boolean allRoutes(List<Scenario> scenarios) {
        return scenarios.stream()
                .allMatch(
                        s ->
                                sends.stream()
                                        .filter(send -> send.chat().equals(s.binding().chatId()))
                                        .toList()
                                        .equals(List.of(expected(s))));
    }

    private int noticeTotal() {
        return raw.queryForObject("SELECT count(*)::int FROM channel.notices", Integer.class);
    }

    private int noticeCount(Scenario scenario) {
        return raw.queryForObject(
                "SELECT count(*)::int FROM channel.notices WHERE turn_id=?",
                Integer.class,
                scenario.turn().id());
    }

    private Notice notice(Scenario scenario) {
        var notice =
                raw.queryForObject(
                        "SELECT id,turn_id,binding_id,binding_revision,revision,payload,status,reason FROM channel.notices WHERE turn_id=? AND revision=?",
                        (row, index) ->
                                new Notice(
                                        row.getString("id"),
                                        row.getString("turn_id"),
                                        row.getString("binding_id"),
                                        row.getLong("binding_revision"),
                                        row.getLong("revision"),
                                        row.getString("payload"),
                                        row.getString("status"),
                                        row.getString("reason")),
                        scenario.turn().id(),
                        scenario.turn().revision());
        assertThat(notice).isNotNull();
        assertThat(notice.bindingId()).isEqualTo(scenario.binding().id());
        assertThat(notice.bindingRevision()).isEqualTo(scenario.binding().revision());
        assertThat(notice.turnId()).isEqualTo(scenario.turn().id());
        assertThat(notice.revision()).isEqualTo(scenario.turn().revision());
        return notice;
    }

    private Map<String, Object> emptyFacts(List<Scenario> scenarios) {
        return Map.of(
                "waiting_turns",
                scenarios.size(),
                "all_expired_pending_preserved",
                scenarios.stream()
                        .allMatch(
                                s ->
                                        history(s).stream()
                                                .allMatch(
                                                        i ->
                                                                i.status().equals("pending")
                                                                        && !i.live())),
                "no_notices",
                noticeTotal() == 0,
                "no_external_send",
                sends.isEmpty());
    }

    private Map<String, Object> prefixFacts(List<Scenario> expired, Scenario valid) {
        return Map.of(
                "intentional_progress_difference",
                true,
                "expired_bindings",
                expired.size(),
                "all_expired_have_no_notice",
                expired.stream().allMatch(s -> noticeCount(s) == 0),
                "valid_notice_count",
                noticeCount(valid),
                "external_sends",
                sends.size(),
                "all_sent_routes_match_valid_owner",
                sends.stream().allMatch(send -> send.equals(expected(valid))));
    }

    private Observed afterDiscovery(
            String phase, Scenario scenario, FeishuDelivery delivery, Runnable mutation)
            throws Exception {
        try (var gate = recorder.pauseAfterDiscovery(scenario.binding().id());
                var executor = Executors.newSingleThreadExecutor()) {
            var pending = executor.submit(() -> observe(phase, delivery::scheduled));
            try {
                gate.awaitEntered();
                mutation.run();
            } finally {
                gate.release();
            }
            return pending.get(10, TimeUnit.SECONDS);
        }
    }

    private Observed observe(String phase, Runnable action) {
        counter.begin(phase);
        recorder.begin(phase);
        Throwable failure = null;
        try {
            action.run();
        } catch (Throwable caught) {
            failure = caught;
        }
        JdbcExecutionCounter.Measurement sql;
        ProjectionDiscoveryRecorder.Measurement discovery;
        try {
            discovery = recorder.finish();
        } finally {
            sql = counter.finish();
        }
        var evidence = new LinkedHashMap<String, Object>();
        evidence.put("phase", phase);
        evidence.put("completed_without_exception", failure == null);
        evidence.put("sql", sql);
        evidence.put("discovery", discovery);
        if (failure != null) evidence.put("failure_type", failure.getClass().getSimpleName());
        measurements.add(evidence);
        return new Observed(failure, sql, discovery, evidence);
    }

    private void assertSql(Observed observed, int expectedSql, Integer candidates, int commits) {
        assertSql(
                observed,
                expectedSql,
                candidates == null ? List.of() : List.of(candidates),
                commits);
    }

    private void assertSql(
            Observed observed, int expectedSql, List<Integer> candidates, int commits) {
        assertThat(observed.failure()).isNull();
        assertThat(observed.sql().statements()).as(observed.sql().phase()).isEqualTo(expectedSql);
        assertThat(observed.sql().commits()).isEqualTo(commits);
        assertThat(observed.sql().rollbacks()).isZero();
        assertThat(observed.discovery().phase()).isEqualTo(observed.sql().phase());
        assertThat(
                        observed.discovery().queries().stream()
                                .map(ProjectionDiscoveryRecorder.QueryObservation::candidates)
                                .toList())
                .isEqualTo(candidates);
        var hashes =
                observed.sql().executions().stream()
                        .filter(e -> e.family().equals("notice_projection_scan"))
                        .map(JdbcExecutionCounter.SqlExecution::templateSha256)
                        .toList();
        assertThat(
                        observed.discovery().queries().stream()
                                .map(ProjectionDiscoveryRecorder.QueryObservation::templateSha256)
                                .toList())
                .isEqualTo(hashes);
        assertThat(hashes)
                .allMatch(
                        hash ->
                                hash.equals(
                                        baseline
                                                ? ProjectionDiscoveryRecorder.OLD_HASH
                                                : ProjectionDiscoveryRecorder.NEW_HASH));
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

    private void closeResources() {
        try {
            if (application != null) application.close();
        } finally {
            if (postgres.isRunning()) postgres.stop();
        }
    }
}
