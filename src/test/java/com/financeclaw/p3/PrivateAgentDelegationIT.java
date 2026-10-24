package com.financeclaw.p3;

import static org.assertj.core.api.Assertions.*;

import com.financeclaw.app.agent.AgentService;
import com.financeclaw.app.execution.CapabilityStepService;
import com.financeclaw.app.execution.ChildInvocationService;
import com.financeclaw.app.execution.port.ChildInvocationGateway;
import com.financeclaw.app.memory.MemoryService;
import com.financeclaw.app.memory.port.ForgetRecoveryRepository;
import com.financeclaw.app.release.ReleaseService;
import com.financeclaw.app.task.SubmitTaskService;
import com.financeclaw.bootstrap.FinanceClawApplication;
import com.financeclaw.domain.Governance.Blocked;
import com.financeclaw.domain.Task.*;
import com.financeclaw.domain.agent.AgentComposition;
import com.financeclaw.domain.agent.PrivateAgentTarget;
import com.financeclaw.domain.memory.ForgetFact;
import com.financeclaw.domain.release.BusinessFeatures;
import com.financeclaw.domain.release.ReleaseDefinition;
import com.financeclaw.fixture.GovernanceFixture;
import com.financeclaw.fixture.TaskFixture;
import com.financeclaw.infrastructure.agentscope.NativeToolCalls;
import com.financeclaw.infrastructure.agentscope.capability.PrivateAgentDelegation;
import com.financeclaw.infrastructure.agentscope.capability.PrivateAgentStateStore;
import com.financeclaw.infrastructure.agentscope.skills.PackagedSkills;
import com.financeclaw.infrastructure.state.MyBatisAgentStateStore;
import com.financeclaw.infrastructure.state.RuntimeMapper;
import com.financeclaw.support.PayloadJson;
import com.financeclaw.support.PrivateAgentContracts;
import io.agentscope.core.message.Msg;
import io.agentscope.core.message.MsgRole;
import io.agentscope.core.message.TextBlock;
import io.agentscope.core.state.AgentState;
import io.agentscope.core.tool.ToolCallParam;
import java.time.Duration;
import java.util.List;
import java.util.Map;
import java.util.UUID;
import java.util.concurrent.atomic.AtomicBoolean;
import javax.sql.DataSource;
import org.junit.jupiter.api.*;
import org.junit.jupiter.params.ParameterizedTest;
import org.junit.jupiter.params.provider.ValueSource;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.boot.test.context.SpringBootTest;
import org.springframework.context.annotation.Import;
import org.springframework.dao.DataIntegrityViolationException;
import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.test.context.DynamicPropertyRegistry;
import org.springframework.test.context.DynamicPropertySource;
import org.springframework.transaction.support.TransactionTemplate;
import org.testcontainers.containers.PostgreSQLContainer;

/** 私有叶子的冻结身份、根预算和隐私边界；一个真实数据库上下文，不创建新的子队列或进程。 */
@Import({TaskFixture.class, GovernanceFixture.class})
@SpringBootTest(
        classes = FinanceClawApplication.class,
        properties = {
            "financeclaw.worker.enabled=false", "financeclaw.integrations.enabled=false",
            "financeclaw.memory.enabled=false", "financeclaw.memory.profile-enabled=true",
            "financeclaw.memory.profile-directory=",
                    "financeclaw.memory.forget-ledger.enabled=false"
        })
class PrivateAgentDelegationIT {
    private static final PostgreSQLContainer<?> PG =
            new PostgreSQLContainer<>("postgres:16.10-alpine");
    private static final Owner ALICE = new Owner("private-agent-test", "alice");
    private static final String CALL = "private-call:stable";
    private static final Map<String, Object> INPUT =
            Map.of("message", "Only this explicit request.");

    static {
        PG.start();
    }

    @DynamicPropertySource
    static void database(DynamicPropertyRegistry registry) {
        registry.add("spring.datasource.url", PG::getJdbcUrl);
        registry.add("spring.datasource.username", PG::getUsername);
        registry.add("spring.datasource.password", PG::getPassword);
    }

    @Autowired AgentService agents;
    @Autowired ReleaseService releases;
    @Autowired SubmitTaskService submission;
    @Autowired TaskFixture tasks;
    @Autowired GovernanceFixture governance;
    @Autowired ChildInvocationService children;
    @Autowired PrivateAgentDelegation runtime;
    @Autowired ChildInvocationGateway calls;
    @Autowired CapabilityStepService steps;
    @Autowired MemoryService memory;
    @Autowired ForgetRecoveryRepository recovery;
    @Autowired RuntimeMapper states;
    @Autowired TransactionTemplate tx;
    @Autowired DataSource source;
    private JdbcTemplate sql;

    @AfterAll
    static void stop() {
        PG.stop();
    }

    @BeforeEach
    void reset() {
        sql = new JdbcTemplate(source);
        sql.execute(
                "TRUNCATE agent_runtime.states,business.conversations,memory.owners,memory.jobs,memory.operations,memory.audit CASCADE");
        sql.update(
                "UPDATE memory.privacy_ledger_binding SET lineage=?,active=false",
                UUID.randomUUID().toString());
        publishBase(32);
    }

    @Test
    void nativeLeafRunsInTheExistingRootAndReplaysTheSamePersistedResult() {
        var target = leaf(ALICE, List.of());
        var parent = parent(ALICE, target);
        var param = parameter(target);
        var first =
                runtime.run(parent, PrivateAgentContracts.binding(target), param)
                        .block(Duration.ofSeconds(15));
        assertThat(first).isNotNull();
        String firstText = ((TextBlock) first.getOutput().getFirst()).getText();
        assertThat(PayloadJson.object(firstText).get("answer"))
                .isEqualTo("已收到委派：" + INPUT.get("message"));
        var replay =
                runtime.run(parent, PrivateAgentContracts.binding(target), param)
                        .block(Duration.ofSeconds(15));
        assertThat(((TextBlock) replay.getOutput().getFirst()).getText()).isEqualTo(firstText);
        assertThat(rootModels(parent)).isEqualTo(1);
        assertThat(calls.get(parent.turn().id(), CALL).modelCalls()).isEqualTo(1);
        assertThat(calls.get(parent.turn().id(), CALL).toolCalls()).isZero();
        assertThat(sql.queryForObject("SELECT count(*) FROM business.turns", Integer.class))
                .isOne();
        assertThat(sql.queryForObject("SELECT count(*) FROM business.commands", Integer.class))
                .isOne();
        assertThat(sql.queryForObject("SELECT count(*) FROM memory.sources", Integer.class))
                .isOne();
        assertThat(
                        sql.queryForObject(
                                "SELECT count(*) FROM agent_runtime.states WHERE NOT deleted",
                                Integer.class))
                .isOne();
    }

    @Test
    void completedNativeCheckpointFinishesTheReceiptWithExhaustedPersistentModelBudget() {
        var target = leaf(ALICE, List.of());
        var parent = parent(ALICE, target);
        var invocation = children.beginPrivate(parent, CALL, INPUT, target).invocation();
        for (int n = 0; n < 4; n++) children.reserveModel(invocation);
        var scope = target.scope(parent.scope(), CALL);
        var finalState =
                AgentState.builder()
                        .userId(scope.userId())
                        .sessionId(scope.sessionId())
                        .addMessage(
                                Msg.builder()
                                        .role(MsgRole.ASSISTANT)
                                        .content(
                                                TextBlock.builder()
                                                        .text("Stored final leaf answer.")
                                                        .build())
                                        .build())
                        .build();
        var store = new PrivateAgentStateStore(states, scope, children, invocation);
        assertThat(
                        store.saveIfVersion(
                                scope.userId(), scope.sessionId(), "agent_state", finalState, 0))
                .isEqualTo(1);
        var result =
                runtime.run(parent, PrivateAgentContracts.binding(target), parameter(target))
                        .block(Duration.ofSeconds(15));
        assertThat(result).isNotNull();
        assertThat(
                        PayloadJson.object(((TextBlock) result.getOutput().getFirst()).getText())
                                .get("answer"))
                .isEqualTo("Stored final leaf answer.");
        assertThat(rootModels(parent)).isEqualTo(4);
        assertThat(calls.get(parent.turn().id(), CALL).modelCalls()).isEqualTo(4);
        assertThat(calls.get(parent.turn().id(), CALL).status()).isEqualTo("completed");
    }

    @Test
    void parentPlanPinsTheOldLeafAcrossDraftEditsAndPublicationDrift() {
        var target = leaf(ALICE, List.of());
        var parent = parent(ALICE, target);
        var first = children.beginPrivate(parent, CALL, INPUT, target);
        assertThat(first.release().systemPrompt()).isEqualTo("Original leaf prompt.");
        assertThat(first.release().plan().capabilities()).isEmpty();
        agents.update(
                ALICE,
                target.agentId(),
                1,
                "Changed",
                "New inherited tools.",
                new AgentComposition("primary", List.of("function:search_catalog")));
        var newer = agents.publish(ALICE, target.agentId(), 2, 1);
        assertThat(newer.releaseHash()).isNotEqualTo(target.releaseHash());

        var replay = rebuilt().beginPrivate(parent, CALL, INPUT, target);
        assertThat(replay.release()).isEqualTo(first.release());
        children.reserveModel(replay.invocation());
        assertThat(children.complete(replay.invocation(), () -> Map.of("answer", "original")))
                .isEqualTo(Map.of("answer", "original"));
        assertThat(calls.get(parent.turn().id(), CALL).targetReleaseHash())
                .isEqualTo(target.releaseHash());
        var changed = new PrivateAgentTarget(target.agentId(), newer.releaseHash());
        assertThatThrownBy(
                        () ->
                                children.beginPrivate(
                                        parent, "unapproved-new-release", INPUT, changed))
                .hasMessage("agent_delegation_denied");
        assertThat(childRows(parent)).isOne();
    }

    @Test
    void anotherTargetOrAnUnboundBuiltinParentCannotBorrowAnApprovedBinding() {
        var selected = leaf(ALICE, List.of());
        var other = leaf(ALICE, List.of());
        var parent = parent(ALICE, selected);
        assertThatThrownBy(() -> children.beginPrivate(parent, CALL, INPUT, other))
                .hasMessage("agent_delegation_denied");
        assertThat(childRows(parent)).isZero();
        assertThat(rootModels(parent)).isZero();
        var builtin = submit(ALICE, tasks.createConversation(ALICE));
        assertThatThrownBy(() -> children.beginPrivate(builtin, CALL, INPUT, selected))
                .hasMessage("agent_delegation_denied");
        assertThat(childRows(builtin)).isZero();
    }

    @ParameterizedTest
    @ValueSource(strings = {"inherit", "tools", "foreign-subject", "foreign-tenant", "unpublished"})
    void nonLeafOrForeignTargetsCannotBecomeParentCapabilities(String failure) {
        Owner owner =
                switch (failure) {
                    case "foreign-subject" -> new Owner(ALICE.tenant(), "bob");
                    case "foreign-tenant" -> new Owner("another-tenant", ALICE.subject());
                    default -> ALICE;
                };
        String id = UUID.randomUUID().toString();
        AgentComposition composition =
                failure.equals("inherit")
                        ? null
                        : new AgentComposition(
                                "primary",
                                failure.equals("tools")
                                        ? List.of("function:search_catalog")
                                        : List.of());
        agents.create(owner, id, "Candidate", "Candidate prompt.", composition);
        if (!failure.equals("unpublished")) agents.publish(owner, id, 1, 0);
        String parentId = UUID.randomUUID().toString();
        agents.create(
                ALICE,
                parentId,
                "Parent",
                "Parent prompt.",
                new AgentComposition("primary", List.of("agent:" + id)));
        assertThatThrownBy(() -> agents.publish(ALICE, parentId, 1, 0))
                .isInstanceOf(RuntimeException.class);
        assertThat(releases.currentForAgent(parentId)).isNull();
        assertThat(
                        sql.queryForObject(
                                "SELECT count(*) FROM business.child_invocations", Integer.class))
                .isZero();
    }

    @ParameterizedTest
    @ValueSource(strings = {"wrong-owner", "nonleaf"})
    void restoredInvalidTargetBindingAndResultAreUnsafeEvenBeforeAnySdkStateExists(
            String corruption) {
        var approved = leaf(ALICE, List.of());
        var parent = parent(ALICE, approved);
        var invalid =
                corruption.equals("wrong-owner")
                        ? leaf(new Owner(ALICE.tenant(), "foreign-target-owner"), List.of())
                        : leaf(ALICE, List.of("function:search_catalog"));
        String callId = "restored-invalid-target:" + corruption;
        String inputHash = "c".repeat(64);
        // Simulate an inconsistent restore using valid foreign keys, without disabling identity
        // triggers or granting execution through the Java publication/dispatch boundaries.
        sql.update(
                "INSERT INTO business.child_invocations(root_turn_id,call_id,target,target_agent_id,target_release_hash,input_hash,scopes,status)"
                        + " VALUES(?,?,'private_agent',?,?,?,'[]'::jsonb,'completed')",
                parent.turn().id(),
                callId,
                invalid.agentId(),
                invalid.releaseHash(),
                inputHash);
        sql.update(
                "INSERT INTO business.capability_steps(turn_id,namespace,step,input_hash,result) VALUES(?,?,'result',?,?)",
                parent.turn().id(),
                "child:" + callId,
                inputHash,
                "{\"answer\":\"restored private result must remain blocked\"}");
        assertThat(sql.queryForObject("SELECT count(*) FROM agent_runtime.states", Integer.class))
                .isZero();
        assertThat(
                        sql.queryForObject(
                                "SELECT binding_valid FROM agent_runtime.child_invocation_bindings() WHERE root_turn_id=? AND call_id=?",
                                Boolean.class,
                                parent.turn().id(),
                                callId))
                .isFalse();
        for (String kind : List.of("child_invocations", "capability_steps")) {
            assertThat(
                            sql.queryForObject(
                                    "SELECT count(*) FROM agent_runtime.unsafe_execution_data() WHERE tenant=? AND subject=? AND kind=?",
                                    Integer.class,
                                    ALICE.tenant(),
                                    ALICE.subject(),
                                    kind))
                    .as(kind)
                    .isEqualTo(1);
        }
        assertThat(recovery.unsafeRows()).isPositive();

        // Invalid binding still retains enough frozen identity to locate an old SDK body. It is
        // a cleanup candidate, never an authorization source or an excuse to create fresh budget.
        var scope = invalid.scope(parent.scope(), callId);
        assertThat(
                        sql.queryForObject(
                                "SELECT invocation_bound FROM agent_runtime.execution_scopes() WHERE root_turn_id=? AND namespace=? AND user_id=? AND session_id=?",
                                Boolean.class,
                                parent.turn().id(),
                                scope.namespace(),
                                scope.userId(),
                                scope.sessionId()))
                .isFalse();
        var key =
                new RuntimeMapper.StateKey(
                        scope.namespace(), scope.userId(), scope.sessionId(), "agent_state");
        assertThat(states.insertStateIfAbsent(key, "{\"private\":\"restored-sdk-body\"}"))
                .isEqualTo(1);
        assertThat(
                        sql.queryForObject(
                                "SELECT count(*) FROM agent_runtime.unsafe_execution_data() WHERE tenant=? AND subject=? AND kind='sdk_states'",
                                Integer.class,
                                ALICE.tenant(),
                                ALICE.subject()))
                .isEqualTo(1);

        assertThat(memory.forget(ALICE, 1, "forget-corrupt-private-target:" + corruption))
                .isEqualTo(2);
        var erased = states.getState(key);
        assertThat(erased.deleted()).isTrue();
        assertThat(erased.payload()).isNull();
        assertThat(erased.version()).isEqualTo(2);
        assertThat(states.updateStateByExpectedVersion(key, "{}", 1)).isZero();
        assertThat(states.insertStateIfAbsent(key, "{}")).isZero();
        assertThat(childRows(parent)).isZero();
        assertThat(
                        sql.queryForObject(
                                "SELECT count(*) FROM business.capability_steps WHERE turn_id=?",
                                Integer.class,
                                parent.turn().id()))
                .isZero();
        assertThat(
                        sql.queryForObject(
                                "SELECT count(*) FROM agent_runtime.unsafe_execution_data()",
                                Integer.class))
                .isZero();
        assertThat(recovery.unsafeRows()).isZero();
    }

    @Test
    void missingArtifactPermissionRejectsDelegationBeforeSavingOrCharging() {
        var target = leaf(ALICE, List.of());
        String parentId = parentId(ALICE, target);
        String conversation = submission.createConversation(ALICE, parentId);
        governance.changeGrant(ALICE, conversation, "deny-before-admission", 1, List.of());
        var parent = submit(ALICE, conversation);
        assertThatThrownBy(() -> children.beginPrivate(parent, CALL, INPUT, target))
                .hasMessage("agent_delegation_denied");
        assertThat(childRows(parent)).isZero();
        assertThat(rootModels(parent)).isZero();
    }

    @Test
    void persistentBudgetSurvivesServiceReconstructionAndNewAuthorizedAttempt() {
        var target = leaf(ALICE, List.of());
        var parent = parent(ALICE, target);
        var first = children.beginPrivate(parent, CALL, INPUT, target).invocation();
        children.reserveModel(first);
        children.reserveModel(first);
        var next = nextAttempt(parent);
        var rebuilt = rebuilt();
        var resumed = rebuilt.beginPrivate(next, CALL, INPUT, target).invocation();
        rebuilt.reserveModel(resumed);
        rebuilt.reserveModel(resumed);
        assertThatThrownBy(() -> rebuilt.reserveModel(resumed))
                .hasMessage("child_model_budget_exceeded");
        assertThat(calls.get(parent.turn().id(), CALL).modelCalls()).isEqualTo(4);
        assertThat(rootModels(parent)).isEqualTo(4);
        assertThat(calls.get(parent.turn().id(), CALL).toolCalls()).isZero();
        assertThatThrownBy(() -> children.reserveModel(first)).isInstanceOf(Fenced.class);
    }

    @Test
    void frozenLeafLimitCanNarrowTheFourCallCeiling() {
        publishBase(2);
        var target = leaf(ALICE, List.of());
        publishBase(32);
        var parent = parent(ALICE, target);
        var invocation = children.beginPrivate(parent, CALL, INPUT, target).invocation();
        assertThat(invocation.maxModelCalls()).isEqualTo(2);
        children.reserveModel(invocation);
        children.reserveModel(invocation);
        assertThatThrownBy(() -> children.reserveModel(invocation))
                .hasMessage("child_model_budget_exceeded");
        assertThat(calls.get(parent.turn().id(), CALL).modelCalls()).isEqualTo(2);
        assertThat(rootModels(parent)).isEqualTo(2);
    }

    @Test
    void exhaustedRootBudgetRollsBackChildReservationWithoutCreatingToolAllowance() {
        var target = leaf(ALICE, List.of());
        publishBase(1);
        var parent = parent(ALICE, target);
        var invocation = children.beginPrivate(parent, CALL, INPUT, target).invocation();
        children.reserveModel(invocation);
        assertThatThrownBy(() -> children.reserveModel(invocation))
                .hasMessage("execution_budget_exhausted");
        assertThat(calls.get(parent.turn().id(), CALL).modelCalls()).isEqualTo(1);
        assertThat(rootModels(parent)).isEqualTo(1);
        var executed = new AtomicBoolean();
        assertThatThrownBy(
                        () ->
                                children.snapshot(
                                        invocation,
                                        "forged-tool",
                                        Map.of(),
                                        () -> {
                                            executed.set(true);
                                            return "must not execute";
                                        }))
                .hasMessage("child_tools_denied");
        assertThat(executed).isFalse();
        assertThat(calls.reserveTool(parent.turn().id(), CALL, invocation.inputHash())).isZero();
        assertThatThrownBy(
                        () ->
                                sql.update(
                                        "UPDATE business.child_invocations SET tool_calls=1 WHERE root_turn_id=? AND call_id=?",
                                        parent.turn().id(),
                                        CALL))
                .isInstanceOf(DataIntegrityViolationException.class);
    }

    @Test
    void completionReplayAndInputOrKindConflictsPreserveTheOriginalIdentity() {
        var target = leaf(ALICE, List.of());
        var parent = parent(ALICE, target);
        var invocation = children.beginPrivate(parent, CALL, INPUT, target).invocation();
        children.reserveModel(invocation);
        var result = children.complete(invocation, () -> Map.of("answer", "first exact answer"));
        var cached = rebuilt().beginPrivate(parent, CALL, INPUT, target).invocation();
        assertThat(cached.cachedResult()).contains("first exact answer");
        assertThat(
                        children.complete(
                                cached,
                                () -> {
                                    throw new AssertionError("completed result cannot run again");
                                }))
                .isEqualTo(result);
        assertThat(rootModels(parent)).isEqualTo(1);
        assertThatThrownBy(
                        () ->
                                children.beginPrivate(
                                        parent, CALL, Map.of("message", "changed"), target))
                .hasMessage("child_input_conflict");
        calls.insertIfAbsent(parent.turn().id(), "market-collision", "b".repeat(64));
        assertThatThrownBy(() -> children.beginPrivate(parent, "market-collision", INPUT, target))
                .hasMessage("child_invocation_invalid");
        assertThatThrownBy(
                        () ->
                                sql.update(
                                        "UPDATE business.child_invocations SET target_release_hash=? WHERE root_turn_id=? AND call_id=?",
                                        parent.turn().releaseHash(),
                                        parent.turn().id(),
                                        CALL))
                .isInstanceOf(DataIntegrityViolationException.class);
        assertThat(calls.get(parent.turn().id(), CALL).targetReleaseHash())
                .isEqualTo(target.releaseHash());
    }

    @Test
    void resultAndCompletionMarkerRollBackTogetherWhenMetadataCasFails() {
        var target = leaf(ALICE, List.of());
        var parent = parent(ALICE, target);
        var invocation = children.beginPrivate(parent, CALL, INPUT, target).invocation();
        sql.execute(
                "CREATE FUNCTION public.reject_private_completion() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN IF NEW.status='completed' THEN RAISE EXCEPTION 'synthetic private completion failure'; END IF; RETURN NEW; END $$");
        sql.execute(
                "CREATE TRIGGER reject_private_completion BEFORE UPDATE ON business.child_invocations FOR EACH ROW EXECUTE FUNCTION public.reject_private_completion()");
        try {
            assertThatThrownBy(
                            () ->
                                    children.complete(
                                            invocation, () -> Map.of("answer", "no orphan result")))
                    .hasStackTraceContaining("synthetic private completion failure");
        } finally {
            sql.execute("DROP TRIGGER reject_private_completion ON business.child_invocations");
            sql.execute("DROP FUNCTION public.reject_private_completion()");
        }
        assertThat(calls.get(parent.turn().id(), CALL).status()).isEqualTo("pending");
        assertThat(calls.get(parent.turn().id(), CALL).result()).isNull();
        assertThat(
                        sql.queryForObject(
                                "SELECT count(*) FROM business.capability_steps WHERE turn_id=? AND namespace=?",
                                Integer.class,
                                parent.turn().id(),
                                invocation.namespace()))
                .isZero();
    }

    @Test
    void sameCallIdsRemainIsolatedAcrossOwnersAndRootTurns() {
        var alice = leaf(ALICE, List.of());
        var bobOwner = new Owner(ALICE.tenant(), "bob");
        var bob = leaf(bobOwner, List.of());
        var first = parent(ALICE, alice);
        var second = parent(ALICE, alice);
        var foreign = parent(bobOwner, bob);
        for (var pair :
                List.of(
                        Map.entry(first, alice),
                        Map.entry(second, alice),
                        Map.entry(foreign, bob))) {
            var invocation =
                    children.beginPrivate(pair.getKey(), CALL, INPUT, pair.getValue()).invocation();
            children.complete(invocation, () -> Map.of("root", pair.getKey().turn().id()));
        }
        assertThat(calls.get(first.turn().id(), CALL).result())
                .contains(first.turn().id())
                .doesNotContain(second.turn().id(), foreign.turn().id());
        assertThat(alice.scope(first.scope(), CALL))
                .isNotEqualTo(alice.scope(second.scope(), CALL))
                .isNotEqualTo(bob.scope(foreign.scope(), CALL));
        assertThatThrownBy(() -> children.beginPrivate(first, "foreign-binding", INPUT, bob))
                .hasMessage("agent_delegation_denied");
    }

    @ParameterizedTest
    @ValueSource(strings = {"cancel", "lease", "grant", "forget"})
    void parentGatesProtectPrivateCachedResultsAndSdkOperations(String change) {
        var target = leaf(ALICE, List.of());
        var parent = parent(ALICE, target);
        var pending = children.beginPrivate(parent, CALL, INPUT, target).invocation();
        var scope = target.scope(parent.scope(), CALL);
        var store = new PrivateAgentStateStore(states, scope, children, pending);
        store.saveIfVersion(
                scope.userId(), scope.sessionId(), "agent_state", AgentState.builder().build(), 0);
        var completed = children.beginPrivate(parent, "completed", INPUT, target).invocation();
        children.complete(completed, () -> Map.of("answer", "cached"));
        switch (change) {
            case "cancel" ->
                    tasks.cancel(
                            ALICE,
                            parent.turn().conversationId(),
                            parent.turn().id(),
                            "cancel-private");
            case "lease" ->
                    sql.update(
                            "UPDATE business.commands SET lease_until=clock_timestamp()-interval '1 second' WHERE id=?",
                            parent.command().id());
            case "grant" ->
                    governance.changeGrant(
                            ALICE, parent.turn().conversationId(), "revoke-private", 1, List.of());
            case "forget" -> memory.forget(ALICE, 1, "forget-private");
            default -> throw new AssertionError();
        }
        assertThatThrownBy(() -> children.beginPrivate(parent, "completed", INPUT, target))
                .isInstanceOfAny(Fenced.class, Blocked.class);
        assertThatThrownBy(() -> children.complete(completed, () -> "must not read cached result"))
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
        var row =
                states.getState(
                        new RuntimeMapper.StateKey(
                                scope.namespace(),
                                scope.userId(),
                                scope.sessionId(),
                                "agent_state"));
        assertThat(row.version()).isEqualTo(change.equals("forget") ? 2 : 1);
        assertThat(row.deleted()).isEqualTo(change.equals("forget"));
    }

    @Test
    void ledgerReplayErasesFrozenLeafScopesAcrossAttemptsWithoutResurrection() {
        var target = leaf(ALICE, List.of());
        var parent = parent(ALICE, target);
        var first = children.beginPrivate(parent, CALL, INPUT, target).invocation();
        var oldScope = target.scope(parent.scope(), CALL);
        var oldStore = new MyBatisAgentStateStore(states, oldScope);
        children.checkpoint(
                first,
                () ->
                        oldStore.saveIfVersion(
                                oldScope.userId(),
                                oldScope.sessionId(),
                                "agent_state",
                                AgentState.builder().build(),
                                0));
        var next = nextAttempt(parent);
        var resumed = children.beginPrivate(next, CALL, INPUT, target).invocation();
        var newScope = target.scope(next.scope(), CALL);
        var newStore = new MyBatisAgentStateStore(states, newScope);
        children.checkpoint(
                resumed,
                () ->
                        newStore.saveIfVersion(
                                newScope.userId(),
                                newScope.sessionId(),
                                "agent_state",
                                AgentState.builder().build(),
                                0));
        assertThat(
                        sql.queryForList(
                                "SELECT namespace||':'||user_id||':'||session_id FROM agent_runtime.execution_scopes() WHERE root_turn_id=?",
                                String.class,
                                parent.turn().id()))
                .contains(keyText(oldScope), keyText(newScope));
        var fact =
                new ForgetFact(
                        UUID.randomUUID().toString(),
                        recovery.binding().lineage(),
                        ALICE,
                        1,
                        "authoritative-private-forget",
                        ForgetFact.hash(1));
        tx.executeWithoutResult(
                status -> {
                    sql.queryForObject(
                            "SELECT epoch FROM memory.owners WHERE tenant=? AND subject=? FOR UPDATE",
                            Long.class,
                            ALICE.tenant(),
                            ALICE.subject());
                    recovery.prepare(fact);
                    recovery.apply(fact);
                    recovery.complete(ALICE);
                });
        for (var scope : List.of(oldScope, newScope)) {
            var row =
                    states.getState(
                            new RuntimeMapper.StateKey(
                                    scope.namespace(),
                                    scope.userId(),
                                    scope.sessionId(),
                                    "agent_state"));
            assertThat(row.deleted()).isTrue();
            assertThat(row.payload()).isNull();
            assertThat(row.version()).isEqualTo(2);
            assertThat(
                            new MyBatisAgentStateStore(states, scope)
                                    .saveIfVersion(
                                            scope.userId(),
                                            scope.sessionId(),
                                            "agent_state",
                                            AgentState.builder().build(),
                                            0))
                    .isEqualTo(-1);
        }
        assertThat(childRows(parent)).isZero();
        assertThat(
                        sql.queryForObject(
                                "SELECT count(*) FROM agent_runtime.unsafe_execution_data()",
                                Integer.class))
                .isZero();
        tx.executeWithoutResult(
                status -> {
                    sql.queryForObject(
                            "SELECT epoch FROM memory.owners WHERE tenant=? AND subject=? FOR UPDATE",
                            Long.class,
                            ALICE.tenant(),
                            ALICE.subject());
                    recovery.apply(fact);
                    recovery.complete(ALICE);
                });
        assertThat(
                        states.getState(
                                        new RuntimeMapper.StateKey(
                                                oldScope.namespace(),
                                                oldScope.userId(),
                                                oldScope.sessionId(),
                                                "agent_state"))
                                .version())
                .isEqualTo(2);
    }

    private static ToolCallParam parameter(PrivateAgentTarget target) {
        return ToolCallParam.builder()
                .toolUseBlock(
                        NativeToolCalls.call(
                                CALL, PrivateAgentContracts.binding(target).name(), INPUT))
                .input(INPUT)
                .build();
    }

    private PrivateAgentTarget leaf(Owner owner, List<String> capabilities) {
        String id = UUID.randomUUID().toString();
        agents.create(
                owner,
                id,
                "Leaf",
                "Original leaf prompt.",
                new AgentComposition("primary", capabilities));
        return new PrivateAgentTarget(id, agents.publish(owner, id, 1, 0).releaseHash());
    }

    private String parentId(Owner owner, PrivateAgentTarget target) {
        String id = UUID.randomUUID().toString();
        agents.create(
                owner,
                id,
                "Parent",
                "Parent private context must not be forwarded.",
                new AgentComposition("primary", List.of("agent:" + target.agentId())));
        agents.publish(owner, id, 1, 0);
        return id;
    }

    private Claim parent(Owner owner, PrivateAgentTarget target) {
        return submit(owner, submission.createConversation(owner, parentId(owner, target)));
    }

    private Claim submit(Owner owner, String conversation) {
        var accepted =
                tasks.submit(
                        owner,
                        conversation,
                        UUID.randomUUID().toString(),
                        "Parent original user message.");
        var claim = tasks.claim("private-agent-test", 120000);
        assertThat(claim.turn().id()).isEqualTo(accepted.turnId());
        return claim;
    }

    private Claim nextAttempt(Claim previous) {
        tx.executeWithoutResult(
                status ->
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
                                }));
        var next = tasks.claim("private-agent-replacement", 120000);
        assertThat(next.turn().id()).isEqualTo(previous.turn().id());
        return next;
    }

    private ChildInvocationService rebuilt() {
        return new ChildInvocationService(calls, steps, tasks.policy(), releases);
    }

    private int childRows(Claim parent) {
        return sql.queryForObject(
                "SELECT count(*) FROM business.child_invocations WHERE root_turn_id=?",
                Integer.class,
                parent.turn().id());
    }

    private int rootModels(Claim parent) {
        return sql.queryForObject(
                "SELECT model_calls FROM business.turn_policies WHERE turn_id=?",
                Integer.class,
                parent.turn().id());
    }

    private void publishBase(int modelLimit) {
        releases.publish(
                new ReleaseDefinition(
                        "private-agent-test",
                        "finance_agent",
                        ReleaseDefinition.SCOPES,
                        ReleaseDefinition.OFFLINE,
                        null,
                        modelLimit,
                        64,
                        new BusinessFeatures(List.of(), PackagedSkills.hash(), "{}", null)));
    }

    private static String keyText(com.financeclaw.domain.ExecutionScope scope) {
        return scope.namespace() + ':' + scope.userId() + ':' + scope.sessionId();
    }
}
