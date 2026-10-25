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
import com.financeclaw.channel.FeishuText;
import com.financeclaw.domain.Feishu.Binding;
import com.financeclaw.domain.Feishu.Notice;
import com.financeclaw.domain.Governance.Answer;
import com.financeclaw.domain.Governance.Interaction;
import com.financeclaw.domain.Task.Claim;
import com.financeclaw.domain.Task.Owner;
import com.financeclaw.domain.Task.Turn;
import com.financeclaw.fixture.GovernanceFixture;
import com.financeclaw.fixture.TaskFixture;
import com.financeclaw.infrastructure.agentscope.capability.BusinessCapabilities;
import com.financeclaw.infrastructure.agentscope.context.WorkingContextSession;
import com.financeclaw.infrastructure.agentscope.execution.TaskAgentRuntime;
import com.financeclaw.infrastructure.channel.FeishuMapper;
import com.financeclaw.infrastructure.state.RuntimeMapper;
import com.financeclaw.m1.TaskWorkerFixture;
import com.financeclaw.support.PayloadJson;
import io.agentscope.core.message.Msg;
import io.agentscope.core.message.TextBlock;
import io.agentscope.core.message.ToolResultBlock;
import io.agentscope.core.message.ToolUseBlock;
import io.agentscope.core.model.ChatResponse;
import io.agentscope.core.model.GenerateOptions;
import io.agentscope.core.model.Model;
import io.agentscope.core.model.ToolSchema;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.ArrayList;
import java.util.Comparator;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.UUID;
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
import org.springframework.dao.DataIntegrityViolationException;
import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.transaction.support.TransactionSynchronizationManager;
import org.springframework.transaction.support.TransactionTemplate;
import org.testcontainers.containers.PostgreSQLContainer;
import reactor.core.publisher.Flux;

/**
 * Waiting 通知专用 pending 查询的真实 PG 前后基线。
 *
 * <p>正常绑定、受理、原生 SDK 审批暂停、回答、恢复、投影和领取均使用生产服务。场景模型只决定下一批工具调用， 末端 send 替身只验证投递路由；不声称验证远端模型或原生飞书 HTTP
 * SDK。交互和回答正文不由测试 SQL 伪造。
 *
 * <p>观察窗口只包括生产 project 调用；fixture、原始全量历史 oracle 和后续发送/回答/恢复均在窗口外。 MyBatis 观察器统计已经映射的行及 response
 * 字符串逻辑长度，逐项匹配真实 JDBC execute 摘要； SQL 次数与事务应保持，不能把过滤收益解释成网络字节、JDBC getter、TOAST、内存或吞吐改善。
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
@Execution(ExecutionMode.SAME_THREAD)
class WaitingNoticeQueryIT {
    private static final Owner ALICE = new Owner("waiting-query-tenant-a", "alice");
    private static final Owner BOB = new Owner("waiting-query-tenant-b", "bob");
    private static final String CHANNEL_TENANT = "waiting-query-channel";
    private static final String LARGE_REASON = "证据🙂A".repeat(399);
    private static final Comparator<Interaction> INTERACTION_ORDER =
            Comparator.comparingLong(Interaction::revision).thenComparing(Interaction::id);
    private final boolean baseline = Boolean.getBoolean("financeclaw.test.waiting-notice-baseline");
    private final PostgreSQLContainer<?> postgres =
            new PostgreSQLContainer<>("postgres:16.10-alpine");
    private final ObjectMapper json = new ObjectMapper();
    private final List<Map<String, Object>> measurements = new ArrayList<>();
    private final List<Send> sends = new ArrayList<>();
    private ConfigurableApplicationContext application;
    private JdbcTemplate raw;
    private JdbcExecutionCounter counter;
    private WaitingInteractionRecorder recorder;
    private TaskFixture tasks;
    private GovernanceFixture governance;
    private FeishuMapper db;
    private FeishuService inbound;
    private FeishuSettings settings;
    private AgentScopeFeishu sender;

    record Send(String chat, String text) {}

    record Scenario(Owner owner, Binding binding, int batches) {}

    record Oracle(Turn turn, List<Interaction> history, List<Interaction> pending, String text) {}

    record Observed(
            Throwable failure,
            JdbcExecutionCounter.Measurement sql,
            WaitingInteractionRecorder.Measurement mapped,
            Map<String, Object> evidence) {}

    static final class RawSource {
        private DataSource source;
    }

    @TestConfiguration(proxyBeanMethods = false)
    @Import({TaskFixture.class, GovernanceFixture.class})
    static class CountingConfiguration {
        @Bean
        static JdbcExecutionCounter waitingNoticeCounter() {
            return new JdbcExecutionCounter();
        }

        @Bean
        static WaitingInteractionRecorder waitingInteractionRecorder() {
            return new WaitingInteractionRecorder();
        }

        @Bean
        static RawSource waitingNoticeRawSource() {
            return new RawSource();
        }

        @Bean
        static BeanPostProcessor waitingNoticeSource(JdbcExecutionCounter counter, RawSource raw) {
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
            throw new IllegalStateException("Use the dedicated waiting-notice-baseline flag");
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
                                    "--financeclaw.auth.token=synthetic-waiting-query-token",
                                    "--financeclaw.feishu.enabled=true",
                                    "--financeclaw.feishu.app-id=waiting-query",
                                    "--financeclaw.feishu.app-secret=synthetic-waiting-query-secret",
                                    "--financeclaw.feishu.verification-token=synthetic-waiting-query-token",
                                    "--financeclaw.feishu.encrypt-key=synthetic-waiting-query-key",
                                    "--financeclaw.feishu.tenant-key=" + CHANNEL_TENANT,
                                    "--financeclaw.business.ziwei.enabled=false");
            raw = new JdbcTemplate(application.getBean(RawSource.class).source);
            counter = application.getBean(JdbcExecutionCounter.class);
            recorder = application.getBean(WaitingInteractionRecorder.class);
            tasks = application.getBean(TaskFixture.class);
            governance = application.getBean(GovernanceFixture.class);
            db = application.getBean(FeishuMapper.class);
            inbound = application.getBean(FeishuService.class);
            settings = application.getBean(FeishuSettings.class);
            sender = mock(AgentScopeFeishu.class);
            var configuration = application.getBean(SqlSessionFactory.class).getConfiguration();
            String history =
                    configuration
                            .getMappedStatement(WaitingInteractionRecorder.HISTORY_ID)
                            .getBoundSql("fixture-turn")
                            .getSql();
            assertThat(WaitingInteractionRecorder.digest(history))
                    .isEqualTo(WaitingInteractionRecorder.HISTORY_HASH);
            // before 不依赖尚未存在的新 Java 方法；after 只按 statement 字符串核对完整字段/live/order。
            if (!baseline) {
                String pending =
                        configuration
                                .getMappedStatement(WaitingInteractionRecorder.PENDING_ID)
                                .getBoundSql("fixture-turn")
                                .getSql();
                assertThat(WaitingInteractionRecorder.digest(pending))
                        .isEqualTo(WaitingInteractionRecorder.PENDING_HASH);
            }
            assertThat(configuration.getInterceptors()).contains(recorder);
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
            Path output = Path.of("target/p3a15/waiting-notice-sql.json");
            Files.createDirectories(output.getParent());
            var report = new LinkedHashMap<String, Object>();
            report.put("schema", "waiting-notice-sql/1");
            report.put("phase", "P3a.15");
            report.put("mode", baseline ? "before" : "after");
            report.put("scope", "real-pg-native-approval-resume-project-terminal-send-mock");
            report.put("background_pollers_enabled", false);
            report.put(
                    "sql_boundary", "production-project-only-fixture-oracle-send-resume-excluded");
            report.put("result_metric", "mybatis-already-mapped-response-utf8-logical-length");
            report.put("no_jdbc_getter_wire_toast_memory_or_throughput_claim", true);
            report.put("measurements", List.copyOf(measurements));
            json.writerWithDefaultPrettyPrinter().writeValue(output.toFile(), report);
        } finally {
            closeResources();
        }
    }

    @Test
    void multipleNativeApprovalRoundsKeepOnlyCurrentPendingRowsWithoutBreakingResume()
            throws Exception {
        assertThat(LARGE_REASON.length()).isEqualTo(1995);
        var scenario = scenario(ALICE, 3, 2);
        var current = pending(history(scenario));
        assertThat(current).hasSize(2);
        Turn beforePartial = turn(scenario);
        String answeredToken =
                FeishuText.token(scenario.binding(), beforePartial, current.getFirst());
        int attempts = attempts(scenario);
        approve(scenario, current.getFirst(), true);
        // 一批只答一半不能提前入队；后续 resume 仍须读取原通用全量历史中的已回答问题。
        assertThat(turn(scenario)).isEqualTo(beforePartial);
        assertThat(commandStatus(scenario)).isEqualTo("done");
        assertThat(attempts(scenario)).isEqualTo(attempts);
        var oracle = oracle(scenario);
        assertThat(oracle.history()).hasSize(6);
        assertThat(oracle.pending()).hasSize(1);
        assertThat(responseCount(oracle.history())).isEqualTo(5);
        assertThat(responseBytes(oracle.history())).isGreaterThan(20_000);
        var projected =
                observe(
                        "history-partial-current-project",
                        () -> delivery().project(scenario.binding().id()));
        var facts = projectFacts(scenario, oracle, true);
        facts.put("partial_answer_keeps_waiting_command_done_without_new_attempt", true);
        projected.evidence().put("business_facts", facts);
        assertProjection(projected, oracle, false);
        assertThat(readNotice(scenario, oracle.turn()).payload()).isEqualTo(oracle.text());
        delivery().scheduled();
        assertThat(sends).containsExactly(new Send(scenario.binding().chatId(), oracle.text()));
        assertThatThrownBy(
                        () ->
                                inbound.receive(
                                        message(scenario.binding(), "/approve " + answeredToken)))
                .hasMessage("stale_approval");
        var last = oracle.pending().getFirst();
        String lastToken = FeishuText.token(scenario.binding(), oracle.turn(), last);
        inbound.receive(message(scenario.binding(), "/approve " + lastToken));
        assertThat(turn(scenario).status()).isEqualTo("resuming");
        run(scenario);
        assertThat(turn(scenario).status()).isEqualTo("completed");
        assertThat(turn(scenario).result()).isEqualTo("all approval rounds completed");
        assertThat(reports(scenario)).isEqualTo(6);
        assertThat(history(scenario)).hasSize(6).allMatch(i -> i.status().equals("answered"));
        assertThat(attempts(scenario)).isEqualTo(4);
        var policy =
                raw.queryForMap(
                        "SELECT model_calls,tool_calls FROM business.turn_policies WHERE turn_id=?",
                        turn(scenario).id());
        var definition = tasks.release(turn(scenario).releaseHash());
        assertThat(definition.maxModelCalls()).isEqualTo(12);
        assertThat(definition.maxToolCalls()).isEqualTo(32);
        assertThat(((Number) policy.get("model_calls")).intValue())
                .isPositive()
                .isLessThanOrEqualTo(definition.maxModelCalls());
        assertThat(((Number) policy.get("tool_calls")).intValue())
                .isPositive()
                .isLessThanOrEqualTo(definition.maxToolCalls());
        facts.put("all_answers_resume_to_completed_with_six_reports", true);
        facts.put("original_budgets_preserved", true);
    }

    @Test
    void expiredPendingRowsRemainVisibleToTheClockCheckButNeverCreateExpiredApprovalText()
            throws Exception {
        for (boolean allExpired : List.of(false, true)) {
            var scenario = scenario(ALICE, 2, 1);
            var current = pending(history(scenario));
            assertThat(current).hasSize(2);
            int count = allExpired ? 2 : 1;
            for (int i = 0; i < count; i++) {
                // 只调整既有真实问题的时间；没有手工制造交互、状态、回答或运行时回执。
                assertThat(
                                raw.update(
                                        "UPDATE business.interactions SET expires_at=clock_timestamp()-interval '1 second' WHERE id=?",
                                        current.get(i).id()))
                        .isOne();
            }
            var oracle = oracle(scenario);
            assertThat(oracle.pending()).hasSize(2);
            assertThat(oracle.pending().stream().filter(Interaction::live).count())
                    .isEqualTo(allExpired ? 0 : 1);
            String phase = allExpired ? "expiry-all-project" : "expiry-mixed-project";
            var projected = observe(phase, () -> delivery().project(scenario.binding().id()));
            projected.evidence().put("business_facts", projectFacts(scenario, oracle, !allExpired));
            assertProjection(projected, oracle, false);
            var expired =
                    oracle.pending().stream().filter(i -> !i.live()).findFirst().orElseThrow();
            String expiredToken = FeishuText.token(scenario.binding(), oracle.turn(), expired);
            assertThatThrownBy(
                            () ->
                                    inbound.receive(
                                            message(
                                                    scenario.binding(),
                                                    "/approve " + expiredToken)))
                    .hasMessage("stale_approval");
            assertThatThrownBy(() -> approve(scenario, expired, false))
                    .hasMessage("stale_interaction");
            if (allExpired) {
                assertThat(oracle.text()).isEmpty();
                assertThat(noticeCount(scenario)).isZero();
            } else {
                var live =
                        oracle.pending().stream()
                                .filter(Interaction::live)
                                .findFirst()
                                .orElseThrow();
                String liveToken = FeishuText.token(scenario.binding(), oracle.turn(), live);
                assertThat(readNotice(scenario, oracle.turn()).payload())
                        .isEqualTo(oracle.text())
                        .contains(liveToken)
                        .doesNotContain(expiredToken);
                delivery().scheduled();
                assertThat(sends)
                        .containsExactly(new Send(scenario.binding().chatId(), oracle.text()));
            }
        }
    }

    @Test
    void separateOwnersKeepTheirRoutesAndCancellationPreservesInvalidHistoryOutsideWaiting()
            throws Exception {
        var alice = scenario(ALICE, 2, 1);
        var bob = scenario(BOB, 2, 1);
        var aliceOracle = oracle(alice);
        var bobOracle = oracle(bob);
        var first = observe("owner-alice-project", () -> delivery().project(alice.binding().id()));
        first.evidence().put("business_facts", projectFacts(alice, aliceOracle, true));
        assertProjection(first, aliceOracle, false);
        var second = observe("owner-bob-project", () -> delivery().project(bob.binding().id()));
        second.evidence().put("business_facts", projectFacts(bob, bobOracle, true));
        assertProjection(second, bobOracle, false);
        assertThat(readNotice(alice, aliceOracle.turn()).payload()).isEqualTo(aliceOracle.text());
        assertThat(readNotice(bob, bobOracle.turn()).payload()).isEqualTo(bobOracle.text());
        assertThat(aliceOracle.text()).isNotEqualTo(bobOracle.text());
        delivery().scheduled();
        delivery().scheduled();
        assertThat(sends)
                .containsExactlyInAnyOrder(
                        new Send(alice.binding().chatId(), aliceOracle.text()),
                        new Send(bob.binding().chatId(), bobOracle.text()));
        var bobBefore = history(bob);
        inbound.receive(message(alice.binding(), "/cancel"));
        assertThat(turn(alice).status()).isEqualTo("cancelled");
        var cancelledHistory = history(alice);
        assertThat(cancelledHistory).hasSize(4);
        assertThat(cancelledHistory.stream().filter(i -> i.status().equals("answered")).count())
                .isEqualTo(2);
        assertThat(cancelledHistory.stream().filter(i -> i.status().equals("invalid")).count())
                .isEqualTo(2);
        assertThat(
                        application
                                .getBean(ExecutionContextGateway.class)
                                .interactions(turn(alice).id()))
                .isEqualTo(cancelledHistory);
        var cancelled =
                observe(
                        "owner-alice-cancelled-project",
                        () -> delivery().project(alice.binding().id()));
        cancelled
                .evidence()
                .put(
                        "business_facts",
                        Map.of(
                                "cancelled",
                                true,
                                "invalid_history_retained",
                                true,
                                "other_owner_history_unchanged",
                                history(bob).equals(bobBefore),
                                "cancelled_notice_matches",
                                readNotice(alice, turn(alice)).payload().equals("任务已取消。")));
        assertNoInteractionQuery(cancelled, 3);
        delivery().scheduled();
        assertThat(sends).hasSize(3).contains(new Send(alice.binding().chatId(), "任务已取消。"));
        assertThat(history(bob)).isEqualTo(bobBefore);
        var bobPending = pending(bobBefore).getFirst();
        String token = FeishuText.token(bob.binding(), turn(bob), bobPending);
        inbound.receive(message(bob.binding(), "/approve " + token));
        assertThat(turn(bob).status()).isEqualTo("waiting");
        assertThat(pending(history(bob))).hasSize(1);
        assertThat(history(alice)).isEqualTo(cancelledHistory);
    }

    @Test
    void failedNoticeInsertRollsBackAndRetryUsesTheSameHistoryAndDurableNotice() throws Exception {
        var scenario = scenario(ALICE, 2, 1);
        var oracle = oracle(scenario);
        var bindingBefore = db.getBindingForUpdate(scenario.binding().id());
        var eventsBefore =
                tasks.events(
                        scenario.owner(), oracle.turn().conversationId(), oracle.turn().id(), 0);
        raw.execute(
                "CREATE FUNCTION channel.waiting_notice_projection_failure() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN RAISE EXCEPTION 'waiting_notice_projection_failure' USING ERRCODE='23514'; END $$");
        raw.execute(
                "CREATE TRIGGER waiting_notice_projection_failure AFTER INSERT ON channel.notices FOR EACH ROW EXECUTE FUNCTION channel.waiting_notice_projection_failure()");
        Observed failed;
        try {
            failed =
                    observe(
                            "projection-insert-rollback",
                            () -> delivery().project(scenario.binding().id()));
        } finally {
            raw.execute("DROP TRIGGER waiting_notice_projection_failure ON channel.notices");
            raw.execute("DROP FUNCTION channel.waiting_notice_projection_failure()");
        }
        failed.evidence()
                .put(
                        "business_facts",
                        Map.of(
                                "notice_absent",
                                noticeCount(scenario) == 0,
                                "turn_unchanged",
                                turn(scenario).equals(oracle.turn()),
                                "history_unchanged",
                                history(scenario).equals(oracle.history()),
                                "binding_unchanged",
                                db.getBindingForUpdate(scenario.binding().id())
                                        .equals(bindingBefore),
                                "events_unchanged",
                                tasks.events(
                                                scenario.owner(),
                                                oracle.turn().conversationId(),
                                                oracle.turn().id(),
                                                0)
                                        .equals(eventsBefore)));
        assertThat(failed.failure())
                .isInstanceOf(DataIntegrityViolationException.class)
                .hasMessageContaining("waiting_notice_projection_failure");
        assertProjection(failed, oracle, true);
        assertThat(noticeCount(scenario)).isZero();
        assertThat(turn(scenario)).isEqualTo(oracle.turn());
        assertThat(history(scenario)).isEqualTo(oracle.history());
        assertThat(db.getBindingForUpdate(scenario.binding().id())).isEqualTo(bindingBefore);
        assertThat(
                        tasks.events(
                                scenario.owner(),
                                oracle.turn().conversationId(),
                                oracle.turn().id(),
                                0))
                .isEqualTo(eventsBefore);
        var retry = observe("projection-retry", () -> delivery().project(scenario.binding().id()));
        retry.evidence().put("business_facts", projectFacts(scenario, oracle, true));
        assertProjection(retry, oracle, false);
        var notice = readNotice(scenario, oracle.turn());
        var repeated =
                observe("projection-repeat", () -> delivery().project(scenario.binding().id()));
        repeated.evidence().put("business_facts", projectFacts(scenario, oracle, true));
        assertProjection(repeated, oracle, false);
        assertThat(readNotice(scenario, oracle.turn())).isEqualTo(notice);
        assertThat(noticeCount(scenario)).isOne();
        delivery().scheduled();
        assertThat(sends).containsExactly(new Send(scenario.binding().chatId(), oracle.text()));
    }

    /** 每一轮均由原生 SDK 真正 ASKING、封存 waiting，再由生产 AnswerInteractionService 重新入队。 */
    private Scenario scenario(Owner owner, int batches, int completedBatches) throws Exception {
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
        inbound.receive(message(identity, "approval rounds"));
        var bound =
                db.getBindingByChannelIdentityForUpdate(
                        CHANNEL_TENANT, identity.openId(), identity.chatId());
        var scenario = new Scenario(owner, bound, batches);
        run(scenario);
        for (int round = 0; round < completedBatches; round++) {
            var current = pending(history(scenario));
            assertThat(current).hasSize(2);
            int count = attempts(scenario);
            approve(scenario, current.getFirst(), true);
            assertThat(turn(scenario).status()).isEqualTo("waiting");
            assertThat(commandStatus(scenario)).isEqualTo("done");
            assertThat(attempts(scenario)).isEqualTo(count);
            approve(scenario, current.getLast(), true);
            assertThat(turn(scenario).status()).isEqualTo("resuming");
            run(scenario);
        }
        assertThat(turn(scenario).status())
                .as("normal native approval fixture")
                .isEqualTo("waiting");
        assertThat(commandStatus(scenario)).isEqualTo("done");
        assertThat(pending(history(scenario))).hasSize(2);
        return scenario;
    }

    private void run(Scenario scenario) throws Exception {
        try (var worker =
                TaskWorkerFixture.create(
                        application,
                        new ApprovalRoundsRuntime(scenario.batches()),
                        false,
                        1,
                        60000,
                        2000)) {
            var claim = tasks.claim("waiting-notice-query-worker", 60000);
            assertThat(claim).isNotNull();
            assertThat(claim.turn().id()).isEqualTo(scenario.binding().activeTurn());
            worker.execute(claim);
        }
    }

    /** 只决定三轮固定工具调用；治理、权限询问、持久状态、结果回执和恢复均沿用真实 TaskAgentRuntime。 */
    private final class ApprovalRoundsRuntime extends TaskAgentRuntime {
        private final int batches;

        private ApprovalRoundsRuntime(int batches) {
            super(
                    tasks.policy(),
                    tasks.leases(),
                    tasks.releases(),
                    tasks.credentials(),
                    tasks.context(),
                    governance.artifacts(),
                    governance.reports(),
                    application.getBean(RuntimeMapper.class),
                    application.getBean(BusinessCapabilities.class),
                    15000);
            this.batches = batches;
        }

        @Override
        protected Model model(Claim claim) {
            return new Model() {
                @Override
                public String getModelName() {
                    return "waiting-notice-approval-rounds";
                }

                @Override
                public Flux<ChatResponse> stream(
                        List<Msg> messages, List<ToolSchema> tools, GenerateOptions options) {
                    long resolved =
                            messages.stream()
                                    .filter(message -> !WorkingContextSession.isHistorical(message))
                                    .flatMap(
                                            message ->
                                                    message
                                                            .getContentBlocks(ToolResultBlock.class)
                                                            .stream())
                                    .filter(result -> !result.isSuspended())
                                    .map(ToolResultBlock::getId)
                                    .distinct()
                                    .count();
                    if (resolved == batches * 2L) {
                        return Flux.just(
                                ChatResponse.builder()
                                        .id("approval-rounds-final")
                                        .content(
                                                List.of(
                                                        TextBlock.builder()
                                                                .text(
                                                                        "all approval rounds completed")
                                                                .build()))
                                        .build());
                    }
                    if (resolved % 2 != 0 || resolved > batches * 2L)
                        throw new IllegalStateException(
                                "Approval batch was resumed before all answers");
                    int round = (int) (resolved / 2) + 1;
                    var calls = new ArrayList<ToolUseBlock>();
                    for (int index = 1; index <= 2; index++) {
                        var input =
                                Map.<String, Object>of(
                                        "title", "waiting-round-" + round + "-report-" + index);
                        calls.add(
                                ToolUseBlock.builder()
                                        .id("waiting-round-" + round + "-call-" + index)
                                        .name("create_report")
                                        .input(input)
                                        .content(PayloadJson.canonical(input))
                                        .build());
                    }
                    return Flux.just(
                            ChatResponse.builder()
                                    .id("approval-round-" + round)
                                    .content(new ArrayList<>(calls))
                                    .build());
                }
            };
        }
    }

    private void approve(Scenario scenario, Interaction interaction, boolean largeReason) {
        var answer =
                new Answer(
                        "approval",
                        interaction.revision(),
                        "approve",
                        interaction.actionHash(),
                        null,
                        largeReason ? LARGE_REASON : null);
        assertThat(PayloadJson.canonical(answer).getBytes(StandardCharsets.UTF_8).length)
                .isLessThanOrEqualTo(16384);
        governance.answer(
                scenario.owner(),
                scenario.binding().conversationId(),
                scenario.binding().activeTurn(),
                interaction.id(),
                "answer-" + UUID.randomUUID(),
                answer);
    }

    private Oracle oracle(Scenario scenario) {
        var turn = turn(scenario);
        var history = history(scenario);
        assertThat(history).isSortedAccordingTo(INTERACTION_ORDER);
        var pending = pending(history);
        String rendered = FeishuText.render(scenario.binding(), turn, history);
        assertThat(FeishuText.render(scenario.binding(), turn, pending)).isEqualTo(rendered);
        assertThat(rendered.length()).isLessThan(4000);
        // 通用执行上下文入口必须保留 answered/invalid；不能把通知过滤误用到原生恢复或回答批次判断。
        assertThat(application.getBean(ExecutionContextGateway.class).interactions(turn.id()))
                .isEqualTo(history);
        return new Oracle(turn, history, pending, rendered);
    }

    private Map<String, Object> projectFacts(
            Scenario scenario, Oracle oracle, boolean expectedNotice) {
        assertThat(history(scenario)).isEqualTo(oracle.history());
        assertThat(noticeCount(scenario)).isEqualTo(expectedNotice ? 1 : 0);
        if (expectedNotice)
            assertThat(readNotice(scenario, oracle.turn()).payload()).isEqualTo(oracle.text());
        var facts = new LinkedHashMap<String, Object>();
        facts.put("history_rows", oracle.history().size());
        facts.put("pending_rows", oracle.pending().size());
        facts.put("history_response_nonnull_count", responseCount(oracle.history()));
        facts.put("history_response_utf8_logical_bytes", responseBytes(oracle.history()));
        facts.put(
                "pending_live_count", oracle.pending().stream().filter(Interaction::live).count());
        facts.put(
                "pending_expired_count", oracle.pending().stream().filter(i -> !i.live()).count());
        facts.put("history_retained", history(scenario).equals(oracle.history()));
        facts.put(
                "notice_presence_matches_expected",
                noticeCount(scenario) == (expectedNotice ? 1 : 0));
        facts.put(
                "notice_matches_oracle",
                !expectedNotice
                        || readNotice(scenario, oracle.turn()).payload().equals(oracle.text()));
        return facts;
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
        WaitingInteractionRecorder.Measurement mapped;
        try {
            mapped = recorder.finish();
        } finally {
            sql = counter.finish();
        }
        var evidence = new LinkedHashMap<String, Object>();
        evidence.put("phase", phase);
        evidence.put("completed_without_exception", failure == null);
        evidence.put("sql", sql);
        evidence.put("mapped", mapped);
        if (failure != null) evidence.put("failure_type", failure.getClass().getSimpleName());
        measurements.add(evidence);
        return new Observed(failure, sql, mapped, evidence);
    }

    private void assertProjection(Observed observed, Oracle oracle, boolean rollback) {
        if (!rollback) assertThat(observed.failure()).isNull();
        assertThat(observed.sql().statements())
                .as(observed.sql().phase())
                .isEqualTo(oracle.text().isEmpty() ? 3 : 4);
        assertThat(observed.sql().commits()).isEqualTo(rollback ? 0 : 1);
        assertThat(observed.sql().rollbacks()).isEqualTo(rollback ? 1 : 0);
        assertThat(observed.mapped().phase()).isEqualTo(observed.sql().phase());
        var hashes =
                observed.sql().executions().stream()
                        .filter(e -> e.family().equals("interaction_read"))
                        .map(JdbcExecutionCounter.SqlExecution::templateSha256)
                        .toList();
        assertThat(
                        observed.mapped().queries().stream()
                                .map(WaitingInteractionRecorder.QueryObservation::templateSha256)
                                .toList())
                .isEqualTo(hashes);
        assertThat(observed.mapped().queries()).hasSize(1);
        var query = observed.mapped().queries().getFirst();
        var expected = baseline ? oracle.history() : oracle.pending();
        assertThat(query.templateSha256())
                .isEqualTo(
                        baseline
                                ? WaitingInteractionRecorder.HISTORY_HASH
                                : WaitingInteractionRecorder.PENDING_HASH);
        assertThat(query.rows()).isEqualTo(expected.size());
        var statuses = new LinkedHashMap<String, Long>();
        expected.forEach(i -> statuses.merge(i.status(), 1L, Long::sum));
        assertThat(query.statusCounts()).isEqualTo(statuses);
        assertThat(query.pendingLiveCount())
                .isEqualTo(oracle.pending().stream().filter(Interaction::live).count());
        assertThat(query.pendingExpiredCount())
                .isEqualTo(oracle.pending().stream().filter(i -> !i.live()).count());
        assertThat(query.responseNonNullCount()).isEqualTo(responseCount(expected));
        assertThat(query.responseUtf8LogicalBytes()).isEqualTo(responseBytes(expected));
    }

    private void assertNoInteractionQuery(Observed observed, int sql) {
        assertThat(observed.failure()).isNull();
        assertThat(observed.sql().statements()).isEqualTo(sql);
        assertThat(observed.sql().commits()).isOne();
        assertThat(observed.sql().rollbacks()).isZero();
        assertThat(observed.mapped().phase()).isEqualTo(observed.sql().phase());
        assertThat(observed.sql().count("interaction_read")).isZero();
        assertThat(observed.mapped().queries()).isEmpty();
    }

    private static List<Interaction> pending(List<Interaction> history) {
        return history.stream()
                .filter(i -> i.status().equals("pending"))
                .sorted(INTERACTION_ORDER)
                .toList();
    }

    private static long responseCount(List<Interaction> rows) {
        return rows.stream().filter(i -> i.response() != null).count();
    }

    private static long responseBytes(List<Interaction> rows) {
        return rows.stream()
                .filter(i -> i.response() != null)
                .mapToLong(i -> i.response().getBytes(StandardCharsets.UTF_8).length)
                .sum();
    }

    private Turn turn(Scenario scenario) {
        return tasks.get(
                scenario.owner(),
                scenario.binding().conversationId(),
                scenario.binding().activeTurn());
    }

    private List<Interaction> history(Scenario scenario) {
        return governance.interactions(
                scenario.owner(),
                scenario.binding().conversationId(),
                scenario.binding().activeTurn());
    }

    private int attempts(Scenario scenario) {
        return raw.queryForObject(
                "SELECT count(*)::int FROM business.attempts a JOIN business.commands c ON c.id=a.command_id WHERE c.turn_id=?",
                Integer.class,
                scenario.binding().activeTurn());
    }

    private int reports(Scenario scenario) {
        return raw.queryForObject(
                "SELECT count(*)::int FROM business.reports WHERE turn_id=?",
                Integer.class,
                scenario.binding().activeTurn());
    }

    private String commandStatus(Scenario scenario) {
        return raw.queryForObject(
                "SELECT status FROM business.commands WHERE turn_id=?",
                String.class,
                scenario.binding().activeTurn());
    }

    private int noticeCount(Scenario scenario) {
        return raw.queryForObject(
                "SELECT count(*)::int FROM channel.notices WHERE turn_id=?",
                Integer.class,
                scenario.binding().activeTurn());
    }

    private Notice readNotice(Scenario scenario, Turn turn) {
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
                        turn.id(),
                        turn.revision());
        assertThat(notice).isNotNull();
        assertThat(notice.bindingId()).isEqualTo(scenario.binding().id());
        assertThat(notice.bindingRevision()).isEqualTo(scenario.binding().revision());
        assertThat(notice.turnId()).isEqualTo(turn.id());
        assertThat(notice.revision()).isEqualTo(turn.revision());
        return notice;
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
