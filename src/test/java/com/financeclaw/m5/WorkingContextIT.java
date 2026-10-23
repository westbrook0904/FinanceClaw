package com.financeclaw.m5;

import static org.assertj.core.api.Assertions.*;

import com.financeclaw.domain.Task.*;
import com.financeclaw.infrastructure.agentscope.context.WorkingContextSession;
import com.financeclaw.infrastructure.state.MyBatisAgentStateStore;
import com.financeclaw.infrastructure.state.RuntimeMapper;
import io.agentscope.core.message.Msg;
import io.agentscope.core.message.MsgRole;
import io.agentscope.core.state.AgentState;
import java.util.ArrayList;
import java.util.List;
import org.apache.ibatis.session.Configuration;
import org.apache.ibatis.session.LocalCacheScope;
import org.flywaydb.core.Flyway;
import org.junit.jupiter.api.*;
import org.junit.jupiter.params.ParameterizedTest;
import org.junit.jupiter.params.provider.ValueSource;
import org.mybatis.spring.SqlSessionFactoryBean;
import org.mybatis.spring.SqlSessionTemplate;
import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.jdbc.datasource.DriverManagerDataSource;
import org.springframework.jdbc.support.JdbcTransactionManager;
import org.springframework.transaction.support.TransactionTemplate;
import org.testcontainers.containers.PostgreSQLContainer;

/** 使用真实 PostgreSQL 核对上下文投影的封存资格、用户隔离及 SDK CAS 事务边界。 */
class WorkingContextIT {
    private static final PostgreSQLContainer<?> PG =
            new PostgreSQLContainer<>("postgres:16.10-alpine");
    private static final Owner ALICE = new Owner("context-tenant", "alice");
    private static final Owner BOB = new Owner("context-tenant", "bob");
    private static final String CONVERSATION = "conversation";
    private static final String RELEASE = "release";
    private static final long GRANT_REVISION = 3;
    private static final long SEALED_STATE_VERSION = 7;
    private static JdbcTemplate sql;
    private static RuntimeMapper mapper;
    private static TransactionTemplate tx;
    private Claim current;

    @BeforeAll
    static void database() throws Exception {
        PG.start();
        var source =
                new DriverManagerDataSource(PG.getJdbcUrl(), PG.getUsername(), PG.getPassword());
        Flyway.configure().dataSource(source).load().migrate();
        sql = new JdbcTemplate(source);
        tx = new TransactionTemplate(new JdbcTransactionManager(source));
        var configuration = new Configuration();
        configuration.setMapUnderscoreToCamelCase(true);
        configuration.setCacheEnabled(false);
        configuration.setLocalCacheScope(LocalCacheScope.STATEMENT);
        configuration.addMapper(RuntimeMapper.class);
        var factory = new SqlSessionFactoryBean();
        factory.setDataSource(source);
        factory.setConfiguration(configuration);
        mapper = new SqlSessionTemplate(factory.getObject()).getMapper(RuntimeMapper.class);
    }

    @AfterAll
    static void stop() {
        PG.stop();
    }

    @BeforeEach
    void reset() {
        sql.execute("TRUNCATE business.conversations,memory.owners,agent_runtime.states CASCADE");
        current = seed("current", ALICE, CONVERSATION, "running", null, SEALED_STATE_VERSION);
    }

    @Test
    void latestCompletedProjectionRoundTripsAndWaitingWorkCannotBecomeNewTurnHistory() {
        var older =
                seed("older", ALICE, CONVERSATION, "completed", "completed", SEALED_STATE_VERSION);
        var latest =
                seed("latest", ALICE, CONVERSATION, "completed", "completed", SEALED_STATE_VERSION);
        save(older, "older completed");
        save(latest, "latest completed");
        sql.update("UPDATE business.turns SET created_at='2026-10-01T00:00:00Z' WHERE id='older'");
        sql.update("UPDATE business.turns SET created_at='2026-10-02T00:00:00Z' WHERE id='latest'");
        // 查询层自身也必须排除 waiting；受理层通常会在等待尚未结束时阻止新 Turn。
        sql.update("UPDATE business.turns SET status='completed' WHERE id='current'");
        var waiting =
                seed("waiting", ALICE, CONVERSATION, "waiting", "waiting", SEALED_STATE_VERSION);
        save(waiting, "waiting must stay private to its Turn");
        sql.update(
                "UPDATE business.turns SET created_at='2026-10-03T00:00:00Z' WHERE id='waiting'");

        var row = mapper.getLatestCompletedWorkContext(current, GRANT_REVISION);
        assertThat(row).isNotNull();
        assertThat(WorkingContextSession.decode(row.payload()))
                .extracting(Msg::getTextContent)
                .containsExactly("latest completed");
        assertThat(WorkingContextSession.decode(row.transcript()))
                .extracting(Msg::getTextContent)
                .containsExactly("latest completed");
    }

    @ParameterizedTest
    @ValueSource(
            strings = {
                "tenant",
                "subject",
                "conversation",
                "release",
                "grant",
                "epoch",
                "attempt-version",
                "attempt-outcome",
                "turn-status",
                "attempt-turn",
                "turn-owner",
                "turn-conversation",
                "turn-release",
                "current-epoch",
                "missing-current-source"
            })
    void onlyTheMatchingSealedOwnerEpochGrantReleaseAndAttemptCanBeInherited(String mismatch) {
        var previous =
                seed(
                        "previous",
                        ALICE,
                        CONVERSATION,
                        "completed",
                        "completed",
                        SEALED_STATE_VERSION);
        save(previous, "sensitive previous answer");
        sql.update("UPDATE business.turns SET status='completed' WHERE id='current'");
        switch (mismatch) {
            case "tenant" ->
                    sql.update("UPDATE agent_runtime.work_contexts SET tenant='other-tenant'");
            case "subject" -> sql.update("UPDATE agent_runtime.work_contexts SET subject='bob'");
            case "conversation" -> {
                conversation("other", ALICE);
                sql.update("UPDATE agent_runtime.work_contexts SET conversation_id='other'");
            }
            case "release" ->
                    sql.update(
                            "UPDATE agent_runtime.work_contexts SET release_hash='other-release'");
            case "grant" ->
                    sql.update(
                            "UPDATE agent_runtime.work_contexts SET grant_revision=grant_revision+1");
            case "epoch" -> sql.update("UPDATE agent_runtime.work_contexts SET epoch=epoch+1");
            case "attempt-version" ->
                    sql.update(
                            "UPDATE business.attempts SET state_version=state_version+1 WHERE id=?",
                            previous.command().attemptId());
            case "attempt-outcome" ->
                    sql.update(
                            "UPDATE business.attempts SET outcome='waiting' WHERE id=?",
                            previous.command().attemptId());
            case "turn-status" ->
                    sql.update("UPDATE business.turns SET status='waiting' WHERE id='previous'");
            case "attempt-turn" -> {
                var other =
                        seed(
                                "other",
                                BOB,
                                "other-conversation",
                                "completed",
                                "completed",
                                SEALED_STATE_VERSION);
                sql.update(
                        "UPDATE agent_runtime.work_contexts SET attempt_id=?",
                        other.command().attemptId());
            }
            case "turn-owner" ->
                    sql.update("UPDATE business.turns SET subject='bob' WHERE id='previous'");
            case "turn-conversation" -> {
                conversation("other", ALICE);
                sql.update("UPDATE business.turns SET conversation_id='other' WHERE id='previous'");
            }
            case "turn-release" ->
                    sql.update(
                            "UPDATE business.turns SET release_hash='other-release' WHERE id='previous'");
            case "current-epoch" ->
                    sql.update("UPDATE memory.sources SET epoch=2 WHERE id='current'");
            case "missing-current-source" ->
                    sql.update("DELETE FROM memory.sources WHERE id='current'");
            default -> throw new AssertionError(mismatch);
        }
        assertThat(mapper.getLatestCompletedWorkContext(current, GRANT_REVISION)).isNull();
    }

    @Test
    void completedTurnsInAnotherConversationOrOwnerCannotBecomeHistory() {
        var foreignConversation =
                seed(
                        "foreign-conversation",
                        ALICE,
                        "another-conversation",
                        "completed",
                        "completed",
                        SEALED_STATE_VERSION);
        var foreignOwner =
                seed(
                        "foreign-owner",
                        BOB,
                        "bob-conversation",
                        "completed",
                        "completed",
                        SEALED_STATE_VERSION);
        save(foreignConversation, "different conversation secret");
        save(foreignOwner, "different owner secret");
        assertThat(mapper.getLatestCompletedWorkContext(current, GRANT_REVISION)).isNull();
    }

    @Test
    void inheritedProjectionContinuesButCurrentTurnTranscriptDoesNotDuplicateEarlierTurns() {
        var previous =
                seed(
                        "previous",
                        ALICE,
                        CONVERSATION,
                        "completed",
                        "completed",
                        SEALED_STATE_VERSION);
        save(previous, "previous question and answer");
        var session = new WorkingContextSession(mapper, current, GRANT_REVISION);
        var history = new ArrayList<>(session.previous());
        history.add(message(session.inputId(), "本轮问题 😀"));
        session.record(history);
        var state = AgentState.builder().context(history).build();
        session.persist(state, 1);

        var saved = mapper.getTurnWorkContext(current.turn(), current.command().attemptId(), 1);
        assertThat(WorkingContextSession.decode(saved.payload()))
                .extracting(Msg::getTextContent)
                .containsExactly("previous question and answer", "本轮问题 😀");
        assertThat(WorkingContextSession.decode(saved.transcript()))
                .extracting(Msg::getTextContent)
                .containsExactly("本轮问题 😀");
    }

    @Test
    void resumeProjectionRequiresTheOriginalAttemptVersionOwnerAndPrivacyEpoch() {
        save(current, "waiting transcript");
        assertThat(
                        mapper.getTurnWorkContext(
                                current.turn(),
                                current.command().attemptId(),
                                SEALED_STATE_VERSION))
                .isNotNull();
        assertThat(
                        mapper.getTurnWorkContext(
                                current.turn(), "another-attempt", SEALED_STATE_VERSION))
                .isNull();
        assertThat(
                        mapper.getTurnWorkContext(
                                current.turn(),
                                current.command().attemptId(),
                                SEALED_STATE_VERSION + 1))
                .isNull();
        var t = current.turn();
        var wrongOwner =
                new Turn(
                        t.id(),
                        t.conversationId(),
                        t.tenant(),
                        "bob",
                        t.idempotencyKey(),
                        t.requestHash(),
                        t.message(),
                        t.releaseHash(),
                        t.status(),
                        t.revision(),
                        t.result(),
                        t.reason());
        assertThat(
                        mapper.getTurnWorkContext(
                                wrongOwner, current.command().attemptId(), SEALED_STATE_VERSION))
                .isNull();
        sql.update("UPDATE memory.sources SET epoch=2 WHERE id='current'");
        assertThat(
                        mapper.getTurnWorkContext(
                                current.turn(),
                                current.command().attemptId(),
                                SEALED_STATE_VERSION))
                .isNull();
    }

    @Test
    void sdkCasAndProjectionJoinTheSameDatabaseTransactionAndRollbackTogether() {
        var scope = current.scope();
        var store = new MyBatisAgentStateStore(mapper, scope);
        var session = new WorkingContextSession(mapper, current, GRANT_REVISION);
        var state =
                AgentState.builder()
                        .context(List.of(message(session.inputId(), "atomic payload")))
                        .build();
        assertThatThrownBy(
                        () ->
                                tx.executeWithoutResult(
                                        status -> {
                                            long version =
                                                    store.saveIfVersion(
                                                            scope.userId(),
                                                            scope.sessionId(),
                                                            "agent_state",
                                                            state,
                                                            0);
                                            assertThat(version).isEqualTo(1);
                                            session.persist(state, version);
                                            throw new IllegalStateException(
                                                    "simulated transaction rollback");
                                        }))
                .hasMessage("simulated transaction rollback");
        assertThat(
                        store.getVersioned(
                                        scope.userId(),
                                        scope.sessionId(),
                                        "agent_state",
                                        AgentState.class)
                                .isPresent())
                .isFalse();
        assertThat(mapper.getTurnWorkContext(current.turn(), current.command().attemptId(), 1))
                .isNull();
        tx.executeWithoutResult(
                status -> {
                    long version =
                            store.saveIfVersion(
                                    scope.userId(), scope.sessionId(), "agent_state", state, 0);
                    assertThat(version).isEqualTo(1);
                    session.persist(state, version);
                });
        assertThat(
                        store.getVersioned(
                                        scope.userId(),
                                        scope.sessionId(),
                                        "agent_state",
                                        AgentState.class)
                                .version())
                .isEqualTo(1);
        assertThat(mapper.getTurnWorkContext(current.turn(), current.command().attemptId(), 1))
                .isNotNull();
    }

    private static void save(Claim claim, String text) {
        String payload =
                WorkingContextSession.encode(
                        List.of(message("message-" + claim.turn().id(), text)));
        mapper.saveTurnWorkContext(claim, GRANT_REVISION, SEALED_STATE_VERSION, payload, payload);
    }

    private static Msg message(String id, String text) {
        return Msg.builder().id(id).name("user").role(MsgRole.USER).textContent(text).build();
    }

    private static void conversation(String id, Owner owner) {
        sql.update(
                "INSERT INTO business.conversations(id,tenant,subject) VALUES(?,?,?) ON CONFLICT DO NOTHING",
                id,
                owner.tenant(),
                owner.subject());
    }

    private static Claim seed(
            String id,
            Owner owner,
            String conversation,
            String status,
            String outcome,
            long version) {
        conversation(conversation, owner);
        sql.update(
                "INSERT INTO memory.owners(tenant,subject) VALUES(?,?) ON CONFLICT DO NOTHING",
                owner.tenant(),
                owner.subject());
        sql.update(
                "INSERT INTO business.turns(id,conversation_id,tenant,subject,idempotency_key,request_hash,message,release_hash,status) VALUES(?,?,?,?,?,'hash','message',?,?)",
                id,
                conversation,
                owner.tenant(),
                owner.subject(),
                id,
                RELEASE,
                status);
        sql.update(
                "INSERT INTO memory.sources(id,tenant,subject,epoch,status) VALUES(?,?,?,1,'profile_only')",
                id,
                owner.tenant(),
                owner.subject());
        String command = "command-" + id;
        String attempt = "attempt-" + id;
        sql.update(
                "INSERT INTO business.commands(id,turn_id,status,attempt_id,fence) VALUES(?,?,'done',?,1)",
                command,
                id,
                attempt);
        sql.update(
                "INSERT INTO business.attempts(id,command_id,fence,state_version,outcome) VALUES(?,?,1,?,?)",
                attempt,
                command,
                version,
                outcome);
        var turn =
                new Turn(
                        id,
                        conversation,
                        owner.tenant(),
                        owner.subject(),
                        id,
                        "hash",
                        "message",
                        RELEASE,
                        status,
                        1,
                        null,
                        null);
        return new Claim(new Command(command, id, "running", attempt, 1, true, null), turn);
    }
}
