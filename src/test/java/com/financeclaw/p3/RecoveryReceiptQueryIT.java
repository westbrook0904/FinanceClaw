package com.financeclaw.p3;

import static org.assertj.core.api.Assertions.*;

import com.fasterxml.jackson.databind.ObjectMapper;
import com.financeclaw.app.execution.ExecutionCompletionService;
import com.financeclaw.app.execution.RecoverTaskService;
import com.financeclaw.app.execution.port.ExecutionMemory;
import com.financeclaw.app.memory.MemoryService;
import com.financeclaw.bootstrap.FinanceClawApplication;
import com.financeclaw.domain.Task.Claim;
import com.financeclaw.domain.Task.Outcome;
import com.financeclaw.domain.Task.Owner;
import com.financeclaw.fixture.GovernanceFixture;
import com.financeclaw.fixture.TaskFixture;
import com.financeclaw.infrastructure.agentscope.capability.BusinessCapabilities;
import com.financeclaw.infrastructure.agentscope.execution.TaskAgentRuntime;
import com.financeclaw.infrastructure.state.MyBatisAgentStateStore;
import com.financeclaw.infrastructure.state.RuntimeMapper;
import com.financeclaw.infrastructure.task.TaskMapper;
import com.financeclaw.m2.ScriptedRuntime;
import com.financeclaw.support.PayloadDigest;
import java.nio.file.Files;
import java.nio.file.Path;
import java.sql.Connection;
import java.time.Duration;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Locale;
import java.util.Map;
import java.util.UUID;
import java.util.concurrent.CopyOnWriteArrayList;
import java.util.concurrent.CountDownLatch;
import java.util.concurrent.Executors;
import java.util.concurrent.TimeUnit;
import java.util.function.Supplier;
import javax.sql.DataSource;
import org.apache.ibatis.session.SqlSessionFactory;
import org.junit.jupiter.api.*;
import org.junit.jupiter.api.condition.DisabledIfSystemProperty;
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
import org.testcontainers.containers.PostgreSQLContainer;

/**
 * sealed receipt 恢复的真实 PG 基线。TaskFixture 只驱动生产受理/领取/执行/封存步骤；不 mock 仓库、 内存治理或运行时。观察窗口只包
 * recoverExpired/finish，准备、故障注入及业务核查均使用未计数数据源。 baseline 标记只控制重复读取次数的预期，不更改任何业务操作或故障语义。
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
@Execution(ExecutionMode.SAME_THREAD)
class RecoveryReceiptQueryIT {
    private static final Owner OWNER = new Owner("recovery-receipt-query", "alice");
    private final boolean baseline = Boolean.getBoolean("financeclaw.test.query-baseline");
    private final PostgreSQLContainer<?> postgres =
            new PostgreSQLContainer<>("postgres:16.10-alpine");
    private final List<Map<String, Object>> measurements = new CopyOnWriteArrayList<>();
    private ConfigurableApplicationContext application;
    private DataSource original;
    private JdbcTemplate raw;
    private JdbcExecutionCounter counter;
    private TaskFixture tasks;
    private TaskAgentRuntime runtime;
    private RuntimeMapper states;
    private RecoverTaskService recovery;
    private ExecutionCompletionService completion;
    private String receiptTemplate;
    private long excludedObservations;

    record Scenario(Claim claim, Outcome outcome) {}

    record Observed<T>(
            T value,
            Throwable failure,
            JdbcExecutionCounter.Measurement sql,
            Map<String, Object> evidence) {}

    static final class RawSource {
        private DataSource source;
    }

    @TestConfiguration(proxyBeanMethods = false)
    @Import({TaskFixture.class, GovernanceFixture.class})
    static class CountingConfiguration {
        @Bean
        static JdbcExecutionCounter recoveryReceiptCounter() {
            return new JdbcExecutionCounter();
        }

        @Bean
        static RawSource recoveryRawSource() {
            return new RawSource();
        }

        @Bean
        static BeanPostProcessor recoveryCountingSource(
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
                                    "--spring.datasource.hikari.maximum-pool-size=6",
                                    "--spring.config.import=",
                                    "--server.port=0",
                                    "--spring.main.banner-mode=off",
                                    "--financeclaw.worker.enabled=false",
                                    "--financeclaw.integrations.enabled=false",
                                    "--financeclaw.memory.enabled=false",
                                    "--financeclaw.memory.profile-enabled=true",
                                    "--financeclaw.memory.profile-directory=",
                                    "--financeclaw.memory.forget-ledger.enabled=false",
                                    "--financeclaw.feishu.enabled=false",
                                    "--financeclaw.business.ziwei.enabled=false",
                                    "--financeclaw.auth.token=synthetic-recovery-query-token",
                                    "--financeclaw.model.name=offline",
                                    "--financeclaw.fallback.name=");
            counter = application.getBean(JdbcExecutionCounter.class);
            original = application.getBean(RawSource.class).source;
            raw = new JdbcTemplate(original);
            tasks = application.getBean(TaskFixture.class);
            runtime = application.getBean(TaskAgentRuntime.class);
            states = application.getBean(RuntimeMapper.class);
            recovery = application.getBean(RecoverTaskService.class);
            completion = application.getBean(ExecutionCompletionService.class);
            assertThat(application.getBean(ExecutionMemory.class))
                    .isInstanceOf(MemoryService.class);
            var statement =
                    application
                            .getBean(SqlSessionFactory.class)
                            .getConfiguration()
                            .getMappedStatement(TaskMapper.class.getName() + ".getAttemptReceipt");
            receiptTemplate =
                    PayloadDigest.sha256(
                            statement
                                    .getBoundSql("attempt")
                                    .getSql()
                                    .replaceAll("\\s+", " ")
                                    .trim()
                                    .toLowerCase(Locale.ROOT));
        } catch (RuntimeException | Error failure) {
            closeResources();
            throw failure;
        }
    }

    @BeforeEach
    void reset() {
        dropFailureTrigger();
        raw.execute(
                "TRUNCATE agent_runtime.states,business.conversations,memory.owners,"
                        + "memory.jobs,memory.operations,memory.audit CASCADE");
        raw.update(
                "UPDATE memory.privacy_ledger_binding SET lineage=?,active=false",
                UUID.randomUUID().toString());
    }

    @AfterEach
    void removeFault() {
        dropFailureTrigger();
    }

    @AfterAll
    void writeEvidenceAndClose() throws Exception {
        try {
            Path output = Path.of("target/p3a11/recovery-receipt-sql.json");
            Files.createDirectories(output.getParent());
            var report = new LinkedHashMap<String, Object>();
            report.put("schema", "recovery-receipt-sql/1");
            report.put("scope", "real-pg-owned-spring-production-task-memory-and-runtime");
            report.put("mode", baseline ? "before" : "after");
            report.put("background_pollers_enabled", false);
            report.put("measurement_boundary", "only-recoverExpired-or-normal-finish");
            report.put("sql_semantics", "jdbc-execute-attempts-including-failed-executions");
            report.put("receipt_template_sha256", receiptTemplate);
            report.put("excluded_observation_queries", excludedObservations);
            report.put("measurements", measurements);
            new ObjectMapper().writerWithDefaultPrettyPrinter().writeValue(output.toFile(), report);
        } finally {
            closeResources();
        }
    }

    @ParameterizedTest
    @ValueSource(strings = {"completed", "waiting"})
    void sealedRecoveryFinishesFromTheFrozenReceiptWithoutRepeatingModelOrToolWork(String outcome) {
        var scenario = prepare(outcome, true);
        var before = facts(scenario);
        expire(scenario);
        var recovered = observe("sealed-" + outcome + "-recovery", recovery::recoverExpired);
        var after = attachFacts(recovered, scenario);
        assertThat(recovered.failure()).isNull();
        assertThat(recovered.value()).isEqualTo(1);
        assertReceiptReads(recovered, baseline ? 2 : 1);
        assertAuthoritativeReads(recovered);
        assertRecoveryTransaction(recovered, false);
        assertThat(after.get("turn_status")).isEqualTo(outcome);
        assertThat(after.get("command_status")).isEqualTo("done");
        assertThat(after.get("model_calls")).isEqualTo(before.get("model_calls"));
        assertThat(after.get("tool_calls")).isEqualTo(before.get("tool_calls"));
        assertThat(after.get("attempt_count")).isEqualTo(1);
        assertThat(after.get("sdk_version")).isEqualTo(before.get("sdk_version"));
        assertThat(after.get("receipt_outcome")).isEqualTo(outcome);
        assertThat(after.get("assistant_count")).isEqualTo(outcome.equals("completed") ? 1 : 0);
        if (outcome.equals("completed")) {
            assertThat(after.get("turn_result_sha256"))
                    .isEqualTo(PayloadDigest.sha256(scenario.outcome().result()));
        } else {
            assertThat(after.get("pending_interactions")).isEqualTo(1);
            assertThat(after.get("turn_result_sha256")).isNull();
        }
        assertEmptyRepeat("sealed-" + outcome + "-repeat-empty", scenario);
    }

    @ParameterizedTest
    @ValueSource(strings = {"no-sdk", "sdk-unsealed", "missing-receipt"})
    void missingOrUnsealedReceiptStaysBlockedWithoutGuessingExecutionSuccess(String kind) {
        var scenario = prepare(kind.equals("sdk-unsealed") ? "completed" : "no-sdk", false);
        if (kind.equals("missing-receipt")) {
            // 无 SDK/work context/interaction 外键的未执行 attempt，模拟恢复数据缺失，而不是重建结果。
            assertThat(
                            integer(
                                    "SELECT count(*) FROM agent_runtime.work_contexts WHERE turn_id=?",
                                    turnId(scenario)))
                    .isZero();
            assertThat(
                            raw.update(
                                    "DELETE FROM business.attempts WHERE id=? AND outcome IS NULL",
                                    attemptId(scenario)))
                    .isOne();
        }
        expire(scenario);
        var recovered = observe("unconfirmed-" + kind + "-recovery", recovery::recoverExpired);
        var after = attachFacts(recovered, scenario);
        assertThat(recovered.failure()).isNull();
        assertThat(recovered.value()).isEqualTo(1);
        assertReceiptReads(recovered, 1);
        assertThat(recovered.sql().count("sdk_state_read")).isZero();
        assertThat(recovered.sql().count("release_lookup")).isZero();
        assertRecoveryTransaction(recovered, false);
        assertThat(after.get("turn_status")).isEqualTo("blocked");
        assertThat(after.get("turn_reason")).isEqualTo("unconfirmed_execution");
        assertThat(after.get("assistant_count")).isEqualTo(0);
        assertThat(after.get("command_status")).isEqualTo("done");
        assertEmptyRepeat("unconfirmed-" + kind + "-repeat-empty", scenario);
    }

    @ParameterizedTest
    @ValueSource(strings = {"tombstone", "version"})
    void sealedReceiptStillRequiresItsExactLiveCheckpointVersion(String change) {
        var scenario = prepare("completed", true);
        var key = stateKey(scenario);
        var previous = states.getState(key);
        assertThat(previous).isNotNull();
        assertThat(previous.version()).isEqualTo(scenario.outcome().stateVersion());
        if (change.equals("tombstone")) {
            new MyBatisAgentStateStore(states, scenario.claim().scope())
                    .delete(key.user(), key.session(), key.key());
        } else {
            assertThat(
                            states.updateStateByExpectedVersion(
                                    key, previous.payload(), previous.version()))
                    .isOne();
        }
        var changed = states.getState(key);
        assertThat(changed.version()).isEqualTo(previous.version() + 1);
        expire(scenario);
        var recovered = observe("checkpoint-" + change + "-recovery", recovery::recoverExpired);
        var after = attachFacts(recovered, scenario);
        assertThat(recovered.failure()).isNull();
        assertThat(recovered.value()).isEqualTo(1);
        assertReceiptReads(recovered, baseline ? 2 : 1);
        assertAuthoritativeReads(recovered);
        assertRecoveryTransaction(recovered, false);
        assertThat(after.get("turn_status")).isEqualTo("blocked");
        assertThat(after.get("turn_reason")).isEqualTo("checkpoint_mismatch");
        assertThat(after.get("assistant_count")).isEqualTo(0);
        assertThat(after.get("sdk_version")).isEqualTo(changed.version());
        assertThat(after.get("sdk_deleted")).isEqualTo(change.equals("tombstone"));
    }

    @Test
    void receiptFenceMismatchFailsBeforeCheckpointReadsAndRollsBackTheRecoveryTransaction() {
        var scenario = prepare("completed", true);
        assertThat(
                        raw.update(
                                "UPDATE business.attempts SET fence=fence+1 WHERE id=?",
                                attemptId(scenario)))
                .isOne();
        var before = facts(scenario);
        expire(scenario);
        var recovered = observe("receipt-fence-mismatch-recovery", recovery::recoverExpired);
        var after = attachFacts(recovered, scenario);
        assertThat(recovered.failure())
                .isInstanceOf(IllegalStateException.class)
                .hasMessage("Missing sealed execution receipt");
        assertReceiptReads(recovered, baseline ? 2 : 1);
        assertThat(recovered.sql().count("sdk_state_read")).isZero();
        assertThat(recovered.sql().count("turn_state_update")).isZero();
        assertRecoveryTransaction(recovered, true);
        assertThat(after.get("turn_status")).isEqualTo("running");
        assertThat(after.get("turn_revision")).isEqualTo(before.get("turn_revision"));
        assertThat(after.get("event_count")).isEqualTo(before.get("event_count"));
        assertThat(after.get("assistant_count")).isEqualTo(0);
        assertThat(after.get("command_status")).isEqualTo("running");
        assertThat(after.get("command_fence")).isEqualTo(before.get("command_fence"));
    }

    @Test
    void journalInsertionFailureRollsBackCompletionAndTheSameSealedReceiptCanThenRecoverOnce() {
        var scenario = prepare("completed", true);
        expire(scenario);
        var before = facts(scenario);
        raw.execute(
                "CREATE FUNCTION business.recovery_receipt_query_fail() RETURNS trigger LANGUAGE plpgsql AS $$"
                        + " BEGIN RAISE EXCEPTION 'recovery_receipt_query_journal_failure'; END; $$");
        raw.execute(
                "CREATE TRIGGER recovery_receipt_query_fail AFTER INSERT ON business.journal "
                        + "FOR EACH ROW WHEN (NEW.role='assistant') EXECUTE FUNCTION business.recovery_receipt_query_fail()");
        var failed = observe("recovery-journal-rollback", recovery::recoverExpired);
        var rolledBack = attachFacts(failed, scenario);
        assertThat(failed.failure()).isNotNull();
        assertReceiptReads(failed, baseline ? 2 : 1);
        assertAuthoritativeReads(failed);
        assertRecoveryTransaction(failed, true);
        assertThat(rolledBack).isEqualTo(before);
        assertThat(failed.sql().count("journal_insert")).isOne();
        assertThat(failed.sql().count("command_done_update")).isZero();
        dropFailureTrigger();
        var retried = observe("recovery-journal-retry", recovery::recoverExpired);
        var completed = attachFacts(retried, scenario);
        assertThat(retried.failure()).isNull();
        assertThat(retried.value()).isEqualTo(1);
        assertReceiptReads(retried, baseline ? 2 : 1);
        assertRecoveryTransaction(retried, false);
        assertThat(completed.get("turn_status")).isEqualTo("completed");
        assertThat(completed.get("assistant_count")).isEqualTo(1);
        assertThat(completed.get("attempt_count")).isEqualTo(1);
        assertThat(completed.get("receipt_outcome")).isEqualTo(before.get("receipt_outcome"));
        assertEmptyRepeat("recovery-journal-retry-repeat-empty", scenario);
    }

    @Test
    void twoRecoverersThatSawTheSameExpiredCommandCommitOnlyOneCompletion() throws Exception {
        var scenario = prepare("completed", true);
        expire(scenario);
        Observed<Integer> first;
        Observed<Integer> second;
        try (var holder = lockCommand(scenario);
                var workers = Executors.newFixedThreadPool(2)) {
            var start = new CountDownLatch(1);
            var one =
                    workers.submit(
                            () -> {
                                assertThat(start.await(5, TimeUnit.SECONDS)).isTrue();
                                return observe(
                                        "concurrent-recoverer-one", recovery::recoverExpired);
                            });
            var two =
                    workers.submit(
                            () -> {
                                assertThat(start.await(5, TimeUnit.SECONDS)).isTrue();
                                return observe(
                                        "concurrent-recoverer-two", recovery::recoverExpired);
                            });
            start.countDown();
            // 两个调用先完成无锁 expired scan，再同时阻塞在目标 command 的行锁上。
            // 独立 raw 连接只观察 PG 锁等待，不把观察轮询混入两份业务计数。
            try {
                awaitTwoCommandWaiters();
            } finally {
                holder.rollback();
            }
            first = one.get(10, TimeUnit.SECONDS);
            second = two.get(10, TimeUnit.SECONDS);
        }
        var facts = facts(scenario);
        first.evidence().put("business_facts", facts);
        second.evidence().put("business_facts", facts);
        assertThat(first.failure()).isNull();
        assertThat(second.failure()).isNull();
        assertThat(first.value() + second.value()).isEqualTo(1);
        var winner = first.value() == 1 ? first : second;
        var loser = first.value() == 0 ? first : second;
        assertReceiptReads(winner, baseline ? 2 : 1);
        assertAuthoritativeReads(winner);
        assertRecoveryTransaction(winner, false);
        assertReceiptReads(loser, 0);
        assertThat(loser.sql().count("command_expired_poll")).isOne();
        assertThat(loser.sql().count("command_lock")).isOne();
        assertThat(loser.sql().count("turn_read")).isZero();
        assertRecoveryTransaction(loser, false);
        assertThat(facts.get("turn_status")).isEqualTo("completed");
        assertThat(facts.get("assistant_count")).isEqualTo(1);
        assertThat(facts.get("attempt_count")).isEqualTo(1);
    }

    @Test
    void normalLiveFinishKeepsItsOriginalSingleReceiptReadAndTransaction() {
        var scenario = prepare("completed", true);
        var finished =
                observe(
                        "normal-live-finish",
                        () -> {
                            completion.finish(scenario.claim());
                            return null;
                        });
        var after = attachFacts(finished, scenario);
        assertThat(finished.failure()).isNull();
        assertReceiptReads(finished, 1);
        assertThat(finished.sql().count("command_expired_poll")).isZero();
        assertThat(finished.sql().commits()).isEqualTo(1);
        assertThat(finished.sql().rollbacks()).isZero();
        assertThat(
                        finished.sql().executions().stream()
                                .map(JdbcExecutionCounter.SqlExecution::connection)
                                .distinct())
                .hasSize(1);
        assertThat(after.get("turn_status")).isEqualTo("completed");
        assertThat(after.get("assistant_count")).isEqualTo(1);
        assertThat(after.get("attempt_count")).isEqualTo(1);
    }

    /**
     * 在 before 证据封存后新增的防护验收。旧实现没有显式 commandId 核验，不能把这两项描述为 before 已经拒绝；未来显式重跑 baseline 模式时也跳过该新增能力。
     */
    @ParameterizedTest
    @ValueSource(strings = {"normal-finish", "expired-recovery"})
    @DisabledIfSystemProperty(named = "financeclaw.test.query-baseline", matches = "true")
    void afterOnlyWrongReceiptCommandBindingCannotCompleteEitherCommand(String entrypoint) {
        var scenario = prepare("completed", true);
        String otherConversation = tasks.createConversation(OWNER);
        var other =
                tasks.submit(
                        OWNER,
                        otherConversation,
                        UUID.randomUUID().toString(),
                        "Another legitimate queued command.");
        excludedObservations++;
        String otherCommand =
                raw.queryForObject(
                        "SELECT id FROM business.commands WHERE turn_id=?",
                        String.class,
                        other.turnId());
        assertThat(otherCommand).isNotEqualTo(commandId(scenario));
        var queued = otherCommandFacts(other.turnId());
        assertThat(queued.get("turn_status")).isEqualTo("queued");
        assertThat(queued.get("command_status")).isEqualTo("queued");
        assertThat(queued.get("attempt_bound")).isEqualTo(false);
        assertThat(queued.get("attempt_count")).isEqualTo(0);
        // 只故意破坏 attempt 的 command 关联。另一 command 从未领取，因此 (command_id,fence)
        // 唯一约束不会因碰撞拒绝夹具；所有外键/唯一约束继续启用。
        assertThat(
                        raw.update(
                                "UPDATE business.attempts SET command_id=? WHERE id=? AND command_id=?",
                                otherCommand,
                                attemptId(scenario),
                                commandId(scenario)))
                .isOne();
        excludedObservations++;
        var binding =
                raw.queryForMap(
                        "SELECT id,command_id,fence,outcome,state_version FROM business.attempts WHERE id=?",
                        attemptId(scenario));
        assertThat(binding.get("id")).isEqualTo(attemptId(scenario));
        assertThat(binding.get("command_id"))
                .isEqualTo(otherCommand)
                .isNotEqualTo(commandId(scenario));
        assertThat(binding.get("fence")).isEqualTo(scenario.claim().command().fence());
        assertThat(binding.get("outcome")).isEqualTo("completed");
        assertThat(binding.get("state_version")).isEqualTo(scenario.outcome().stateVersion());
        if (entrypoint.equals("expired-recovery")) expire(scenario);
        var before = facts(scenario);
        var otherBefore = otherCommandFacts(other.turnId());
        var rejected =
                observe(
                        "after-only-wrong-command-" + entrypoint,
                        () -> {
                            if (entrypoint.equals("expired-recovery"))
                                return recovery.recoverExpired();
                            completion.finish(scenario.claim());
                            return null;
                        });
        var after = attachFacts(rejected, scenario);
        var otherAfter = otherCommandFacts(other.turnId());
        rejected.evidence().put("after_only", true);
        rejected.evidence().put("receipt_binding_mismatch_present", true);
        rejected.evidence().put("business_facts_before", before);
        rejected.evidence().put("other_command_facts_before", otherBefore);
        rejected.evidence().put("other_command_facts_after", otherAfter);
        assertThat(rejected.failure())
                .isInstanceOf(IllegalStateException.class)
                .hasMessage("Missing sealed execution receipt");
        assertReceiptReads(rejected, 1);
        for (String family :
                List.of(
                        "sdk_state_read",
                        "release_lookup",
                        "owner_lock",
                        "source_read",
                        "policy_read",
                        "grant_lock",
                        "turn_state_update",
                        "event_insert",
                        "journal_insert",
                        "command_done_update")) {
            assertThat(rejected.sql().count(family))
                    .as("binding guard precedes %s", family)
                    .isZero();
        }
        assertThat(rejected.sql().commits()).isZero();
        assertThat(rejected.sql().rollbacks()).isEqualTo(1);
        if (entrypoint.equals("expired-recovery")) {
            assertRecoveryTransaction(rejected, true);
        } else {
            assertThat(rejected.sql().count("command_expired_poll")).isZero();
            assertThat(
                            rejected.sql().executions().stream()
                                    .map(JdbcExecutionCounter.SqlExecution::connection)
                                    .distinct())
                    .hasSize(1);
        }
        assertThat(after).isEqualTo(before);
        assertThat(otherAfter).isEqualTo(otherBefore);
        assertThat(after.get("turn_status")).isEqualTo("running");
        assertThat(after.get("assistant_count")).isEqualTo(0);
        assertThat(otherAfter.get("turn_status")).isEqualTo("queued");
        assertThat(otherAfter.get("command_status")).isEqualTo("queued");
        assertThat(otherAfter.get("attempt_bound")).isEqualTo(false);
        assertThat(otherAfter.get("attempt_count")).isEqualTo(1);
    }

    private Map<String, Object> otherCommandFacts(String turn) {
        excludedObservations++;
        return raw.queryForMap(
                "SELECT t.status AS turn_status,t.revision AS turn_revision,"
                        + "c.status AS command_status,c.fence AS command_fence,"
                        + "c.attempt_id IS NOT NULL AS attempt_bound,"
                        + "(SELECT count(*)::int FROM business.attempts a WHERE a.command_id=c.id) AS attempt_count "
                        + "FROM business.turns t JOIN business.commands c ON c.turn_id=t.id WHERE t.id=?",
                turn);
    }

    /** 正常执行只停在 seal 之后，模拟“已封存、尚未提交业务终态”的恢复边界，不伪造回执。 */
    private Scenario prepare(String desired, boolean seal) {
        String conversation = tasks.createConversation(OWNER);
        var accepted =
                tasks.submit(
                        OWNER,
                        conversation,
                        UUID.randomUUID().toString(),
                        desired.equals("waiting") ? "city" : "Recovery receipt fixture.");
        var claim = tasks.claim("receipt-query-worker", 60000);
        assertThat(claim).isNotNull();
        assertThat(claim.turn().id()).isEqualTo(accepted.turnId());
        Outcome outcome = null;
        if (!desired.equals("no-sdk")) {
            var selected =
                    desired.equals("waiting")
                            ? new ScriptedRuntime(
                                    tasks,
                                    application.getBean(GovernanceFixture.class),
                                    states,
                                    application.getBean(BusinessCapabilities.class))
                            : runtime;
            outcome = selected.execute(claim);
            assertThat(outcome.status()).isEqualTo(desired);
            assertThat(outcome.stateVersion()).isPositive();
            if (seal) tasks.seal(claim, outcome);
        }
        var scenario = new Scenario(claim, outcome);
        var facts = facts(scenario);
        assertThat(facts.get("turn_status")).isEqualTo("running");
        assertThat(facts.get("command_status")).isEqualTo("running");
        assertThat(facts.get("assistant_count")).isEqualTo(0);
        assertThat(facts.get("receipt_outcome")).isEqualTo(seal ? desired : null);
        return scenario;
    }

    private void expire(Scenario scenario) {
        assertThat(
                        raw.update(
                                "UPDATE business.commands SET lease_until=clock_timestamp()-interval '1 second' "
                                        + "WHERE id=? AND attempt_id=? AND status='running'",
                                commandId(scenario),
                                attemptId(scenario)))
                .isOne();
    }

    private RuntimeMapper.StateKey stateKey(Scenario scenario) {
        var scope = scenario.claim().scope();
        return new RuntimeMapper.StateKey(
                scope.namespace(), scope.userId(), scope.sessionId(), "agent_state");
    }

    private Map<String, Object> attachFacts(Observed<?> observation, Scenario scenario) {
        var facts = facts(scenario);
        observation.evidence().put("business_facts", facts);
        return facts;
    }

    /** 导出安全事实而非 owner、问题、结果正文、SDK payload 或数据库凭据。 */
    private Map<String, Object> facts(Scenario scenario) {
        excludedObservations++;
        var key = stateKey(scenario);
        var row =
                raw.queryForMap(
                        "SELECT t.status AS turn_status,t.reason AS turn_reason,t.revision AS turn_revision,"
                                + "t.result,c.status AS command_status,c.fence AS command_fence,"
                                + "a.outcome AS receipt_outcome,a.state_version AS receipt_state_version,"
                                + "s.version AS sdk_version,s.deleted AS sdk_deleted,p.model_calls,p.tool_calls,"
                                + "(SELECT count(*)::int FROM business.attempts x WHERE x.command_id=c.id) AS attempt_count,"
                                + "(SELECT count(*)::int FROM business.events e WHERE e.turn_id=t.id) AS event_count,"
                                + "(SELECT count(*)::int FROM business.journal j WHERE j.turn_id=t.id AND j.role='assistant') AS assistant_count,"
                                + "(SELECT count(*)::int FROM business.interactions i WHERE i.turn_id=t.id AND i.status='pending') AS pending_interactions "
                                + "FROM business.turns t JOIN business.commands c ON c.turn_id=t.id "
                                + "JOIN business.turn_policies p ON p.turn_id=t.id LEFT JOIN business.attempts a ON a.id=c.attempt_id "
                                + "LEFT JOIN agent_runtime.states s ON s.namespace=? AND s.user_id=? AND s.session_id=? AND s.state_key=? "
                                + "WHERE t.id=? AND t.tenant=? AND t.subject=?",
                        key.namespace(),
                        key.user(),
                        key.session(),
                        key.key(),
                        turnId(scenario),
                        OWNER.tenant(),
                        OWNER.subject());
        var facts = new LinkedHashMap<>(row);
        Object result = facts.remove("result");
        facts.put(
                "turn_result_sha256",
                result == null ? null : PayloadDigest.sha256(result.toString()));
        return facts;
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
        var row = new LinkedHashMap<String, Object>();
        row.put("phase", phase);
        row.put("sql", measured);
        row.put("completed_without_exception", failure == null);
        if (value instanceof Integer changed) row.put("recovered", changed);
        if (failure != null) row.put("failure_type", failure.getClass().getSimpleName());
        measurements.add(row);
        return new Observed<>(value, failure, measured, row);
    }

    private void assertReceiptReads(Observed<?> observation, long expected) {
        assertThat(observation.sql().count("receipt_read"))
                .as(observation.sql().phase())
                .isEqualTo(expected);
        assertThat(
                        observation.sql().executions().stream()
                                .filter(e -> e.templateSha256().equals(receiptTemplate))
                                .count())
                .isEqualTo(expected);
    }

    private static void assertAuthoritativeReads(Observed<?> observation) {
        for (String family :
                List.of(
                        "sdk_state_read",
                        "release_lookup",
                        "owner_lock",
                        "source_read",
                        "policy_read",
                        "grant_lock")) {
            assertThat(observation.sql().count(family))
                    .as("%s retains %s", observation.sql().phase(), family)
                    .isOne();
        }
    }

    private static void assertRecoveryTransaction(Observed<?> observation, boolean rolledBack) {
        var sql = observation.sql();
        assertThat(sql.count("command_expired_poll")).isOne();
        assertThat(sql.count("command_lock")).isOne();
        assertThat(sql.commits()).isEqualTo(rolledBack ? 0 : 1);
        assertThat(sql.rollbacks()).isEqualTo(rolledBack ? 1 : 0);
        assertThat(sql.executions()).isNotEmpty();
        var first = sql.executions().getFirst();
        assertThat(first.family()).isEqualTo("command_expired_poll");
        var transaction = sql.executions().subList(1, sql.executions().size());
        assertThat(
                        transaction.stream()
                                .map(JdbcExecutionCounter.SqlExecution::connection)
                                .distinct())
                .hasSize(1);
        assertThat(transaction.getFirst().connection()).isNotEqualTo(first.connection());
    }

    private void assertEmptyRepeat(String phase, Scenario scenario) {
        var repeat = observe(phase, recovery::recoverExpired);
        attachFacts(repeat, scenario);
        assertThat(repeat.failure()).isNull();
        assertThat(repeat.value()).isZero();
        assertThat(repeat.sql().statements()).isOne();
        assertThat(repeat.sql().count("command_expired_poll")).isOne();
        assertReceiptReads(repeat, 0);
        assertThat(repeat.sql().commits()).isZero();
        assertThat(repeat.sql().rollbacks()).isZero();
    }

    private Connection lockCommand(Scenario scenario) throws Exception {
        var connection = original.getConnection();
        connection.setAutoCommit(false);
        try (var statement =
                connection.prepareStatement(
                        "SELECT id FROM business.commands WHERE id=? FOR UPDATE")) {
            statement.setString(1, commandId(scenario));
            statement.executeQuery().close();
        } catch (Exception failure) {
            connection.close();
            throw failure;
        }
        return connection;
    }

    private void awaitTwoCommandWaiters() throws InterruptedException {
        long deadline = System.nanoTime() + Duration.ofSeconds(2).toNanos();
        int waiting;
        do {
            waiting =
                    integer(
                            "SELECT count(*) FROM pg_stat_activity WHERE datname=current_database() "
                                    + "AND pid<>pg_backend_pid() AND wait_event_type='Lock'");
            if (waiting >= 2) return;
            Thread.sleep(10);
        } while (System.nanoTime() < deadline);
        throw new AssertionError(
                "Both recovery calls must reach the held command lock; observed=" + waiting);
    }

    private int integer(String query, Object... parameters) {
        excludedObservations++;
        return raw.queryForObject(query, Integer.class, parameters);
    }

    private static String turnId(Scenario scenario) {
        return scenario.claim().turn().id();
    }

    private static String commandId(Scenario scenario) {
        return scenario.claim().command().id();
    }

    private static String attemptId(Scenario scenario) {
        return scenario.claim().command().attemptId();
    }

    private void dropFailureTrigger() {
        if (raw != null)
            raw.execute("DROP FUNCTION IF EXISTS business.recovery_receipt_query_fail() CASCADE");
    }

    private void closeResources() {
        try {
            if (application != null) application.close();
        } finally {
            postgres.stop();
        }
    }
}
