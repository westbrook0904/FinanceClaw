package com.financeclaw.p3;

import static org.assertj.core.api.Assertions.*;
import static org.awaitility.Awaitility.await;

import com.financeclaw.app.execution.CapabilityStepService;
import com.financeclaw.app.execution.ChildInvocationService;
import com.financeclaw.app.execution.ChildInvocationService.Invocation;
import com.financeclaw.app.execution.port.ChildInvocationGateway;
import com.financeclaw.app.market.MarketDataService;
import com.financeclaw.app.memory.MemoryService;
import com.financeclaw.app.memory.port.MemoryRepository;
import com.financeclaw.app.release.ReleaseService;
import com.financeclaw.app.research.MarketResearchService;
import com.financeclaw.bootstrap.FinanceClawApplication;
import com.financeclaw.domain.Governance.Blocked;
import com.financeclaw.domain.Task.*;
import com.financeclaw.domain.release.BusinessFeatures;
import com.financeclaw.domain.release.ReleaseDefinition;
import com.financeclaw.fixture.GovernanceFixture;
import com.financeclaw.fixture.TaskFixture;
import com.financeclaw.infrastructure.agentscope.AgentStateInspector;
import com.financeclaw.infrastructure.agentscope.NativeToolCalls;
import com.financeclaw.infrastructure.agentscope.capability.ChildResearchStateStore;
import com.financeclaw.infrastructure.agentscope.capability.MarketResearchAgent;
import com.financeclaw.infrastructure.agentscope.skills.PackagedSkills;
import com.financeclaw.infrastructure.state.RuntimeMapper;
import com.financeclaw.support.PayloadJson;
import io.agentscope.core.message.Msg;
import io.agentscope.core.message.MsgRole;
import io.agentscope.core.message.TextBlock;
import io.agentscope.core.model.ChatResponse;
import io.agentscope.core.model.GenerateOptions;
import io.agentscope.core.model.Model;
import io.agentscope.core.model.ToolSchema;
import io.agentscope.core.state.AgentState;
import io.agentscope.core.tool.ToolCallParam;
import java.time.Duration;
import java.util.List;
import java.util.Map;
import java.util.UUID;
import java.util.concurrent.*;
import java.util.concurrent.atomic.AtomicInteger;
import javax.sql.DataSource;
import org.junit.jupiter.api.*;
import org.junit.jupiter.params.ParameterizedTest;
import org.junit.jupiter.params.provider.ValueSource;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.boot.test.context.SpringBootTest;
import org.springframework.context.annotation.Import;
import org.springframework.dao.DataAccessException;
import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.test.context.DynamicPropertyRegistry;
import org.springframework.test.context.DynamicPropertySource;
import org.springframework.transaction.support.TransactionTemplate;
import org.testcontainers.containers.PostgreSQLContainer;
import reactor.core.publisher.Flux;

/** 真实父租约、授权、隐私及本地行情验证子调用回执与持久预算，不创建独立子任务队列。 */
@Import({TaskFixture.class, GovernanceFixture.class})
@SpringBootTest(
        classes = FinanceClawApplication.class,
        properties = {
            "financeclaw.worker.enabled=false",
            "financeclaw.integrations.enabled=false",
            "financeclaw.memory.enabled=false",
            "financeclaw.memory.profile-enabled=true",
            "financeclaw.memory.profile-directory=",
            "financeclaw.memory.forget-ledger.enabled=false"
        })
class ChildInvocationIT {
    private static final PostgreSQLContainer<?> PG =
            new PostgreSQLContainer<>("postgres:16.10-alpine");
    private static final Owner ALICE = new Owner("local", "alice");
    private static final Owner BOB = new Owner("local", "bob");
    private static final Map<String, Object> INPUT =
            Map.of("symbols", List.of("AAPL"), "analysis_period", "2026-10");
    private static final Map<String, Object> SNAPSHOT_INPUT = Map.of("symbol", "AAPL");
    private static final String CALL = "research-call";

    static {
        PG.start();
    }

    @DynamicPropertySource
    static void database(DynamicPropertyRegistry registry) {
        registry.add("spring.datasource.url", PG::getJdbcUrl);
        registry.add("spring.datasource.username", PG::getUsername);
        registry.add("spring.datasource.password", PG::getPassword);
    }

    @Autowired TaskFixture tasks;
    @Autowired GovernanceFixture governance;
    @Autowired ReleaseService releases;
    @Autowired ChildInvocationService children;
    @Autowired ChildInvocationGateway calls;
    @Autowired CapabilityStepService steps;
    @Autowired MarketDataService market;
    @Autowired MarketResearchService research;
    @Autowired MemoryService memory;
    @Autowired MemoryRepository memories;
    @Autowired RuntimeMapper states;
    @Autowired TransactionTemplate tx;
    @Autowired DataSource source;
    private JdbcTemplate sql;

    @AfterAll
    static void stopDatabase() {
        PG.stop();
    }

    @BeforeEach
    void reset() {
        sql = new JdbcTemplate(source);
        sql.execute("TRUNCATE business.conversations CASCADE");
        sql.execute("TRUNCATE memory.owners,memory.jobs,memory.operations,memory.audit CASCADE");
        sql.execute("TRUNCATE agent_runtime.states");
        publish(32, 64);
    }

    @Test
    void modelBudgetSurvivesNewServiceAndAValidNewParentAttempt() {
        var parent = claim(ALICE);
        var invocation = children.begin(parent, CALL, INPUT);
        children.reserveModel(invocation);
        children.reserveModel(invocation);
        var rebuilt = rebuilt();
        rebuilt.reserveModel(rebuilt.begin(parent, CALL, INPUT));

        var nextParent = nextAttempt(parent);
        assertThat(nextParent.command().attemptId()).isNotEqualTo(parent.command().attemptId());
        assertThat(nextParent.command().fence()).isGreaterThan(parent.command().fence());
        var resumed = rebuilt.begin(nextParent, CALL, INPUT);
        rebuilt.reserveModel(resumed);
        assertThatThrownBy(() -> rebuilt.reserveModel(resumed))
                .hasMessage("child_model_budget_exceeded");
        assertThat(childCount(parent, CALL, "model_calls")).isEqualTo(4);
        assertThat(rootCount(parent, "model_calls")).isEqualTo(4);
        assertThatThrownBy(() -> children.reserveModel(invocation)).isInstanceOf(Fenced.class);
    }

    @Test
    void exhaustedRootBudgetsRollBackTheChildReservationsAndLocalResult() {
        publish(1, 1);
        var parent = claim(ALICE);
        var invocation = children.begin(parent, CALL, INPUT);
        children.reserveModel(invocation);
        assertThatThrownBy(() -> children.reserveModel(invocation))
                .hasMessage("execution_budget_exhausted");
        assertThat(childCount(parent, CALL, "model_calls")).isEqualTo(1);
        assertThat(rootCount(parent, "model_calls")).isEqualTo(1);

        children.snapshot(invocation, "first", SNAPSHOT_INPUT, () -> market.read(parent, "AAPL"));
        assertThatThrownBy(
                        () ->
                                children.snapshot(
                                        invocation,
                                        "second",
                                        SNAPSHOT_INPUT,
                                        () -> market.read(parent, "AAPL")))
                .hasMessage("execution_budget_exhausted");
        assertThat(childCount(parent, CALL, "tool_calls")).isEqualTo(1);
        assertThat(rootCount(parent, "tool_calls")).isEqualTo(1);
        assertThat(stepCount(parent, CALL, "tool:second")).isZero();
    }

    @Test
    void snapshotReplayRestoresEvidenceAndOnlyTenFirstReadsCanConsumeChildSlots() {
        var parent = claim(ALICE);
        var execution = research.begin(parent, CALL, INPUT);
        var snapshot = research.snapshot(execution, "snapshot", SNAPSHOT_INPUT);
        var resumed = research.begin(parent, CALL, INPUT);
        assertThat(research.snapshot(resumed, "snapshot", SNAPSHOT_INPUT)).isEqualTo(snapshot);
        var completed = (Map<?, ?>) research.complete(resumed, true, "Verified frozen evidence");
        assertThat(completed.get("evidence")).isEqualTo(List.of(snapshot));
        assertThat(childCount(parent, CALL, "tool_calls")).isEqualTo(1);
        assertThat(rootCount(parent, "tool_calls")).isEqualTo(1);

        var rebuilt = rebuilt();
        var bounded = rebuilt.begin(parent, "bounded", INPUT);
        var actualReads = new AtomicInteger();
        for (int i = 0; i < 10; i++) {
            rebuilt.snapshot(
                    bounded,
                    "snapshot-" + i,
                    SNAPSHOT_INPUT,
                    () -> {
                        actualReads.incrementAndGet();
                        return market.read(parent, "AAPL");
                    });
        }
        var next = rebuilt().begin(parent, "bounded", INPUT);
        assertThatThrownBy(
                        () ->
                                children.snapshot(
                                        next,
                                        "snapshot-10",
                                        SNAPSHOT_INPUT,
                                        () -> {
                                            actualReads.incrementAndGet();
                                            return market.read(parent, "AAPL");
                                        }))
                .hasMessage("child_tool_budget_exceeded");
        children.snapshot(
                next,
                "snapshot-0",
                SNAPSHOT_INPUT,
                () -> {
                    throw new AssertionError("A cached snapshot must not execute its supplier");
                });
        assertThat(actualReads).hasValue(10);
        assertThat(childCount(parent, "bounded", "tool_calls")).isEqualTo(10);
        assertThat(rootCount(parent, "tool_calls")).isEqualTo(11);
    }

    @Test
    void completedNativeCheckpointFinishesPendingReceiptWithoutASecondModelRequest() {
        var parent = claim(ALICE);
        var execution = research.begin(parent, CALL, INPUT);
        var snapshot = research.snapshot(execution, "persisted-snapshot", SNAPSHOT_INPUT);
        for (int i = 0; i < 4; i++) {
            research.modelCall(execution);
        }
        var scope = execution.run().scope();
        var finalState =
                AgentState.builder()
                        .userId(scope.userId())
                        .sessionId(scope.sessionId())
                        .addMessage(
                                Msg.builder()
                                        .role(MsgRole.ASSISTANT)
                                        .content(
                                                TextBlock.builder()
                                                        .text("Completed frozen research")
                                                        .build())
                                        .build())
                        .build();
        var store = new ChildResearchStateStore(states, scope, research, execution);
        assertThat(
                        store.saveIfVersion(
                                scope.userId(), scope.sessionId(), "agent_state", finalState, 0))
                .isEqualTo(1);
        assertThat(AgentStateInspector.classify(finalState, 1).status()).isEqualTo("completed");
        assertThat(status(parent, CALL)).isEqualTo("pending");
        assertThat(stepCount(parent, CALL, "result")).isZero();

        // 模拟 SDK 最终检查点已提交、业务 result 尚未提交就丢失进程内 ResearchRun 的窗口。
        var modelRequests = new AtomicInteger();
        Model forbiddenModel =
                new Model() {
                    @Override
                    public String getModelName() {
                        return "must-not-call-after-native-completion";
                    }

                    @Override
                    public Flux<ChatResponse> stream(
                            List<Msg> messages, List<ToolSchema> schemas, GenerateOptions options) {
                        modelRequests.incrementAndGet();
                        throw new AssertionError(
                                "A completed checkpoint must not call the model again");
                    }
                };
        var restoredResearch = new MarketResearchService(tasks.policy(), rebuilt(), market);
        var result =
                new MarketResearchAgent(restoredResearch, states, parent, forbiddenModel)
                        .run(
                                ToolCallParam.builder()
                                        .toolUseBlock(
                                                NativeToolCalls.call(
                                                        CALL, "market_research", INPUT))
                                        .input(INPUT)
                                        .build())
                        .block(Duration.ofSeconds(10));
        assertThat(result).isNotNull();
        var payload = PayloadJson.object(((TextBlock) result.getOutput().getFirst()).getText());
        assertThat(payload.get("outcome")).isEqualTo("success");
        assertThat(payload.get("summary")).isEqualTo("Completed frozen research");
        assertThat(payload.get("evidence")).isEqualTo(List.of(snapshot));
        assertThat(modelRequests).hasValue(0);
        assertThat(status(parent, CALL)).isEqualTo("completed");
        assertThat(stepCount(parent, CALL, "result")).isEqualTo(1);
        assertThat(childCount(parent, CALL, "model_calls")).isEqualTo(4);
        assertThat(rootCount(parent, "model_calls")).isEqualTo(4);
        assertThat(childCount(parent, CALL, "tool_calls")).isEqualTo(1);
        assertThat(rootCount(parent, "tool_calls")).isEqualTo(1);
    }

    @Test
    void completionAndResultAreAtomicAndInconsistentCompletedReceiptsFailClosed() {
        var parent = claim(ALICE);
        var invocation = children.begin(parent, CALL, INPUT);
        sql.execute(
                """
                CREATE FUNCTION public.reject_child_completion() RETURNS trigger LANGUAGE plpgsql AS $$
                BEGIN
                  IF NEW.status='completed' THEN
                    RAISE EXCEPTION 'child completion fault';
                  END IF;
                  RETURN NEW;
                END;
                $$
                """);
        sql.execute(
                "CREATE TRIGGER reject_child_completion BEFORE UPDATE ON business.child_invocations"
                        + " FOR EACH ROW EXECUTE FUNCTION public.reject_child_completion()");
        try {
            assertThatThrownBy(
                            () -> children.complete(invocation, () -> Map.of("summary", "first")))
                    .isInstanceOf(DataAccessException.class);
            assertThat(status(parent, CALL)).isEqualTo("pending");
            assertThat(stepCount(parent, CALL, "result")).isZero();
        } finally {
            sql.execute("DROP TRIGGER reject_child_completion ON business.child_invocations");
            sql.execute("DROP FUNCTION public.reject_child_completion()");
        }
        var result = children.complete(invocation, () -> Map.of("summary", "first"));
        assertThat(status(parent, CALL)).isEqualTo("completed");
        assertThat(PayloadJson.parse(rebuilt().begin(parent, CALL, INPUT).cachedResult()))
                .isEqualTo(result);
        assertThatThrownBy(
                        () ->
                                children.begin(
                                        parent,
                                        CALL,
                                        Map.of(
                                                "symbols",
                                                List.of("AAPL"),
                                                "analysis_period",
                                                "changed")))
                .hasMessage("child_input_conflict");
        sql.update(
                "UPDATE business.capability_steps SET input_hash='wrong-input'"
                        + " WHERE turn_id=? AND namespace=? AND step='result'",
                parent.turn().id(),
                invocation.namespace());
        assertThatThrownBy(() -> children.begin(parent, CALL, INPUT))
                .hasMessage("child_invocation_invalid");
        sql.update(
                "DELETE FROM business.capability_steps WHERE turn_id=? AND namespace=? AND step='result'",
                parent.turn().id(),
                invocation.namespace());
        assertThatThrownBy(() -> children.begin(parent, CALL, INPUT))
                .hasMessage("child_invocation_invalid");
    }

    @Test
    void identicalCallIdsAcrossOwnersAndRootTurnsHaveIndependentBindingsAndResults() {
        var first = claim(ALICE);
        var other = claim(BOB);
        var another = claim(ALICE);
        var firstInput = input("alice first");
        var otherInput = input("bob");
        var anotherInput = input("alice second");
        children.complete(
                children.begin(first, CALL, firstInput), () -> Map.of("summary", "alice first"));
        children.complete(children.begin(other, CALL, otherInput), () -> Map.of("summary", "bob"));
        children.complete(
                children.begin(another, CALL, anotherInput),
                () -> Map.of("summary", "alice second"));
        assertThat(children.begin(first, CALL, firstInput).cachedResult()).contains("alice first");
        assertThat(children.begin(other, CALL, otherInput).cachedResult()).contains("bob");
        assertThat(children.begin(another, CALL, anotherInput).cachedResult())
                .contains("alice second");
        assertThat(
                        sql.queryForObject(
                                "SELECT count(*) FROM business.child_invocations", Integer.class))
                .isEqualTo(3);
        var foreignHash = children.begin(first, CALL, firstInput).inputHash();
        assertThatThrownBy(
                        () ->
                                children.checkpoint(
                                        new Invocation(other, CALL, foreignHash, null, List.of()),
                                        () -> "must not reach a foreign checkpoint"))
                .hasMessage("child_input_conflict");
    }

    @ParameterizedTest
    @ValueSource(strings = {"cancel", "lease", "grant", "forget"})
    void currentRootGatesProtectCachedResultsAndCheckpointReadsAndWrites(String change) {
        var parent = claim(ALICE);
        var execution = research.begin(parent, "checkpoint-call", INPUT);
        var scope = execution.run().scope();
        var store = new ChildResearchStateStore(states, scope, research, execution);
        assertThat(
                        store.saveIfVersion(
                                scope.userId(),
                                scope.sessionId(),
                                "agent_state",
                                AgentState.builder().build(),
                                0))
                .isEqualTo(1);
        assertThat(
                        store.getVersioned(
                                        scope.userId(),
                                        scope.sessionId(),
                                        "agent_state",
                                        AgentState.class)
                                .version())
                .isEqualTo(1);
        var invocation = children.begin(parent, CALL, INPUT);
        children.complete(invocation, () -> Map.of("summary", "cached"));
        switch (change) {
            case "cancel" ->
                    tasks.cancel(
                            ALICE,
                            parent.turn().conversationId(),
                            parent.turn().id(),
                            "cancel-child");
            case "lease" ->
                    sql.update(
                            "UPDATE business.commands SET lease_until=clock_timestamp()-interval '1 second' WHERE id=?",
                            parent.command().id());
            case "grant" ->
                    governance.changeGrant(
                            ALICE,
                            parent.turn().conversationId(),
                            "revoke-child",
                            1,
                            List.of("market:read"));
            case "forget" -> assertThat(memory.forget(ALICE, 1, "forget-child")).isEqualTo(2);
            default -> throw new AssertionError("Unknown gate scenario");
        }
        assertThatThrownBy(() -> rebuilt().begin(parent, CALL, INPUT))
                .isInstanceOfAny(Fenced.class, Blocked.class);
        assertThatThrownBy(
                        () ->
                                store.getVersioned(
                                        scope.userId(),
                                        scope.sessionId(),
                                        "agent_state",
                                        AgentState.class))
                .isInstanceOfAny(Fenced.class, Blocked.class);
        assertThatThrownBy(
                        () ->
                                store.saveIfVersion(
                                        scope.userId(),
                                        scope.sessionId(),
                                        "agent_state",
                                        AgentState.builder().build(),
                                        1))
                .isInstanceOfAny(Fenced.class, Blocked.class);
        assertThatThrownBy(() -> store.exists(scope.userId(), scope.sessionId()))
                .isInstanceOfAny(Fenced.class, Blocked.class);
        assertThatThrownBy(
                        () ->
                                store.getList(
                                        scope.userId(),
                                        scope.sessionId(),
                                        "agent_state",
                                        AgentState.class))
                .isInstanceOfAny(Fenced.class, Blocked.class);
        assertThatThrownBy(() -> store.delete(scope.userId(), scope.sessionId(), "agent_state"))
                .isInstanceOfAny(Fenced.class, Blocked.class);
        // 遗忘已擦除物理正文并留下递增版本墓碑；其他门禁不删除检查点，迟到写入都不能成功。
        var row =
                states.getState(
                        new RuntimeMapper.StateKey(
                                scope.namespace(),
                                scope.userId(),
                                scope.sessionId(),
                                "agent_state"));
        assertThat(row.version()).isEqualTo(change.equals("forget") ? 2 : 1);
        assertThat(row.deleted()).isEqualTo(change.equals("forget"));
        if (change.equals("forget")) assertThat(row.payload()).isNull();
    }

    @Test
    void forgetCommittedWhileCheckpointWaitsRejectsTheLateCasWrite() throws Exception {
        var parent = claim(ALICE);
        var execution = research.begin(parent, CALL, INPUT);
        var scope = execution.run().scope();
        var store = new ChildResearchStateStore(states, scope, research, execution);
        store.saveIfVersion(
                scope.userId(), scope.sessionId(), "agent_state", AgentState.builder().build(), 0);
        try (var executor = Executors.newSingleThreadExecutor()) {
            var lateWrite =
                    tx.execute(
                            status -> {
                                memories.lock(ALICE);
                                var waiting =
                                        executor.submit(
                                                () ->
                                                        catchThrowable(
                                                                () ->
                                                                        store.saveIfVersion(
                                                                                scope.userId(),
                                                                                scope.sessionId(),
                                                                                "agent_state",
                                                                                AgentState.builder()
                                                                                        .build(),
                                                                                1)));
                                await().atMost(Duration.ofSeconds(10))
                                        .pollInterval(Duration.ofMillis(25))
                                        .untilAsserted(
                                                () ->
                                                        assertThat(
                                                                        sql.queryForObject(
                                                                                "SELECT count(*) FROM pg_stat_activity"
                                                                                        + " WHERE datname=current_database()"
                                                                                        + " AND wait_event_type='Lock'"
                                                                                        + " AND query LIKE '%memory.owners%'"
                                                                                        + " AND query LIKE '%FOR UPDATE%'",
                                                                                Integer.class))
                                                                .isGreaterThanOrEqualTo(1));
                                // 使用同一连接提交真实本地遗忘；解除 owner 锁后，等待者必须读取新 epoch。
                                assertThat(memory.forget(ALICE, 1, "forget-before-cas"))
                                        .isEqualTo(2);
                                return waiting;
                            });
            assertThat(lateWrite.get(10, TimeUnit.SECONDS))
                    .isInstanceOf(Blocked.class)
                    .hasMessage("memory_privacy_changed");
        }
        assertThat(
                        states.getState(
                                        new RuntimeMapper.StateKey(
                                                scope.namespace(),
                                                scope.userId(),
                                                scope.sessionId(),
                                                "agent_state"))
                                .version())
                .isEqualTo(2);
        assertThat(memory.privacy(ALICE).epoch()).isEqualTo(2);
    }

    private ChildInvocationService rebuilt() {
        return new ChildInvocationService(calls, steps, tasks.policy());
    }

    private Claim claim(Owner owner) {
        var accepted =
                tasks.submit(
                        owner,
                        tasks.createConversation(owner),
                        UUID.randomUUID().toString(),
                        "bounded child request");
        var claim = tasks.claim("p3-test", 120000);
        assertThat(claim.turn().id()).isEqualTo(accepted.turnId());
        return claim;
    }

    private Claim nextAttempt(Claim previous) {
        // 夹具显式模拟已获准的新 attempt；生产过期恢复仍保持 unconfirmed_execution 阻断。
        tx.executeWithoutResult(
                status -> {
                    tasks.fenced(
                            previous,
                            () -> {
                                sql.update(
                                        "UPDATE business.commands SET status='queued',attempt_id=NULL,worker_id=NULL,lease_until=NULL WHERE id=?",
                                        previous.command().id());
                                sql.update(
                                        "UPDATE business.turns SET status='resuming' WHERE id=?",
                                        previous.turn().id());
                                return null;
                            });
                });
        var claim = tasks.claim("p3-replacement", 120000);
        assertThat(claim.turn().id()).isEqualTo(previous.turn().id());
        return claim;
    }

    private void publish(int models, int tools) {
        var snapshots =
                PayloadJson.canonical(
                        Map.of(
                                "AAPL",
                                Map.of(
                                        "price", "100.00",
                                        "currency", "USD",
                                        "provider", "p3-frozen-fixture",
                                        "as_of", "2026-10-23T00:00:00Z")));
        releases.publish(
                new ReleaseDefinition(
                        "p3-child-test",
                        "finance_agent",
                        ReleaseDefinition.SCOPES,
                        ReleaseDefinition.OFFLINE,
                        null,
                        models,
                        tools,
                        new BusinessFeatures(List.of(), PackagedSkills.hash(), snapshots, null)));
    }

    private static Map<String, Object> input(String period) {
        return Map.of("symbols", List.of("AAPL"), "analysis_period", period);
    }

    private int childCount(Claim parent, String call, String counter) {
        return sql.queryForObject(
                "SELECT "
                        + counter
                        + " FROM business.child_invocations WHERE root_turn_id=? AND call_id=?",
                Integer.class,
                parent.turn().id(),
                call);
    }

    private int rootCount(Claim parent, String counter) {
        return sql.queryForObject(
                "SELECT " + counter + " FROM business.turn_policies WHERE turn_id=?",
                Integer.class,
                parent.turn().id());
    }

    private int stepCount(Claim parent, String call, String step) {
        return sql.queryForObject(
                "SELECT count(*) FROM business.capability_steps WHERE turn_id=? AND namespace=? AND step=?",
                Integer.class,
                parent.turn().id(),
                "child:" + call,
                step);
    }

    private String status(Claim parent, String call) {
        return sql.queryForObject(
                "SELECT status FROM business.child_invocations WHERE root_turn_id=? AND call_id=?",
                String.class,
                parent.turn().id(),
                call);
    }
}
