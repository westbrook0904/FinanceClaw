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
import com.financeclaw.infrastructure.agentscope.execution.TaskAgentRuntime;
import com.financeclaw.infrastructure.channel.FeishuMapper;
import com.financeclaw.m1.TaskWorkerFixture;
import com.financeclaw.support.PayloadDigest;
import com.financeclaw.support.PayloadJson;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Locale;
import java.util.Map;
import java.util.UUID;
import java.util.concurrent.CopyOnWriteArrayList;
import java.util.concurrent.atomic.AtomicBoolean;
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
 * 候选定位查询的真实 PG 结果宽度基线。业务 SQL 次数不变；只移除 candidate.payload 的重复映射。 锁内 notice_payload 和后续独立 Turn.result
 * 仍含正文，不能把候选零正文解释成整个领取链路零正文。
 *
 * <p>ResultSet 观察器只对生产实际调用的 getString 返回值按 UTF-8 计长，不额外读行或正文。 该指标不代表 PG 网络字节、TOAST
 * 页、内存或吞吐。正常绑定、受理、执行、投影和失效均走生产服务， 只替换最后 AgentScopeFeishu.send 边界；不声称验证飞书原生 HTTP SDK。
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
@Execution(ExecutionMode.SAME_THREAD)
class NoticeCandidateQueryIT {
    private static final Owner ALICE = new Owner("candidate-tenant-a", "alice");
    private static final Owner BOB = new Owner("candidate-tenant-b", "bob");
    private static final String CHANNEL_TENANT = "candidate-channel";
    private static final String LARGE_INPUT = "证据🙂A".repeat(600);
    private static final List<String> OLD_COLUMNS =
            List.of(
                    "id",
                    "turn_id",
                    "binding_id",
                    "binding_revision",
                    "revision",
                    "payload",
                    "status",
                    "reason");
    private static final List<String> NEW_COLUMNS = List.of("id", "binding_id");
    private static final List<String> LOCKED_COLUMNS =
            List.of(
                    "binding_id",
                    "tenant_key",
                    "open_id",
                    "chat_id",
                    "tenant",
                    "subject",
                    "conversation_id",
                    "binding_revision",
                    "muted",
                    "active_turn",
                    "notice_id",
                    "notice_turn_id",
                    "notice_binding_id",
                    "notice_binding_revision",
                    "notice_revision",
                    "notice_payload",
                    "notice_status",
                    "notice_reason");
    private final boolean baseline =
            Boolean.getBoolean("financeclaw.test.notice-candidate-baseline");
    private final PostgreSQLContainer<?> postgres =
            new PostgreSQLContainer<>("postgres:16.10-alpine");
    private final ObjectMapper json = new ObjectMapper();
    private final List<Map<String, Object>> measurements = new CopyOnWriteArrayList<>();
    private final List<Send> sends = new CopyOnWriteArrayList<>();
    private final AtomicBoolean failSend = new AtomicBoolean();
    private ConfigurableApplicationContext application;
    private JdbcTemplate raw;
    private JdbcExecutionCounter counter;
    private NoticeResultSetRecorder recorder;
    private TaskFixture tasks;
    private FeishuMapper db;
    private FeishuService inbound;
    private FeishuSettings settings;
    private AgentScopeFeishu sender;

    record Send(String chat, String text) {}

    record Scenario(Owner owner, Binding binding, Turn turn, Notice notice) {}

    record Observed(
            Throwable failure,
            JdbcExecutionCounter.Measurement sql,
            NoticeResultSetRecorder.Measurement results,
            Map<String, Object> evidence) {}

    static final class RawSource {
        private DataSource source;
    }

    @TestConfiguration(proxyBeanMethods = false)
    @Import({TaskFixture.class, GovernanceFixture.class})
    static class CountingConfiguration {
        @Bean
        static JdbcExecutionCounter noticeCandidateCounter() {
            return new JdbcExecutionCounter();
        }

        @Bean
        static NoticeResultSetRecorder noticeCandidateResults() {
            return new NoticeResultSetRecorder();
        }

        @Bean
        static RawSource noticeCandidateRawSource() {
            return new RawSource();
        }

        @Bean
        static BeanPostProcessor noticeCandidateSource(
                JdbcExecutionCounter counter, NoticeResultSetRecorder recorder, RawSource raw) {
            return new BeanPostProcessor() {
                @Override
                public Object postProcessAfterInitialization(Object bean, String name) {
                    if (name.equals("dataSource") && bean instanceof DataSource source) {
                        raw.source = source;
                        // 事务管理器和 MyBatis 仍共享同一个最外层 DataSource 资源身份。
                        return counter.wrap(recorder.wrap(source));
                    }
                    return bean;
                }
            };
        }
    }

    @BeforeAll
    void start() {
        if (Boolean.getBoolean("financeclaw.test.query-baseline"))
            throw new IllegalStateException(
                    "Use notice-candidate-baseline for this result-width baseline");
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
                                    "--financeclaw.auth.token=synthetic-candidate-token",
                                    "--financeclaw.feishu.enabled=true",
                                    "--financeclaw.feishu.app-id=candidate-query",
                                    "--financeclaw.feishu.app-secret=synthetic-candidate-secret",
                                    "--financeclaw.feishu.verification-token=synthetic-candidate-token",
                                    "--financeclaw.feishu.encrypt-key=synthetic-candidate-key",
                                    "--financeclaw.feishu.tenant-key=" + CHANNEL_TENANT,
                                    "--financeclaw.business.ziwei.enabled=false");
            raw = new JdbcTemplate(application.getBean(RawSource.class).source);
            counter = application.getBean(JdbcExecutionCounter.class);
            recorder = application.getBean(NoticeResultSetRecorder.class);
            tasks = application.getBean(TaskFixture.class);
            db = application.getBean(FeishuMapper.class);
            inbound = application.getBean(FeishuService.class);
            settings = application.getBean(FeishuSettings.class);
            sender = mock(AgentScopeFeishu.class);
            var configuration = application.getBean(SqlSessionFactory.class).getConfiguration();
            String pending =
                    configuration
                            .getMappedStatement(
                                    FeishuMapper.class.getName() + ".getNextPendingNotice")
                            .getBoundSql(null)
                            .getSql();
            assertThat(digest(pending))
                    .isEqualTo(
                            baseline
                                    ? NoticeResultSetRecorder.OLD_CANDIDATE
                                    : NoticeResultSetRecorder.NEW_CANDIDATE);
            String locked =
                    configuration
                            .getMappedStatement(
                                    FeishuMapper.class.getName() + ".getBindingAndNoticeForUpdate")
                            .getBoundSql(
                                    Map.of(
                                            "bindingId",
                                            "fixture-binding",
                                            "noticeId",
                                            "fixture-notice"))
                            .getSql();
            assertThat(digest(locked)).isEqualTo(NoticeResultSetRecorder.LOCKED_NOTICE);
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
                            assertThat(
                                            TransactionSynchronizationManager
                                                    .isActualTransactionActive())
                                    .isFalse();
                            sends.add(new Send(call.getArgument(0), call.getArgument(1)));
                            if (failSend.get())
                                throw new IllegalStateException("synthetic_candidate_send_failure");
                            return null;
                        })
                .when(sender)
                .send(anyString(), anyString());
    }

    @AfterAll
    void writeAndClose() throws Exception {
        try {
            Path output = Path.of("target/p3a14/notice-candidate-sql.json");
            Files.createDirectories(output.getParent());
            var report = new LinkedHashMap<String, Object>();
            report.put("schema", "notice-candidate-sql/1");
            report.put("mode", baseline ? "before" : "after");
            report.put("scope", "real-pg-production-delivery-terminal-send-mock");
            report.put("background_pollers_enabled", false);
            report.put(
                    "measurement_boundary",
                    "production-scheduled-only-fixture-and-readback-excluded");
            report.put(
                    "result_metric",
                    "production-getString-returned-payload-utf8-reencoding-deduplicated-by-resultset-row-column");
            report.put("excluded_body_columns", List.of("owned_turn.result"));
            report.put("no_wire_toast_memory_or_throughput_claim", true);
            report.put("measurements", List.copyOf(measurements));
            json.writerWithDefaultPrettyPrinter().writeValue(output.toFile(), report);
        } finally {
            closeResources();
        }
    }

    @Test
    void largeUtf8NoticeKeepsTheLockedBodyAndOwnerRouteWithoutDuplicateCandidatePayload()
            throws Exception {
        assertThat(LARGE_INPUT.length()).isEqualTo(3000);
        assertThat(LARGE_INPUT.getBytes(StandardCharsets.UTF_8)).hasSize(6600);
        var scenario = scenario(ALICE, "");
        long body = bytes(scenario.notice().payload());
        var delivery = delivery();
        var first = observe("large-notice-send", delivery::scheduled);
        first.evidence()
                .put(
                        "business_facts",
                        Map.of(
                                "send_attempts",
                                sends.size(),
                                "route_payload_match",
                                sends.equals(List.of(expectedSend(scenario))),
                                "notice_submitted_unverified",
                                notice(scenario).status().equals("submitted_unverified"),
                                "result_untruncated",
                                scenario.notice().payload().equals(scenario.turn().result())));
        assertSql(first, 6, 1);
        assertResults(first, List.of(body), List.of(body));
        assertThat(sends).containsExactly(expectedSend(scenario));
        assertThat(notice(scenario).payload()).isEqualTo(scenario.turn().result());
        assertThat(notice(scenario).status()).isEqualTo("submitted_unverified");
        var restarted = delivery();
        var empty = observe("large-notice-restarted-empty", restarted::scheduled);
        empty.evidence()
                .put(
                        "business_facts",
                        Map.of(
                                "send_attempts",
                                sends.size(),
                                "no_duplicate_send",
                                sends.size() == 1,
                                "terminal_notice_kept",
                                notice(scenario).status().equals("submitted_unverified")));
        assertSql(empty, 2, 0);
        assertResults(empty, List.of(0L), List.of());
        assertThat(sends).containsExactly(expectedSend(scenario));
    }

    @Test
    void twentyStaleCandidatesRemainBoundedAndTheOtherOwnerProgressesOnTheNextTick()
            throws Exception {
        var stale = new ArrayList<Scenario>();
        for (int i = 0; i < 20; i++) {
            var scenario = scenario(ALICE, "/stale-" + i);
            orderNotice(scenario, i);
            inbound.receive(message(scenario.binding(), "/new"));
            var changed = db.getBindingForUpdate(scenario.binding().id());
            assertThat(changed.revision()).isEqualTo(scenario.binding().revision() + 1);
            assertThat(changed.activeTurn()).isNull();
            assertThat(changed.conversationId()).isNotEqualTo(scenario.binding().conversationId());
            stale.add(scenario);
        }
        var valid = scenario(BOB, "/valid-bob");
        orderNotice(valid, 20);
        var delivery = delivery();
        var first = observe("twenty-stale-prefix", delivery::scheduled);
        int suppressed =
                integer("SELECT count(*)::int FROM channel.notices WHERE status='suppressed'");
        first.evidence()
                .put(
                        "business_facts",
                        Map.of(
                                "stale_suppressed",
                                suppressed,
                                "valid_pending",
                                notice(valid).status().equals("pending"),
                                "send_attempts",
                                sends.size(),
                                "owner_routes_not_crossed",
                                sends.isEmpty()));
        assertSql(first, 61, 20);
        var staleBodies = stale.stream().map(s -> bytes(s.notice().payload())).toList();
        assertResults(first, staleBodies, staleBodies);
        assertThat(suppressed).isEqualTo(20);
        assertThat(first.sql().count("notice_pending_read")).isEqualTo(20);
        assertThat(first.sql().count("notice_claim_locked_read")).isEqualTo(20);
        assertThat(first.sql().count("notice_claim_update")).isZero();
        assertThat(notice(valid).status()).isEqualTo("pending");
        assertThat(sends).isEmpty();
        var next = observe("twenty-stale-next-owner", delivery::scheduled);
        next.evidence()
                .put(
                        "business_facts",
                        Map.of(
                                "send_attempts",
                                sends.size(),
                                "valid_owner_route_match",
                                sends.equals(List.of(expectedSend(valid))),
                                "all_stale_retained",
                                integer(
                                                "SELECT count(*)::int FROM channel.notices WHERE status='suppressed'")
                                        == 20));
        assertSql(next, 6, 1);
        assertResults(
                next,
                List.of(bytes(valid.notice().payload())),
                List.of(bytes(valid.notice().payload())));
        assertThat(sends).containsExactly(expectedSend(valid));
        assertThat(delivery.list(ALICE)).hasSize(20);
        assertThat(delivery.list(BOB)).hasSize(1);
        for (var scenario : stale) {
            var current = notice(scenario);
            assertThat(current.status()).isEqualTo("suppressed");
            assertThat(current.reason()).isEqualTo("binding_changed");
            assertThat(current.payload()).isEqualTo(scenario.notice().payload());
        }
        assertThat(notice(valid).status()).isEqualTo("submitted_unverified");
        var empty = observe("twenty-stale-empty-repeat", delivery::scheduled);
        empty.evidence()
                .put(
                        "business_facts",
                        Map.of(
                                "send_attempts",
                                sends.size(),
                                "no_duplicate_send",
                                sends.size() == 1));
        assertSql(empty, 2, 0);
        assertResults(empty, List.of(0L), List.of());
        assertThat(sends).containsExactly(expectedSend(valid));
    }

    @Test
    void failedSendStaysUnknownAcrossCoordinatorRestartAndEmptyCandidateMetadataIsStillObserved()
            throws Exception {
        var scenario = scenario(BOB, "/unknown");
        failSend.set(true);
        var delivery = delivery();
        var failedSend = observe("unknown-send-attempt", delivery::scheduled);
        failedSend
                .evidence()
                .put(
                        "business_facts",
                        Map.of(
                                "send_attempts",
                                sends.size(),
                                "unknown_kept",
                                notice(scenario).status().equals("unknown"),
                                "attempt_payload_match",
                                sends.equals(List.of(expectedSend(scenario)))));
        assertSql(failedSend, 6, 1);
        assertResults(
                failedSend,
                List.of(bytes(scenario.notice().payload())),
                List.of(bytes(scenario.notice().payload())));
        assertThat(sends).containsExactly(expectedSend(scenario));
        var unknown = notice(scenario);
        assertThat(unknown.status()).isEqualTo("unknown");
        assertThat(unknown.reason()).isEqualTo("native_send_unconfirmed");
        failSend.set(false);
        var restarted = delivery();
        var empty = observe("unknown-restarted-empty", restarted::scheduled);
        empty.evidence()
                .put(
                        "business_facts",
                        Map.of(
                                "send_attempts",
                                sends.size(),
                                "unknown_kept",
                                notice(scenario).status().equals("unknown"),
                                "no_duplicate_send",
                                sends.size() == 1));
        assertSql(empty, 2, 0);
        assertResults(empty, List.of(0L), List.of());
        assertThat(empty.sql().count("notice_claim_locked_read")).isZero();
        assertThat(notice(scenario)).isEqualTo(unknown);
        assertThat(sends).containsExactly(expectedSend(scenario));
    }

    private Scenario scenario(Owner owner, String suffix) throws Exception {
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
        inbound.receive(message(identity, LARGE_INPUT + suffix));
        var bound =
                db.getBindingByChannelIdentityForUpdate(
                        CHANNEL_TENANT, identity.openId(), identity.chatId());
        try (var worker =
                TaskWorkerFixture.create(
                        application,
                        application.getBean(TaskAgentRuntime.class),
                        false,
                        1,
                        60000,
                        2000)) {
            var claim = tasks.claim("candidate-query-worker", 60000);
            assertThat(claim).isNotNull();
            assertThat(claim.turn().id()).isEqualTo(bound.activeTurn());
            worker.execute(claim);
        }
        var turn = tasks.get(owner, conversation, bound.activeTurn());
        assertThat(turn.status())
                .as("normal offline fixture: %s", turn.reason())
                .isEqualTo("completed");
        delivery().project(bound.id());
        var notice = readNotice(turn.id(), turn.revision());
        // Oracle 来自实际服务写入的结果，而非输入常量；同时证明通知没有触发 4000 UTF16 截断。
        assertThat(turn.result()).startsWith("已收到：").contains(LARGE_INPUT);
        assertThat(notice.payload()).isEqualTo(turn.result());
        assertThat(notice.payload().length()).isLessThan(4000);
        assertThat(bytes(notice.payload())).isGreaterThan(6000).isLessThan(8192);
        assertThat(notice.bindingId()).isEqualTo(bound.id());
        assertThat(notice.bindingRevision())
                .isEqualTo(bound.revision())
                .isNotEqualTo(notice.revision());
        assertThat(notice.revision()).isEqualTo(turn.revision());
        assertThat(notice.status()).isEqualTo("pending");
        return new Scenario(owner, bound, turn, notice);
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
        NoticeResultSetRecorder.Measurement results;
        try {
            results = recorder.finish();
        } finally {
            sql = counter.finish();
        }
        var evidence = new LinkedHashMap<String, Object>();
        evidence.put("phase", phase);
        evidence.put("completed_without_exception", failure == null);
        evidence.put("sql", sql);
        evidence.put("results", results);
        if (failure != null) evidence.put("failure_type", failure.getClass().getSimpleName());
        measurements.add(evidence);
        return new Observed(failure, sql, results, evidence);
    }

    private void assertResults(Observed observed, List<Long> candidates, List<Long> locked) {
        assertThat(observed.results().phase()).isEqualTo(observed.sql().phase());
        var recorded = observed.results().results();
        var candidateResults = recorded.stream().filter(r -> r.kind().equals("candidate")).toList();
        var lockedResults =
                recorded.stream().filter(r -> r.kind().equals("locked_notice")).toList();
        assertThat(recorded).hasSize(candidates.size() + locked.size());
        assertThat(candidateResults).hasSize(candidates.size());
        assertThat(lockedResults).hasSize(locked.size());
        // 同一窗口 SQL 与 ResultSet 序列逐项对应，不能靠漏观测把正文误报为零。
        var queryHashes =
                observed.sql().executions().stream()
                        .filter(
                                e ->
                                        List.of("notice_pending_read", "notice_claim_locked_read")
                                                .contains(e.family()))
                        .map(JdbcExecutionCounter.SqlExecution::templateSha256)
                        .toList();
        assertThat(
                        recorded.stream()
                                .map(NoticeResultSetRecorder.ResultObservation::templateSha256)
                                .toList())
                .isEqualTo(queryHashes);
        for (int i = 0; i < candidates.size(); i++) {
            var result = candidateResults.get(i);
            long body = candidates.get(i);
            assertThat(result.resultSetObserved()).isTrue();
            assertThat(result.columns()).isEqualTo(baseline ? OLD_COLUMNS : NEW_COLUMNS);
            assertThat(result.rows()).isEqualTo(body > 0 ? 1 : 0);
            assertThat(result.payloadCellsRead()).isEqualTo(baseline && body > 0 ? 1 : 0);
            assertThat(result.payloadUtf8Bytes()).isEqualTo(baseline ? body : 0);
        }
        for (int i = 0; i < locked.size(); i++) {
            var result = lockedResults.get(i);
            assertThat(result.resultSetObserved()).isTrue();
            assertThat(result.columns()).isEqualTo(LOCKED_COLUMNS);
            assertThat(result.rows()).isOne();
            assertThat(result.payloadCellsRead()).isOne();
            assertThat(result.payloadUtf8Bytes()).isEqualTo(locked.get(i));
        }
    }

    private static void assertSql(Observed observed, int statements, int commits) {
        assertThat(observed.failure()).isNull();
        assertThat(observed.sql().statements()).as(observed.sql().phase()).isEqualTo(statements);
        assertThat(observed.sql().commits()).isEqualTo(commits);
        assertThat(observed.sql().rollbacks()).isZero();
        assertThat(observed.sql().count("other")).isZero();
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

    private Notice notice(Scenario scenario) {
        return readNotice(scenario.turn().id(), scenario.turn().revision());
    }

    private Notice readNotice(String turn, long revision) {
        return raw.queryForObject(
                "SELECT id,turn_id,binding_id,binding_revision,revision,payload,status,reason FROM channel.notices WHERE turn_id=? AND revision=?",
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
                turn,
                revision);
    }

    private void orderNotice(Scenario scenario, int index) {
        raw.update(
                "UPDATE channel.notices SET created_at=timestamptz '2000-01-01 00:00:00+00' + ? * interval '1 second' WHERE id=?",
                index,
                scenario.notice().id());
    }

    private int integer(String query) {
        return raw.queryForObject(query, Integer.class);
    }

    private static Send expectedSend(Scenario scenario) {
        return new Send(scenario.binding().chatId(), scenario.notice().payload());
    }

    private static long bytes(String value) {
        return value.getBytes(StandardCharsets.UTF_8).length;
    }

    private static String digest(String sql) {
        return PayloadDigest.sha256(sql.replaceAll("\\s+", " ").trim().toLowerCase(Locale.ROOT));
    }

    private void closeResources() {
        try {
            if (application != null) application.close();
        } finally {
            postgres.stop();
        }
    }
}
