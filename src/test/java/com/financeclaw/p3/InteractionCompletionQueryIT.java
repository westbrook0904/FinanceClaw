package com.financeclaw.p3;

import static org.assertj.core.api.Assertions.*;

import com.fasterxml.jackson.databind.ObjectMapper;
import com.financeclaw.app.execution.port.ExecutionContextGateway;
import com.financeclaw.app.task.WorkSignals;
import com.financeclaw.bootstrap.FinanceClawApplication;
import com.financeclaw.domain.ExecutionScope;
import com.financeclaw.domain.Governance.Answer;
import com.financeclaw.domain.Governance.AnswerReceipt;
import com.financeclaw.domain.Governance.Interaction;
import com.financeclaw.domain.Task.Claim;
import com.financeclaw.domain.Task.Event;
import com.financeclaw.domain.Task.Owner;
import com.financeclaw.domain.Task.Turn;
import com.financeclaw.fixture.GovernanceFixture;
import com.financeclaw.fixture.TaskFixture;
import com.financeclaw.infrastructure.agentscope.capability.BusinessCapabilities;
import com.financeclaw.infrastructure.agentscope.context.WorkingContextSession;
import com.financeclaw.infrastructure.agentscope.execution.TaskAgentRuntime;
import com.financeclaw.infrastructure.governance.GovernanceMapper;
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
import java.sql.Connection;
import java.time.Duration;
import java.util.ArrayList;
import java.util.Comparator;
import java.util.HashMap;
import java.util.HashSet;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.Set;
import java.util.UUID;
import java.util.concurrent.CopyOnWriteArrayList;
import java.util.concurrent.CountDownLatch;
import java.util.concurrent.Executors;
import java.util.concurrent.TimeUnit;
import java.util.function.Supplier;
import javax.sql.DataSource;
import org.apache.ibatis.session.SqlSessionFactory;
import org.junit.jupiter.api.*;
import org.junit.jupiter.api.parallel.Execution;
import org.junit.jupiter.api.parallel.ExecutionMode;
import org.mybatis.spring.SqlSessionTemplate;
import org.springframework.beans.factory.config.BeanPostProcessor;
import org.springframework.boot.WebApplicationType;
import org.springframework.boot.builder.SpringApplicationBuilder;
import org.springframework.boot.test.context.TestConfiguration;
import org.springframework.context.ConfigurableApplicationContext;
import org.springframework.context.annotation.Bean;
import org.springframework.context.annotation.Import;
import org.springframework.dao.DataIntegrityViolationException;
import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.transaction.support.TransactionTemplate;
import org.testcontainers.containers.PostgreSQLContainer;
import reactor.core.publisher.Flux;

/**
 * 回答批次完成查询的真实 PG 前后基线。正常受理、原生 SDK 暂停/审批/恢复及业务回执均走生产服务， 仅场景模型决定下一批固定工具调用；无
 * HTTP/远端模型验收声明。Worker、渠道和记忆背景任务均关闭。
 *
 * <p>窗口只包含一次生产 answer 调用，或隔离的 mapper 谓词读取；fixture、oracle 和锁观察不纳入计数。 返回集合观察与 JDBC execute
 * 的精确模板逐条对应。只度量完成查询的 response 映射后逻辑长度， 锁定单个交互、SDK 状态和通用历史仍可能返回正文，不声称全链路零正文、网络/TOAST/内存/吞吐改善。
 *
 * <p>mapper 矩阵直接读取真实生命周期记录：invalid 来自正常取消，过期只调整 expires_at； 不把 invalid 伪装成可恢复的
 * waiting，也不制造空批次或删约束。空集合只使用不存在的键组合读取。
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
@Execution(ExecutionMode.SAME_THREAD)
class InteractionCompletionQueryIT {
    private static final Owner OWNER = new Owner("completion-query-tenant", "owner");
    private static final Owner OTHER = new Owner("completion-query-other", "other");
    private static final String LARGE_REASON = "证据🙂A".repeat(399);
    private static final Comparator<Interaction> ORDER =
            Comparator.comparingLong(Interaction::revision).thenComparing(Interaction::id);
    private final boolean baseline =
            Boolean.getBoolean("financeclaw.test.interaction-completion-baseline");
    private final PostgreSQLContainer<?> postgres =
            new PostgreSQLContainer<>("postgres:16.10-alpine");
    private final ObjectMapper json = new ObjectMapper();
    private final List<Map<String, Object>> measurements = new CopyOnWriteArrayList<>();
    private ConfigurableApplicationContext application;
    private DataSource original;
    private JdbcTemplate raw;
    private JdbcExecutionCounter counter;
    private InteractionCompletionRecorder recorder;
    private TaskFixture tasks;
    private GovernanceFixture governance;
    private WorkSignals signals;
    private TransactionTemplate tx;
    private SqlSessionTemplate sessions;
    private String answerUpdateHash;
    private String resumeUpdateHash;

    record Scenario(Owner owner, String conversation, String turn, int batches) {}

    record Snapshot(
            Turn turn,
            List<Interaction> history,
            List<Event> events,
            String commandStatus,
            String resumeId,
            int attempts,
            int reports,
            long commandSignal) {}

    record Oracle(List<Interaction> history, String attempt) {
        boolean hasUnanswered() {
            // 保留原过滤 current attempt 后 allMatch 的语义，包含空集合的真值；不加入 live 判断。
            return !history.stream()
                    .filter(i -> i.attemptId().equals(attempt))
                    .allMatch(i -> i.status().equals("answered"));
        }

        long currentRows() {
            return history.stream().filter(i -> i.attemptId().equals(attempt)).count();
        }
    }

    record Observed<T>(
            T value,
            Throwable failure,
            JdbcExecutionCounter.Measurement sql,
            InteractionCompletionRecorder.Measurement mapped,
            Map<String, Object> evidence) {}

    record LockWaiter(int pid, Set<Integer> blockers) {}

    static final class RawSource {
        private DataSource source;
    }

    @TestConfiguration(proxyBeanMethods = false)
    @Import({TaskFixture.class, GovernanceFixture.class})
    static class CountingConfiguration {
        @Bean
        static JdbcExecutionCounter completionCounter() {
            return new JdbcExecutionCounter();
        }

        @Bean
        static InteractionCompletionRecorder completionRecorder() {
            return new InteractionCompletionRecorder();
        }

        @Bean
        static RawSource completionRawSource() {
            return new RawSource();
        }

        @Bean
        static BeanPostProcessor completionSource(JdbcExecutionCounter counter, RawSource raw) {
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
            throw new IllegalStateException(
                    "Use the dedicated interaction-completion-baseline flag");
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
                                    "--financeclaw.auth.token=synthetic-completion-query-token",
                                    "--financeclaw.business.ziwei.enabled=false");
            original = application.getBean(RawSource.class).source;
            raw = new JdbcTemplate(original);
            counter = application.getBean(JdbcExecutionCounter.class);
            recorder = application.getBean(InteractionCompletionRecorder.class);
            tasks = application.getBean(TaskFixture.class);
            governance = application.getBean(GovernanceFixture.class);
            signals = application.getBean(WorkSignals.class);
            tx = application.getBean(TransactionTemplate.class);
            sessions = application.getBean(SqlSessionTemplate.class);
            var configuration = application.getBean(SqlSessionFactory.class).getConfiguration();
            assertThat(configuration.getInterceptors()).contains(recorder);
            assertThat(InteractionCompletionRecorder.HISTORY_HASH)
                    .isEqualTo("bdbb2b369192952e890f290f91fc16a1ba45820c8ba5bd9ad468d47e860fed0f");
            assertThat(InteractionCompletionRecorder.EXISTS_HASH)
                    .isEqualTo("180c40232356ea8b14be70ef845617d8bdf84f50ea448f69b0932e83cfe065c4");
            String selected =
                    baseline
                            ? InteractionCompletionRecorder.HISTORY_ID
                            : InteractionCompletionRecorder.EXISTS_ID;
            Object parameters =
                    baseline
                            ? "fixture-turn"
                            : Map.of("turn", "fixture-turn", "attempt", "fixture-attempt");
            assertThat(
                            InteractionCompletionRecorder.digest(
                                    configuration
                                            .getMappedStatement(selected)
                                            .getBoundSql(parameters)
                                            .getSql()))
                    .isEqualTo(
                            baseline
                                    ? InteractionCompletionRecorder.HISTORY_HASH
                                    : InteractionCompletionRecorder.EXISTS_HASH);
            answerUpdateHash =
                    InteractionCompletionRecorder.digest(
                            configuration
                                    .getMappedStatement(
                                            GovernanceMapper.class.getName()
                                                    + ".updatePendingInteractionAnswer")
                                    .getBoundSql(
                                            Map.of(
                                                    "id", "fixture", "hash", "fixture", "json",
                                                    "{}"))
                                    .getSql());
            resumeUpdateHash =
                    InteractionCompletionRecorder.digest(
                            configuration
                                    .getMappedStatement(
                                            GovernanceMapper.class.getName()
                                                    + ".updateDoneCommandForResume")
                                    .getBoundSql(
                                            Map.of("command", "fixture", "interaction", "fixture"))
                                    .getSql());
        } catch (RuntimeException | Error failure) {
            closeResources();
            throw failure;
        }
    }

    @BeforeEach
    void reset() {
        raw.execute(
                "TRUNCATE agent_runtime.states,business.conversations,business.admission,memory.jobs,memory.owners,memory.operations,memory.audit CASCADE");
    }

    @AfterAll
    void writeAndClose() throws Exception {
        try {
            Path output = Path.of("target/p3a16/interaction-completion-sql.json");
            Files.createDirectories(output.getParent());
            var report = new LinkedHashMap<String, Object>();
            report.put("schema", "interaction-completion-sql/1");
            report.put("phase", "P3a.16");
            report.put("mode", baseline ? "before" : "after");
            report.put(
                    "scope",
                    "real-pg-native-approval-resume-production-answer-and-isolated-mapper");
            report.put("background_pollers_enabled", false);
            report.put(
                    "sql_boundary",
                    "answer-or-single-predicate-transaction-fixture-oracle-lock-observation-excluded");
            report.put(
                    "result_metric",
                    "mybatis-already-mapped-completion-query-response-utf8-logical-length");
            report.put("no_jdbc_getter_wire_toast_memory_or_throughput_claim", true);
            report.put("measurements", List.copyOf(measurements));
            json.writerWithDefaultPrettyPrinter().writeValue(output.toFile(), report);
        } finally {
            closeResources();
        }
    }

    @Test
    void currentBatchFirstAndLastAnswersIgnoreLargeAnsweredHistoryFromOlderAttempts()
            throws Exception {
        var scenario = scenario(OWNER, 3, 2);
        var current = pending(history(scenario));
        assertThat(current).hasSize(2);
        var before = snapshot(scenario);
        String attempt = current.getFirst().attemptId();
        var firstAnswer = approval(current.getFirst(), LARGE_REASON);
        var first =
                observe(
                        "current-batch-first-answer",
                        attempt,
                        () -> answer(scenario, current.getFirst(), firstAnswer));
        var partial = snapshot(scenario);
        var firstOracle = new Oracle(partial.history(), attempt);
        attachOracle(first, firstOracle);
        first.evidence()
                .put(
                        "business_facts",
                        Map.of(
                                "partial_batch_stays_waiting",
                                partial.turn().status().equals("waiting"),
                                "command_stays_done",
                                partial.commandStatus().equals("done"),
                                "no_new_attempt",
                                partial.attempts() == before.attempts(),
                                "no_resume_signal",
                                partial.commandSignal() == before.commandSignal()));
        assertSingleCompletion(first, firstOracle, true, false);
        assertThat(first.value().idempotentReplay()).isFalse();
        assertThat(firstOracle.hasUnanswered()).isTrue();
        assertThat(firstOracle.currentRows()).isEqualTo(2);
        assertThat(firstOracle.history()).hasSize(6);
        assertThat(responseBytes(firstOracle.history())).isGreaterThan(20_000);
        assertNoAdvance(before, partial);
        assertThat(partial.history())
                .isEqualTo(answeredHistory(before.history(), current.getFirst(), firstAnswer));
        var lastAnswer = approval(current.getLast(), LARGE_REASON);
        var last =
                observe(
                        "current-batch-last-answer",
                        attempt,
                        () -> answer(scenario, current.getLast(), lastAnswer));
        var resumed = snapshot(scenario);
        var lastOracle = new Oracle(resumed.history(), attempt);
        attachOracle(last, lastOracle);
        var facts = new LinkedHashMap<String, Object>();
        facts.put("turn_resuming", resumed.turn().status().equals("resuming"));
        facts.put("command_queued", resumed.commandStatus().equals("queued"));
        facts.put("one_resume_event", resumingEvents(resumed) == resumingEvents(before) + 1);
        facts.put("one_resume_signal", resumed.commandSignal() == before.commandSignal() + 1);
        last.evidence().put("business_facts", facts);
        assertSingleCompletion(last, lastOracle, true, false);
        assertThat(lastOracle.hasUnanswered()).isFalse();
        assertOneResume(before, resumed, current.getLast());
        run(scenario);
        var completed = snapshot(scenario);
        assertThat(completed.turn().status()).isEqualTo("completed");
        assertThat(completed.turn().result()).isEqualTo("all approval rounds completed");
        assertThat(completed.reports()).isEqualTo(6);
        assertThat(completed.attempts()).isEqualTo(4);
        assertThat(completed.history()).hasSize(6).allMatch(i -> i.status().equals("answered"));
        assertOriginalBudget(scenario);
        facts.put("native_resume_completed_with_six_reports", true);
        facts.put("original_budgets_preserved", true);
    }

    @Test
    void answeredReplayAndContentConflictNeverReadBatchCompletionOrEnqueueAnotherResume()
            throws Exception {
        var scenario = scenario(OWNER, 1, 0);
        var target = pending(history(scenario)).getFirst();
        var supplied = approval(target, LARGE_REASON);
        answer(scenario, target, supplied);
        var before = snapshot(scenario);
        var replay =
                observe(
                        "answered-same-hash-replay",
                        target.attemptId(),
                        () -> answer(scenario, target, supplied));
        replay.evidence()
                .put(
                        "business_facts",
                        Map.of(
                                "idempotent_replay",
                                replay.value() != null && replay.value().idempotentReplay(),
                                "all_facts_unchanged",
                                snapshot(scenario).equals(before)));
        assertNoCompletion(replay);
        assertThat(replay.failure()).isNull();
        assertThat(replay.value().idempotentReplay()).isTrue();
        assertThat(snapshot(scenario)).isEqualTo(before);
        var conflicting = approval(target, LARGE_REASON + "X");
        var conflict =
                observe(
                        "answered-conflicting-hash",
                        target.attemptId(),
                        () -> answer(scenario, target, conflicting));
        conflict.evidence()
                .put(
                        "business_facts",
                        Map.of(
                                "all_facts_unchanged",
                                snapshot(scenario).equals(before),
                                "conflict_rejected",
                                conflict.failure() != null));
        assertNoCompletion(conflict);
        assertThat(conflict.failure()).hasMessage("idempotency_conflict");
        assertThat(snapshot(scenario)).isEqualTo(before);
    }

    @Test
    void twoDistinctLastAnswersSerializeOnTheRealCommandLockAndResumeExactlyOnce()
            throws Exception {
        var scenario = scenario(OWNER, 2, 1);
        var current = pending(history(scenario));
        var firstAnswer = approval(current.getFirst(), LARGE_REASON);
        var secondAnswer = approval(current.getLast(), LARGE_REASON);
        var before = snapshot(scenario);
        String attempt = current.getFirst().attemptId();
        Observed<AnswerReceipt> first;
        Observed<AnswerReceipt> second;
        try (var holder = lockCommand(scenario);
                var workers = Executors.newFixedThreadPool(2)) {
            int holderPid = backendPid(holder);
            var start = new CountDownLatch(1);
            var one =
                    workers.submit(
                            () -> {
                                assertThat(start.await(5, TimeUnit.SECONDS)).isTrue();
                                return observe(
                                        "concurrent-answer-one",
                                        attempt,
                                        () -> answer(scenario, current.getFirst(), firstAnswer));
                            });
            var two =
                    workers.submit(
                            () -> {
                                assertThat(start.await(5, TimeUnit.SECONDS)).isTrue();
                                return observe(
                                        "concurrent-answer-two",
                                        attempt,
                                        () -> answer(scenario, current.getLast(), secondAnswer));
                            });
            start.countDown();
            try {
                awaitTwoCommandWaiters(holderPid);
            } finally {
                holder.rollback();
            }
            first = one.get(10, TimeUnit.SECONDS);
            second = two.get(10, TimeUnit.SECONDS);
        }
        assertThat(first.failure()).isNull();
        assertThat(second.failure()).isNull();
        assertThat(first.value().idempotentReplay()).isFalse();
        assertThat(second.value().idempotentReplay()).isFalse();
        assertThat(first.mapped().queries()).hasSize(1);
        assertThat(second.mapped().queries()).hasSize(1);
        boolean firstHas = first.mapped().queries().getFirst().hasUnanswered();
        boolean secondHas = second.mapped().queries().getFirst().hasUnanswered();
        assertThat(List.of(firstHas, secondHas)).containsExactlyInAnyOrder(true, false);
        var allAnswered =
                answeredHistory(
                        answeredHistory(before.history(), current.getFirst(), firstAnswer),
                        current.getLast(),
                        secondAnswer);
        // 并发完成后数据库已是全 answered，不能把该终态冒充首个回答查询时的 oracle。
        // 初始两条 distinct pending + command 串行锁 + 两次非 replay 成功，要求恰有一个仍有未答，另一个全部回答。
        // 对首答只加入其自身回答，对末答加入两条回答；若首答错误提前恢复，另一请求会被 requirePending 拒绝。
        var firstOracle =
                new Oracle(
                        firstHas
                                ? answeredHistory(before.history(), current.getFirst(), firstAnswer)
                                : allAnswered,
                        attempt);
        var secondOracle =
                new Oracle(
                        secondHas
                                ? answeredHistory(before.history(), current.getLast(), secondAnswer)
                                : allAnswered,
                        attempt);
        attachOracle(first, firstOracle);
        attachOracle(second, secondOracle);
        assertSingleCompletion(first, firstOracle, true, false);
        assertSingleCompletion(second, secondOracle, true, false);
        var after = snapshot(scenario);
        var last = firstHas ? current.getLast() : current.getFirst();
        assertOneResume(before, after, last);
        assertThat(after.history()).isEqualTo(allAnswered);
        var facts =
                Map.<String, Object>of(
                        "both_waited_for_real_command_lock",
                        true,
                        "both_distinct_answers_accepted",
                        true,
                        "one_resume_event",
                        true,
                        "one_resume_signal",
                        true,
                        "no_new_attempt_before_worker",
                        after.attempts() == before.attempts());
        first.evidence().put("business_facts", facts);
        second.evidence().put("business_facts", facts);
        run(scenario);
        assertThat(turn(scenario).status()).isEqualTo("completed");
        assertThat(reports(scenario)).isEqualTo(4);
        assertThat(attempts(scenario)).isEqualTo(before.attempts() + 1);
        assertThat(tasks.claim("no-extra-resume", 60000)).isNull();
    }

    @Test
    void
            ownershipStaleExpiryAndCancellationGuardsStayBeforeCompletionAndExpiredSiblingsStillBlockIt()
                    throws Exception {
        var ordinary = scenario(OWNER, 1, 0);
        var target = pending(history(ordinary)).getFirst();
        var before = snapshot(ordinary);
        var wrongOwner =
                observe(
                        "wrong-owner-answer",
                        target.attemptId(),
                        () ->
                                governance.answer(
                                        OTHER,
                                        ordinary.conversation(),
                                        ordinary.turn(),
                                        target.id(),
                                        UUID.randomUUID().toString(),
                                        approval(target, LARGE_REASON)));
        assertGuard(wrongOwner, ordinary, before, "not_found");
        var wrongRevisionAnswer =
                new Answer(
                        "approval",
                        target.revision() + 1,
                        "approve",
                        target.actionHash(),
                        null,
                        LARGE_REASON);
        var wrongRevision =
                observe(
                        "wrong-revision-answer",
                        target.attemptId(),
                        () -> answer(ordinary, target, wrongRevisionAnswer));
        assertGuard(wrongRevision, ordinary, before, "interaction_mismatch");
        var expired = scenario(OWNER, 1, 0);
        var expiredTarget = pending(history(expired)).getFirst();
        expire(expiredTarget);
        var expiredBefore = snapshot(expired);
        var rejectedExpiry =
                observe(
                        "expired-target-answer",
                        expiredTarget.attemptId(),
                        () ->
                                answer(
                                        expired,
                                        expiredTarget,
                                        approval(expiredTarget, LARGE_REASON)));
        assertGuard(rejectedExpiry, expired, expiredBefore, "stale_interaction");
        var cancelled = scenario(OWNER, 1, 0);
        var cancelledTarget = pending(history(cancelled)).getFirst();
        tasks.cancel(
                OWNER, cancelled.conversation(), cancelled.turn(), UUID.randomUUID().toString());
        var cancelledBefore = snapshot(cancelled);
        assertThat(cancelledBefore.turn().status()).isEqualTo("cancelled");
        assertThat(cancelledBefore.history()).allMatch(i -> i.status().equals("invalid"));
        var rejectedInvalid =
                observe(
                        "cancelled-invalid-answer",
                        cancelledTarget.attemptId(),
                        () ->
                                answer(
                                        cancelled,
                                        cancelledTarget,
                                        approval(cancelledTarget, LARGE_REASON)));
        assertGuard(rejectedInvalid, cancelled, cancelledBefore, "stale_interaction");
        var sibling = scenario(OWNER, 2, 1);
        var batch = pending(history(sibling));
        expire(batch.getLast());
        var siblingBefore = snapshot(sibling);
        assertThat(
                        siblingBefore.history().stream()
                                .filter(i -> i.id().equals(batch.getFirst().id()))
                                .findFirst()
                                .orElseThrow()
                                .live())
                .isTrue();
        assertThat(
                        siblingBefore.history().stream()
                                .filter(i -> i.id().equals(batch.getLast().id()))
                                .findFirst()
                                .orElseThrow()
                                .live())
                .isFalse();
        var supplied = approval(batch.getFirst(), LARGE_REASON);
        var validAnswer =
                observe(
                        "expired-sibling-answer",
                        batch.getFirst().attemptId(),
                        () -> answer(sibling, batch.getFirst(), supplied));
        var siblingAfter = snapshot(sibling);
        var oracle = new Oracle(siblingAfter.history(), batch.getFirst().attemptId());
        attachOracle(validAnswer, oracle);
        validAnswer
                .evidence()
                .put(
                        "business_facts",
                        Map.of(
                                "live_target_answered",
                                true,
                                "expired_pending_sibling_retained",
                                siblingAfter.history().stream()
                                        .anyMatch(
                                                i ->
                                                        i.id().equals(batch.getLast().id())
                                                                && i.status().equals("pending")
                                                                && !i.live()),
                                "turn_stays_waiting",
                                siblingAfter.turn().status().equals("waiting"),
                                "command_stays_done",
                                siblingAfter.commandStatus().equals("done"),
                                "no_new_attempt",
                                siblingAfter.attempts() == siblingBefore.attempts()));
        assertSingleCompletion(validAnswer, oracle, true, false);
        assertThat(oracle.hasUnanswered()).isTrue();
        assertNoAdvance(siblingBefore, siblingAfter);
        assertThat(siblingAfter.history())
                .isEqualTo(answeredHistory(siblingBefore.history(), batch.getFirst(), supplied));
    }

    @Test
    void failedResumeTransitionRollsBackTheAnswerAndRetryUsesTheSameCompletionMeaning()
            throws Exception {
        var scenario = scenario(OWNER, 2, 1);
        var batch = pending(history(scenario));
        answer(scenario, batch.getFirst(), approval(batch.getFirst(), LARGE_REASON));
        var target = batch.getLast();
        var supplied = approval(target, LARGE_REASON);
        var before = snapshot(scenario);
        var queryOracle =
                new Oracle(answeredHistory(before.history(), target, supplied), target.attemptId());
        assertThat(queryOracle.hasUnanswered()).isFalse();
        raw.execute(
                "CREATE FUNCTION business.completion_resume_failure() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN IF NEW.status='resuming' THEN RAISE EXCEPTION 'completion_resume_failure' USING ERRCODE='23514'; END IF; RETURN NEW; END $$");
        raw.execute(
                "CREATE TRIGGER completion_resume_failure AFTER UPDATE ON business.turns FOR EACH ROW EXECUTE FUNCTION business.completion_resume_failure()");
        Observed<AnswerReceipt> failed;
        try {
            failed =
                    observe(
                            "resume-transition-rollback",
                            target.attemptId(),
                            () -> answer(scenario, target, supplied));
        } finally {
            raw.execute("DROP TRIGGER completion_resume_failure ON business.turns");
            raw.execute("DROP FUNCTION business.completion_resume_failure()");
        }
        attachOracle(failed, queryOracle);
        failed.evidence()
                .put(
                        "business_facts",
                        Map.of(
                                "all_facts_rolled_back",
                                snapshot(scenario).equals(before),
                                "last_answer_still_pending",
                                pending(history(scenario)).size() == 1,
                                "no_resume_signal",
                                signals.generation(WorkSignals.Queue.COMMAND)
                                        == before.commandSignal()));
        assertThat(failed.failure())
                .isInstanceOf(DataIntegrityViolationException.class)
                .hasMessageContaining("completion_resume_failure");
        assertSingleCompletion(failed, queryOracle, true, true);
        assertThat(snapshot(scenario)).isEqualTo(before);
        var retried =
                observe(
                        "resume-transition-retry",
                        target.attemptId(),
                        () -> answer(scenario, target, supplied));
        var after = snapshot(scenario);
        attachOracle(retried, new Oracle(after.history(), target.attemptId()));
        retried.evidence()
                .put(
                        "business_facts",
                        Map.of(
                                "retry_is_first_acceptance",
                                retried.value() != null && !retried.value().idempotentReplay(),
                                "one_resume_signal",
                                after.commandSignal() == before.commandSignal() + 1,
                                "one_resume_event",
                                resumingEvents(after) == resumingEvents(before) + 1));
        assertSingleCompletion(retried, queryOracle, true, false);
        assertThat(retried.value().idempotentReplay()).isFalse();
        assertOneResume(before, after, target);
        run(scenario);
        assertThat(turn(scenario).status()).isEqualTo("completed");
        assertThat(reports(scenario)).isEqualTo(4);
        assertThat(attempts(scenario)).isEqualTo(before.attempts() + 1);
    }

    @Test
    void isolatedMapperPredicateCoversRealLifecycleStatusesAndMismatchedTurnAttemptKeys()
            throws Exception {
        var scenario = scenario(OWNER, 2, 1);
        var other = scenario(OTHER, 1, 0);
        var initial = history(scenario);
        String answeredAttempt =
                initial.stream()
                        .filter(i -> i.status().equals("answered"))
                        .findFirst()
                        .orElseThrow()
                        .attemptId();
        var current = pending(initial);
        String currentAttempt = current.getFirst().attemptId();
        String otherAttempt = pending(history(other)).getFirst().attemptId();
        assertThat(answeredAttempt).isNotEqualTo(currentAttempt).isNotEqualTo(otherAttempt);
        assertThat(currentAttempt).isNotEqualTo(otherAttempt);
        String absentAttempt = UUID.randomUUID().toString();
        String absentTurn = UUID.randomUUID().toString();
        assertPredicate("predicate-empty-attempt", scenario.turn(), absentAttempt, initial, false);
        assertPredicate("predicate-empty-turn", absentTurn, currentAttempt, List.of(), false);
        assertPredicate("predicate-all-answered", scenario.turn(), answeredAttempt, initial, false);
        assertPredicate("predicate-pending", scenario.turn(), currentAttempt, initial, true);
        current.forEach(this::expire);
        var expired = history(scenario);
        assertThat(expired.stream().filter(i -> i.attemptId().equals(currentAttempt)))
                .allMatch(i -> i.status().equals("pending") && !i.live());
        assertPredicate(
                "predicate-expired-pending", scenario.turn(), currentAttempt, expired, true);
        tasks.cancel(OWNER, scenario.conversation(), scenario.turn(), UUID.randomUUID().toString());
        var invalid = history(scenario);
        assertThat(turn(scenario).status()).isEqualTo("cancelled");
        assertThat(invalid.stream().filter(i -> i.attemptId().equals(currentAttempt)))
                .allMatch(i -> i.status().equals("invalid"));
        assertPredicate("predicate-invalid", scenario.turn(), currentAttempt, invalid, true);
        assertPredicate(
                "predicate-wrong-turn-attempt",
                other.turn(),
                currentAttempt,
                history(other),
                false);
        assertPredicate(
                "predicate-wrong-attempt-turn", scenario.turn(), otherAttempt, invalid, false);
        assertThat(turn(scenario).status()).isEqualTo("cancelled");
        assertThat(turn(other).status()).isEqualTo("waiting");
        assertThat(history(scenario)).isEqualTo(invalid);
    }

    /** 动态 statement 身份让同一 fixture 在旧生产上编译；隔离矩阵不调用尚不存在的新 Java 方法。 */
    private void assertPredicate(
            String phase,
            String turn,
            String attempt,
            List<Interaction> history,
            boolean expected) {
        var oracle = new Oracle(history, attempt);
        assertThat(oracle.hasUnanswered()).isEqualTo(expected);
        var measured =
                observe(
                        phase,
                        attempt,
                        () ->
                                tx.execute(
                                        status -> {
                                            if (baseline) {
                                                List<Interaction> rows =
                                                        sessions.selectList(
                                                                InteractionCompletionRecorder
                                                                        .HISTORY_ID,
                                                                turn);
                                                return !rows.stream()
                                                        .filter(i -> i.attemptId().equals(attempt))
                                                        .allMatch(
                                                                i -> i.status().equals("answered"));
                                            }
                                            List<Boolean> rows =
                                                    sessions.selectList(
                                                            InteractionCompletionRecorder.EXISTS_ID,
                                                            Map.of(
                                                                    "turn", turn, "attempt",
                                                                    attempt));
                                            assertThat(rows).hasSize(1);
                                            assertThat(rows.getFirst()).isNotNull();
                                            return rows.getFirst();
                                        }));
        attachOracle(measured, oracle);
        measured.evidence()
                .put(
                        "business_facts",
                        Map.of(
                                "isolated_mapper_only",
                                true,
                                "predicate_matches_original_all_match",
                                measured.value() != null && measured.value() == expected));
        assertSingleCompletion(measured, oracle, false, false);
        assertThat(measured.sql().statements()).isOne();
        assertThat(measured.value()).isEqualTo(expected);
    }

    private Scenario scenario(Owner owner, int batches, int completedBatches) throws Exception {
        String conversation = tasks.createConversation(owner);
        var accepted =
                tasks.submit(
                        owner,
                        conversation,
                        UUID.randomUUID().toString(),
                        "approval completion rounds");
        var scenario = new Scenario(owner, conversation, accepted.turnId(), batches);
        run(scenario);
        for (int round = 0; round < completedBatches; round++) {
            var current = pending(history(scenario));
            assertThat(current).hasSize(2);
            var before = snapshot(scenario);
            answer(scenario, current.getFirst(), approval(current.getFirst(), LARGE_REASON));
            assertNoAdvance(before, snapshot(scenario));
            answer(scenario, current.getLast(), approval(current.getLast(), LARGE_REASON));
            assertThat(turn(scenario).status()).isEqualTo("resuming");
            run(scenario);
        }
        assertThat(turn(scenario).status()).isEqualTo("waiting");
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
            var claim = tasks.claim("interaction-completion-query-worker", 60000);
            assertThat(claim).isNotNull();
            assertThat(claim.turn().id()).isEqualTo(scenario.turn());
            worker.execute(claim);
        }
    }

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
                    return "interaction-completion-approval-rounds";
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
                    if (resolved == batches * 2L)
                        return Flux.just(
                                ChatResponse.builder()
                                        .id("completion-rounds-final")
                                        .content(
                                                List.of(
                                                        TextBlock.builder()
                                                                .text(
                                                                        "all approval rounds completed")
                                                                .build()))
                                        .build());
                    if (resolved % 2 != 0 || resolved > batches * 2L)
                        throw new IllegalStateException("Incomplete approval batch was resumed");
                    int round = (int) (resolved / 2) + 1;
                    var calls = new ArrayList<ToolUseBlock>();
                    for (int index = 1; index <= 2; index++) {
                        var input =
                                Map.<String, Object>of(
                                        "title", "completion-round-" + round + "-report-" + index);
                        calls.add(
                                ToolUseBlock.builder()
                                        .id("completion-round-" + round + "-call-" + index)
                                        .name("create_report")
                                        .input(input)
                                        .content(PayloadJson.canonical(input))
                                        .build());
                    }
                    return Flux.just(
                            ChatResponse.builder()
                                    .id("completion-round-" + round)
                                    .content(new ArrayList<>(calls))
                                    .build());
                }
            };
        }
    }

    private Answer approval(Interaction interaction, String reason) {
        var answer =
                new Answer(
                        "approval",
                        interaction.revision(),
                        "approve",
                        interaction.actionHash(),
                        null,
                        reason);
        assertThat(reason.length()).isLessThanOrEqualTo(2000);
        assertThat(PayloadJson.canonical(answer).getBytes(StandardCharsets.UTF_8).length)
                .isLessThanOrEqualTo(16384);
        return answer;
    }

    private AnswerReceipt answer(Scenario scenario, Interaction interaction, Answer answer) {
        return governance.answer(
                scenario.owner(),
                scenario.conversation(),
                scenario.turn(),
                interaction.id(),
                UUID.randomUUID().toString(),
                answer);
    }

    /** 仅在内存中计算当前事务 UPDATE 后的 oracle；不使用 SQL 制造回答或改变业务状态。 */
    private static List<Interaction> answeredHistory(
            List<Interaction> rows, Interaction target, Answer answer) {
        String json = PayloadJson.canonical(answer);
        String hash = ExecutionScope.hash(List.of(json));
        assertThat(rows.stream().filter(i -> i.id().equals(target.id()))).hasSize(1);
        return rows.stream()
                .map(
                        i ->
                                i.id().equals(target.id())
                                        ? new Interaction(
                                                i.id(),
                                                i.turnId(),
                                                i.commandId(),
                                                i.attemptId(),
                                                i.stateVersion(),
                                                i.grantRevision(),
                                                i.releaseHash(),
                                                i.kind(),
                                                i.callId(),
                                                i.tool(),
                                                i.actionHash(),
                                                i.schema(),
                                                i.question(),
                                                i.revision(),
                                                "answered",
                                                i.live(),
                                                hash,
                                                json)
                                        : i)
                .toList();
    }

    private <T> Observed<T> observe(String phase, String attempt, Supplier<T> operation) {
        counter.begin(phase);
        recorder.begin(phase, attempt);
        T value = null;
        Throwable failure = null;
        try {
            value = operation.get();
        } catch (Throwable caught) {
            failure = caught;
        }
        JdbcExecutionCounter.Measurement sql;
        InteractionCompletionRecorder.Measurement mapped;
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
        return new Observed<>(value, failure, sql, mapped, evidence);
    }

    private void attachOracle(Observed<?> observed, Oracle oracle) {
        observed.evidence()
                .put(
                        "oracle",
                        Map.of(
                                "has_unanswered",
                                oracle.hasUnanswered(),
                                "current_attempt_rows",
                                oracle.currentRows(),
                                "history_rows",
                                oracle.history().size(),
                                "history_response_nonnull_count",
                                responseCount(oracle.history()),
                                "history_response_utf8_logical_bytes",
                                responseBytes(oracle.history())));
    }

    private void assertSingleCompletion(
            Observed<?> observed, Oracle oracle, boolean afterAnswerUpdate, boolean rollback) {
        if (!rollback) assertThat(observed.failure()).isNull();
        assertQuerySequence(observed);
        assertThat(observed.mapped().queries()).hasSize(1);
        var query = observed.mapped().queries().getFirst();
        assertThat(query.templateSha256())
                .isEqualTo(
                        baseline
                                ? InteractionCompletionRecorder.HISTORY_HASH
                                : InteractionCompletionRecorder.EXISTS_HASH);
        assertThat(query.entityRows()).isEqualTo(baseline ? oracle.history().size() : 0);
        assertThat(query.scalarRows()).isEqualTo(baseline ? 0 : 1);
        assertThat(query.responseNonNullCount())
                .isEqualTo(baseline ? responseCount(oracle.history()) : 0);
        assertThat(query.responseUtf8LogicalBytes())
                .isEqualTo(baseline ? responseBytes(oracle.history()) : 0);
        assertThat(query.hasUnanswered()).isEqualTo(oracle.hasUnanswered());
        assertThat(observed.sql().commits()).isEqualTo(rollback ? 0 : 1);
        assertThat(observed.sql().rollbacks()).isEqualTo(rollback ? 1 : 0);
        if (afterAnswerUpdate) {
            var executions = observed.sql().executions();
            var updates =
                    executions.stream()
                            .filter(e -> e.templateSha256().equals(answerUpdateHash))
                            .toList();
            assertThat(updates).hasSize(1);
            var completion =
                    executions.stream()
                            .filter(e -> e.templateSha256().equals(query.templateSha256()))
                            .toList();
            assertThat(completion).hasSize(1);
            assertThat(executions.indexOf(updates.getFirst()))
                    .isLessThan(executions.indexOf(completion.getFirst()));
            assertThat(updates.getFirst().connection())
                    .isEqualTo(completion.getFirst().connection());
            assertThat(updates.getFirst().thread()).isEqualTo(completion.getFirst().thread());
            assertThat(templateCount(observed, resumeUpdateHash))
                    .isEqualTo(oracle.hasUnanswered() ? 0 : 1);
            assertThat(observed.sql().count("turn_state_update"))
                    .isEqualTo(oracle.hasUnanswered() ? 0 : 1);
            assertThat(observed.sql().count("event_insert"))
                    .isEqualTo(oracle.hasUnanswered() || rollback ? 0 : 1);
        } else {
            assertThat(templateCount(observed, answerUpdateHash)).isZero();
            assertThat(templateCount(observed, resumeUpdateHash)).isZero();
        }
    }

    private void assertNoCompletion(Observed<?> observed) {
        assertQuerySequence(observed);
        assertThat(observed.mapped().queries()).isEmpty();
        assertThat(templateCount(observed, answerUpdateHash)).isZero();
        assertThat(templateCount(observed, resumeUpdateHash)).isZero();
        assertThat(observed.sql().count("turn_state_update")).isZero();
        assertThat(observed.sql().count("event_insert")).isZero();
    }

    private void assertQuerySequence(Observed<?> observed) {
        assertThat(observed.mapped().phase()).isEqualTo(observed.sql().phase());
        var expectedHashes =
                observed.sql().executions().stream()
                        .filter(
                                e ->
                                        Set.of(
                                                        InteractionCompletionRecorder.HISTORY_HASH,
                                                        InteractionCompletionRecorder.EXISTS_HASH)
                                                .contains(e.templateSha256()))
                        .map(JdbcExecutionCounter.SqlExecution::templateSha256)
                        .toList();
        assertThat(
                        observed.mapped().queries().stream()
                                .map(InteractionCompletionRecorder.QueryObservation::templateSha256)
                                .toList())
                .isEqualTo(expectedHashes);
    }

    private void assertGuard(
            Observed<?> observed, Scenario scenario, Snapshot before, String reason) {
        observed.evidence()
                .put(
                        "business_facts",
                        Map.of(
                                "guard_rejected",
                                observed.failure() != null,
                                "all_facts_unchanged",
                                snapshot(scenario).equals(before)));
        assertThat(observed.failure()).hasMessage(reason);
        assertNoCompletion(observed);
        assertThat(snapshot(scenario)).isEqualTo(before);
    }

    private void assertNoAdvance(Snapshot before, Snapshot after) {
        assertThat(after.turn()).isEqualTo(before.turn());
        assertThat(after.commandStatus()).isEqualTo(before.commandStatus()).isEqualTo("done");
        assertThat(after.resumeId()).isEqualTo(before.resumeId());
        assertThat(after.events()).isEqualTo(before.events());
        assertThat(after.attempts()).isEqualTo(before.attempts());
        assertThat(after.reports()).isEqualTo(before.reports());
        assertThat(after.commandSignal()).isEqualTo(before.commandSignal());
    }

    private void assertOneResume(Snapshot before, Snapshot after, Interaction last) {
        assertThat(after.turn().status()).isEqualTo("resuming");
        assertThat(after.turn().revision()).isEqualTo(before.turn().revision() + 1);
        assertThat(after.commandStatus()).isEqualTo("queued");
        assertThat(after.resumeId()).isEqualTo(last.id());
        assertThat(after.attempts()).isEqualTo(before.attempts());
        assertThat(after.reports()).isEqualTo(before.reports());
        assertThat(resumingEvents(after)).isEqualTo(resumingEvents(before) + 1);
        assertThat(after.commandSignal()).isEqualTo(before.commandSignal() + 1);
    }

    private void assertOriginalBudget(Scenario scenario) {
        var definition = tasks.release(turn(scenario).releaseHash());
        assertThat(definition.maxModelCalls()).isEqualTo(12);
        assertThat(definition.maxToolCalls()).isEqualTo(32);
        var policy =
                raw.queryForMap(
                        "SELECT model_calls,tool_calls FROM business.turn_policies WHERE turn_id=?",
                        scenario.turn());
        assertThat(((Number) policy.get("model_calls")).intValue())
                .isPositive()
                .isLessThanOrEqualTo(definition.maxModelCalls());
        assertThat(((Number) policy.get("tool_calls")).intValue())
                .isPositive()
                .isLessThanOrEqualTo(definition.maxToolCalls());
    }

    private Snapshot snapshot(Scenario scenario) {
        var turn = turn(scenario);
        var history = history(scenario);
        var command =
                raw.queryForMap(
                        "SELECT status,resume_id FROM business.commands WHERE turn_id=?",
                        scenario.turn());
        return new Snapshot(
                turn,
                history,
                tasks.events(scenario.owner(), scenario.conversation(), scenario.turn(), 0),
                (String) command.get("status"),
                (String) command.get("resume_id"),
                attempts(scenario),
                reports(scenario),
                signals.generation(WorkSignals.Queue.COMMAND));
    }

    private List<Interaction> history(Scenario scenario) {
        var rows =
                governance.interactions(scenario.owner(), scenario.conversation(), scenario.turn());
        assertThat(rows).isSortedAccordingTo(ORDER);
        assertThat(application.getBean(ExecutionContextGateway.class).interactions(scenario.turn()))
                .isEqualTo(rows);
        return rows;
    }

    private static List<Interaction> pending(List<Interaction> history) {
        return history.stream().filter(i -> i.status().equals("pending")).sorted(ORDER).toList();
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

    private static long resumingEvents(Snapshot snapshot) {
        return snapshot.events().stream().filter(e -> e.status().equals("resuming")).count();
    }

    private Turn turn(Scenario scenario) {
        return tasks.get(scenario.owner(), scenario.conversation(), scenario.turn());
    }

    private int attempts(Scenario scenario) {
        return raw.queryForObject(
                "SELECT count(*)::int FROM business.attempts a JOIN business.commands c ON c.id=a.command_id WHERE c.turn_id=?",
                Integer.class,
                scenario.turn());
    }

    private int reports(Scenario scenario) {
        return raw.queryForObject(
                "SELECT count(*)::int FROM business.reports WHERE turn_id=?",
                Integer.class,
                scenario.turn());
    }

    private void expire(Interaction interaction) {
        assertThat(
                        raw.update(
                                "UPDATE business.interactions SET expires_at=clock_timestamp()-interval '1 second' WHERE id=?",
                                interaction.id()))
                .isOne();
    }

    private static long templateCount(Observed<?> observed, String hash) {
        return observed.sql().executions().stream()
                .filter(e -> e.templateSha256().equals(hash))
                .count();
    }

    private Connection lockCommand(Scenario scenario) throws Exception {
        String id =
                raw.queryForObject(
                        "SELECT id FROM business.commands WHERE turn_id=?",
                        String.class,
                        scenario.turn());
        var connection = original.getConnection();
        connection.setAutoCommit(false);
        try (var statement =
                connection.prepareStatement(
                        "SELECT id FROM business.commands WHERE id=? FOR UPDATE")) {
            statement.setString(1, id);
            statement.executeQuery().close();
        } catch (Exception failure) {
            connection.close();
            throw failure;
        }
        return connection;
    }

    private static int backendPid(Connection connection) throws Exception {
        try (var statement = connection.prepareStatement("SELECT pg_backend_pid()");
                var row = statement.executeQuery()) {
            assertThat(row.next()).isTrue();
            return row.getInt(1);
        }
    }

    private void awaitTwoCommandWaiters(int holderPid) throws Exception {
        long deadline = System.nanoTime() + Duration.ofSeconds(2).toNanos();
        int matched = 0;
        do {
            var waiting =
                    raw.query(
                            "SELECT pid,pg_blocking_pids(pid) AS blockers FROM pg_stat_activity WHERE datname=current_database() AND wait_event_type='Lock' AND lower(query) LIKE '%from business.commands where turn_id=%for update%'",
                            (row, index) -> {
                                var array = row.getArray("blockers");
                                try {
                                    var blockers = new HashSet<Integer>();
                                    for (var value : (Object[]) array.getArray())
                                        blockers.add(((Number) value).intValue());
                                    return new LockWaiter(row.getInt("pid"), Set.copyOf(blockers));
                                } finally {
                                    array.free();
                                }
                            });
            var graph = new HashMap<Integer, Set<Integer>>();
            waiting.forEach(w -> graph.put(w.pid(), w.blockers()));
            matched =
                    (int)
                            waiting.stream()
                                    .filter(
                                            w ->
                                                    reachesHolder(
                                                            w.pid(),
                                                            holderPid,
                                                            graph,
                                                            new HashSet<>()))
                                    .count();
            if (matched == 2) return;
            Thread.sleep(10);
        } while (System.nanoTime() < deadline);
        throw new AssertionError(
                "Both answers must really wait for the held command; observed=" + matched);
    }

    private static boolean reachesHolder(
            int pid, int holder, Map<Integer, Set<Integer>> graph, Set<Integer> visited) {
        if (pid == holder) return true;
        if (!visited.add(pid)) return false;
        return graph.getOrDefault(pid, Set.of()).stream()
                .anyMatch(blocker -> reachesHolder(blocker, holder, graph, visited));
    }

    private void closeResources() {
        try {
            if (application != null) application.close();
        } finally {
            if (postgres.isRunning()) postgres.stop();
        }
    }
}
