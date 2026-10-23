package com.financeclaw.m2;

import static org.assertj.core.api.Assertions.*;

import com.financeclaw.app.release.ReleaseService;
import com.financeclaw.app.task.TaskEventHub;
import com.financeclaw.bootstrap.FinanceClawApplication;
import com.financeclaw.domain.Governance.*;
import com.financeclaw.domain.Task.*;
import com.financeclaw.domain.release.BusinessFeatures;
import com.financeclaw.domain.release.ReleaseDefinition;
import com.financeclaw.domain.release.RemoteEndpoint;
import com.financeclaw.fixture.GovernanceFixture;
import com.financeclaw.fixture.TaskFixture;
import com.financeclaw.infrastructure.agentscope.execution.*;
import com.financeclaw.infrastructure.agentscope.skills.PackagedSkills;
import com.financeclaw.infrastructure.governance.GovernanceMapper;
import com.financeclaw.infrastructure.release.BusinessConfigurationLoader;
import com.financeclaw.infrastructure.state.RuntimeMapper;
import com.financeclaw.support.PayloadJson;
import java.nio.file.*;
import java.util.*;
import java.util.concurrent.*;
import javax.sql.DataSource;
import org.junit.jupiter.api.*;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.boot.test.context.SpringBootTest;
import org.springframework.boot.test.web.client.TestRestTemplate;
import org.springframework.context.annotation.Import;
import org.springframework.http.*;
import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.test.context.DynamicPropertyRegistry;
import org.springframework.test.context.DynamicPropertySource;
import org.testcontainers.containers.PostgreSQLContainer;

@Import({TaskFixture.class, GovernanceFixture.class})
@SpringBootTest(
        classes = FinanceClawApplication.class,
        webEnvironment = SpringBootTest.WebEnvironment.RANDOM_PORT,
        properties = {
            "financeclaw.worker.enabled=false",
            "financeclaw.integrations.enabled=false",
            "financeclaw.auth.token=m2-test-token"
        })
class GovernanceIT {
    static final PostgreSQLContainer<?> PG =
            new PostgreSQLContainer<>("postgres:16.10-alpine")
                    .withDatabaseName("m2")
                    .withUsername("m2")
                    .withPassword("test-only");

    static {
        PG.start();
    }

    @DynamicPropertySource
    static void database(DynamicPropertyRegistry r) {
        r.add("spring.datasource.url", PG::getJdbcUrl);
        r.add("spring.datasource.username", PG::getUsername);
        r.add("spring.datasource.password", PG::getPassword);
    }

    @Autowired TaskFixture tasks;
    @Autowired TaskEventHub events;
    @Autowired org.springframework.context.ApplicationContext taskContext;
    @Autowired GovernanceFixture governance;
    @Autowired GovernanceMapper db;
    @Autowired RuntimeMapper states;
    @Autowired ReleaseService releases;
    @Autowired DataSource source;
    @Autowired TestRestTemplate http;
    @Autowired org.springframework.transaction.support.TransactionTemplate tx;
    JdbcTemplate sql;
    final Owner owner = new Owner("local", "developer");

    @BeforeEach
    void reset() {
        sql = new JdbcTemplate(source);
        sql.execute("TRUNCATE business.conversations CASCADE");
        releases.publish(
                new ReleaseDefinition(
                        "m2-v1",
                        "finance_agent",
                        ReleaseDefinition.SCOPES,
                        ReleaseDefinition.OFFLINE,
                        null,
                        12,
                        16,
                        BusinessConfigurationLoader.defaults()));
    }

    Accepted submit(String message) {
        return tasks.submit(
                owner, tasks.createConversation(owner), UUID.randomUUID().toString(), message);
    }

    Claim claim() {
        return tasks.claim("m2-test", 60000);
    }

    void run(Claim claim) throws Exception {
        try (var w =
                com.financeclaw.m1.TaskWorkerFixture.create(
                        taskContext,
                        new ScriptedRuntime(
                                tasks,
                                governance,
                                states,
                                taskContext.getBean(
                                        com.financeclaw.infrastructure.agentscope.capability
                                                .BusinessCapabilities.class)),
                        false,
                        1,
                        60000,
                        2000)) {
            w.execute(claim);
        }
    }

    Turn turn(Accepted t) {
        return tasks.get(owner, t.conversationId(), t.turnId());
    }

    Interaction question(Accepted t) {
        assertThat(turn(t).status()).isEqualTo("waiting");
        return governance.interactions(owner, t.conversationId(), t.turnId()).getFirst();
    }

    Answer approve(Interaction i) {
        return new Answer("approval", i.revision(), "approve", i.actionHash(), null);
    }

    AnswerReceipt answer(Accepted t, Interaction i, Answer a) {
        return governance.answer(owner, t.conversationId(), t.turnId(), i.id(), "reply", a);
    }

    int reports() {
        return sql.queryForObject("SELECT count(*) FROM business.reports", Integer.class);
    }

    @Test
    void approvalResumesInNewAttemptAndEffectOccursOnce() throws Exception {
        var t = submit("report");
        var first = claim();
        run(first);
        var i = question(t);
        assertThat(reports()).isZero();
        var signals = taskContext.getBean(com.financeclaw.app.task.WorkSignals.class);
        long before = signals.generation(com.financeclaw.app.task.WorkSignals.Queue.COMMAND);
        assertThat(answer(t, i, approve(i)).idempotentReplay()).isFalse();
        assertThat(signals.generation(com.financeclaw.app.task.WorkSignals.Queue.COMMAND))
                .isEqualTo(before + 1);
        assertThat(answer(t, i, approve(i)).idempotentReplay()).isTrue();
        assertThat(signals.generation(com.financeclaw.app.task.WorkSignals.Queue.COMMAND))
                .isEqualTo(before + 1);
        var resumed = claim();
        assertThat(resumed.scope().key()).isNotEqualTo(first.scope().key());
        run(resumed);
        assertThat(turn(t).status()).isEqualTo("completed");
        assertThat(reports()).isOne();
        answer(t, i, approve(i));
        assertThat(tasks.claim("other", 60000)).isNull();
        assertThat(reports()).isOne();
    }

    @Test
    void approvalAndClarificationSurviveIndependentJvmExit() throws Exception {
        for (String message : List.of("report", "city")) {
            var t = submit(message);
            fork(message + "-suspend");
            var i = question(t);
            answer(
                    t,
                    i,
                    message.equals("report")
                            ? approve(i)
                            : new Answer("input", 1, null, null, Map.of("city", "上海")));
            fork(message + "-resume");
            assertThat(turn(t).status()).isEqualTo("completed");
        }
        assertThat(reports()).isOne();
    }

    @Test
    void replyValidatesOwnershipTypeRevisionHashAndSchema() throws Exception {
        var t = submit("city");
        run(claim());
        var i = question(t);
        assertThatThrownBy(() -> answer(t, i, approve(i))).hasMessage("interaction_mismatch");
        assertThatThrownBy(
                        () ->
                                answer(
                                        t,
                                        i,
                                        new Answer(
                                                "input",
                                                2,
                                                null,
                                                i.actionHash(),
                                                Map.of("city", "上海"))))
                .hasMessage("interaction_mismatch");
        assertThatThrownBy(
                        () ->
                                answer(
                                        t,
                                        i,
                                        new Answer(
                                                "input", 1, null, "forged", Map.of("city", "上海"))))
                .hasMessage("invalid_answer");
        for (Object value :
                List.of(
                        Map.of("city", 42),
                        Map.of("city", ""),
                        Map.of("city", "上海", "tenant", "forged")))
            assertThatThrownBy(() -> answer(t, i, new Answer("input", 1, null, null, value)))
                    .hasMessage("invalid_answer");
        assertThatThrownBy(
                        () ->
                                governance.answer(
                                        new Owner("other", "developer"),
                                        t.conversationId(),
                                        t.turnId(),
                                        i.id(),
                                        "x",
                                        new Answer("input", 1, null, null, Map.of("city", "上海"))))
                .hasMessage("not_found");
        answer(t, i, new Answer("input", 1, null, null, Map.of("city", "上海")));
        run(claim());
        assertThat(turn(t).status()).isEqualTo("completed");
    }

    @Test
    void concurrentDuplicateRepliesEnqueueOnlyOneResume() throws Exception {
        var t = submit("report");
        run(claim());
        var i = question(t);
        try (var pool = Executors.newFixedThreadPool(4)) {
            var futures = new ArrayList<Future<AnswerReceipt>>();
            for (int n = 0; n < 4; n++) futures.add(pool.submit(() -> answer(t, i, approve(i))));
            int originals = 0;
            for (var f : futures) if (!f.get().idempotentReplay()) originals++;
            assertThat(originals).isOne();
        }
        run(claim());
        assertThat(reports()).isOne();
    }

    @Test
    void deniedApprovalDoesNotExecuteWrite() throws Exception {
        var t = submit("report");
        run(claim());
        var i = question(t);
        answer(t, i, new Answer("approval", 1, "reject", i.actionHash(), null));
        run(claim());
        assertThat(turn(t).status()).isEqualTo("failed");
        assertThat(reports()).isZero();
    }

    @Test
    void expiredAndCancelledQuestionsCannotResume() throws Exception {
        var t = submit("report");
        run(claim());
        var i = question(t);
        sql.update(
                "UPDATE business.interactions SET expires_at=clock_timestamp()-interval '1 second' WHERE id=?",
                i.id());
        assertThatThrownBy(() -> answer(t, i, approve(i))).hasMessage("stale_interaction");
        tasks.cancel(owner, t.conversationId(), t.turnId(), "cancel");
        assertThatThrownBy(() -> answer(t, i, approve(i))).hasMessage("stale_interaction");
        assertThat(reports()).isZero();
    }

    @Test
    void revocationBeforeReplyAndAfterReplyBlocksExecution() throws Exception {
        for (boolean before : List.of(true, false)) {
            var t = submit("report");
            run(claim());
            var i = question(t);
            if (!before) answer(t, i, approve(i));
            var grant = governance.grant(owner, t.conversationId());
            governance.changeGrant(
                    owner, t.conversationId(), "revoke", grant.revision(), List.of());
            if (before)
                assertThatThrownBy(() -> answer(t, i, approve(i)))
                        .hasMessage("authorization_changed");
            else {
                run(claim());
                assertThat(turn(t).status()).isEqualTo("blocked");
                assertThat(turn(t).reason()).isEqualTo("authorization_changed");
            }
        }
        assertThat(reports()).isZero();
    }

    @Test
    void changedCheckpointCannotBeApproved() throws Exception {
        var t = submit("report");
        var c = claim();
        run(c);
        var i = question(t);
        sql.update(
                "UPDATE agent_runtime.states SET version=version+1 WHERE namespace=?",
                c.scope().namespace());
        assertThatThrownBy(() -> answer(t, i, approve(i))).hasMessage("checkpoint_mismatch");
    }

    @Test
    void releaseChangeKeepsOriginalTurnAndUnavailableReleaseBlocks() throws Exception {
        var t = submit("report");
        String pinned = turn(t).releaseHash();
        releases.publish(
                new ReleaseDefinition(
                        "m2-new",
                        "finance_agent",
                        List.of(),
                        ReleaseDefinition.OFFLINE,
                        null,
                        3,
                        3,
                        BusinessConfigurationLoader.defaults()));
        run(claim());
        var i = question(t);
        assertThat(i.releaseHash()).isEqualTo(pinned);
        answer(t, i, approve(i));
        run(claim());
        assertThat(reports()).isOne();
        var missing = submit("report");
        sql.update("UPDATE business.turns SET release_hash='missing' WHERE id=?", missing.turnId());
        run(claim());
        assertThat(turn(missing).reason()).isEqualTo("release_unavailable");
    }

    @Test
    void artifactsAreBoundedOwnedRevocableAndObjectsAreNotListSliced() {
        var t = submit("catalog");
        var c = claim();
        var a = governance.archive(c, List.of(Map.of("id", 1), Map.of("id", 2), Map.of("id", 3)));
        var page = governance.read(owner, a.id(), 1, 1);
        assertThat(page.total()).isEqualTo(3);
        assertThat(page.data()).isEqualTo(List.of(Map.of("id", 2)));
        var object = governance.archive(c, Map.of("value", "intact"));
        assertThat(governance.read(owner, object.id(), 999, 1).data())
                .isEqualTo(Map.of("value", "intact"));
        assertThatThrownBy(() -> governance.read(new Owner("other", "developer"), a.id(), 0, 20))
                .hasMessage("not_found");
        assertThatThrownBy(() -> governance.archive(c, Map.of("huge", "x".repeat(2097152))))
                .hasMessage("artifact_too_large");
        governance.changeGrant(owner, t.conversationId(), "revoke", 1, List.of("catalog:read"));
        assertThatThrownBy(() -> governance.read(owner, a.id(), 0, 20))
                .hasMessage("artifact_denied");
    }

    @Test
    void toolProgressNeverContainsArgumentsResultsOrIntermediateReasoning() throws Exception {
        var t = submit("report");
        try (var observer = events.subscribe(turn(t), 128)) {
            var captured =
                    observer.events()
                            .takeUntil(e -> e.status().equals("completed"))
                            .collectList()
                            .toFuture();
            run(claim());
            var i = question(t);
            answer(t, i, approve(i));
            run(claim());
            var liveEvents = captured.get(10, TimeUnit.SECONDS);
            assertThat(liveEvents).anyMatch(e -> e.kind().startsWith("tool_"));
            assertThat(PayloadJson.canonical(liveEvents))
                    .doesNotContain(
                            "PRIVATE_REPORT_PARAMETER", "PRIVATE_INTERMEDIATE_TEXT", "report_id");
            assertThat(liveEvents.stream().filter(e -> e.kind().startsWith("tool_")).toList())
                    .allMatch(e -> e.text() == null && GovernedTools.NAMES.contains(e.tool()));
            assertThat(tasks.events(owner, t.conversationId(), t.turnId(), 0))
                    .allMatch(e -> e.kind().equals("turn"));
        }
    }

    @Test
    void serverIdentityIsFixedAndExecutionBudgetSharedAcrossAttempts() throws Exception {
        releases.publish(
                new ReleaseDefinition(
                        "limited",
                        "finance_agent",
                        ReleaseDefinition.SCOPES,
                        ReleaseDefinition.OFFLINE,
                        null,
                        1,
                        2,
                        BusinessConfigurationLoader.defaults()));
        var t = submit("report");
        var c = claim();
        var identity = tasks.identity(c);
        assertThat(identity.tenant()).isEqualTo("local");
        assertThat(identity.startedAt()).isNotBlank();
        run(c);
        var i = question(t);
        answer(t, i, approve(i));
        run(claim());
        assertThat(db.getTurnPolicy(t.turnId()).modelCalls()).isEqualTo(1);
        assertThat(turn(t).status()).isIn("blocked", "failed");
        assertThat(reports()).isOne();
    }

    @Test
    void httpRejectsInjectedIdentityAndDoesNotExposeInternalInteractionState() throws Exception {
        var t = submit("report");
        run(claim());
        var headers = new HttpHeaders();
        headers.setBearerAuth("m2-test-token");
        var response =
                http.exchange(
                        "/v1/conversations/"
                                + t.conversationId()
                                + "/turns/"
                                + t.turnId()
                                + "/interactions",
                        HttpMethod.GET,
                        new HttpEntity<>(headers),
                        String.class);
        assertThat(response.getStatusCode().value()).isEqualTo(200);
        assertThat(response.getBody())
                .contains("action_hash")
                .doesNotContain("attempt_id", "command_id", "state_version");
    }

    @Test
    void nativeMcpTransportArchivesResultAndRejectsContractDrift() throws Exception {
        try (var server = new MockMcp()) {
            releases.publish(
                    new ReleaseDefinition(
                            "mock-mcp",
                            "finance_agent",
                            ReleaseDefinition.SCOPES,
                            ReleaseDefinition.OFFLINE,
                            null,
                            12,
                            16,
                            new BusinessFeatures(
                                    List.of(
                                            new RemoteEndpoint(
                                                    "hotel",
                                                    server.url(),
                                                    List.of(
                                                            java.net
                                                                    .URI
                                                                    .create(server.url())
                                                                    .getAuthority()),
                                                    "",
                                                    false,
                                                    List.of("local"),
                                                    List.of("hotels"),
                                                    2000)),
                                    PackagedSkills.hash(),
                                    "{}",
                                    null)));
            var t = submit("mcp");
            List<Event> liveEvents;
            try (var observer = events.subscribe(turn(t), 128)) {
                var captured =
                        observer.events()
                                .takeUntil(e -> e.status().equals("completed"))
                                .collectList()
                                .toFuture();
                run(claim());
                liveEvents = captured.get(10, TimeUnit.SECONDS);
            }
            assertThat(turn(t).status()).isEqualTo("completed");
            assertThat(server.calls).hasValue(1);
            String id =
                    sql.queryForObject(
                            "SELECT id FROM business.artifacts WHERE turn_id=?",
                            String.class,
                            t.turnId());
            assertThat(governance.read(owner, id, 100, 1).data())
                    .isEqualTo(Map.of("hotels", List.of(Map.of("city", "上海", "name", "fixture"))));
            assertThat(liveEvents).anyMatch(e -> e.kind().startsWith("tool_"));
            assertThat(PayloadJson.canonical(liveEvents))
                    .doesNotContain("PRIVATE_MCP_RAW_RESULT", "上海", "fixture");
            server.drift = true;
            var changed = submit("mcp");
            run(claim());
            assertThat(turn(changed).status()).isIn("failed", "blocked");
            assertThat(server.calls).hasValue(1);
        }
    }

    @Test
    void allApprovalsInBatchMustArriveBeforeOneResumeIsQueued() throws Exception {
        var t = submit("batch");
        run(claim());
        var questions = governance.interactions(owner, t.conversationId(), t.turnId());
        assertThat(questions).hasSize(2);
        answer(t, questions.get(0), approve(questions.get(0)));
        assertThat(turn(t).status()).isEqualTo("waiting");
        assertThat(claim()).isNull();
        answer(t, questions.get(1), approve(questions.get(1)));
        run(claim());
        assertThat(turn(t).status()).isEqualTo("completed");
        assertThat(reports()).isEqualTo(2);
    }

    @Test
    void allInputAnswersInBatchMustArriveBeforeOneResumeIsQueued() throws Exception {
        var t = submit("city-batch");
        run(claim());
        var questions = governance.interactions(owner, t.conversationId(), t.turnId());
        assertThat(questions).hasSize(2);
        answer(
                t,
                questions.getFirst(),
                new Answer(
                        "input",
                        questions.getFirst().revision(),
                        null,
                        null,
                        Map.of("city", "上海")));
        assertThat(turn(t).status()).isEqualTo("waiting");
        assertThat(claim()).isNull();
        answer(
                t,
                questions.getLast(),
                new Answer(
                        "input", questions.getLast().revision(), null, null, Map.of("city", "北京")));
        run(claim());
        assertThat(turn(t).status()).isEqualTo("completed");
        assertThat(reports()).isZero();
    }

    @Test
    void failedTurnTransitionRollsBackAnswerCommandAndEvents() throws Exception {
        var t = submit("report");
        run(claim());
        var question = question(t);
        var before = turn(t);
        var events = tasks.events(owner, t.conversationId(), t.turnId(), 0);
        sql.execute(
                "ALTER TABLE business.turns ADD CONSTRAINT r3_reject_resume CHECK (status <> 'resuming') NOT VALID");
        try {
            assertThatThrownBy(() -> answer(t, question, approve(question)))
                    .isInstanceOf(org.springframework.dao.DataIntegrityViolationException.class);
            assertPendingAnswerAndDoneCommand(question);
            assertThat(turn(t)).isEqualTo(before);
            assertThat(tasks.events(owner, t.conversationId(), t.turnId(), 0)).isEqualTo(events);
        } finally {
            sql.execute("ALTER TABLE business.turns DROP CONSTRAINT r3_reject_resume");
        }
    }

    @Test
    void failedResumeConditionRollsBackAnswerReceipt() throws Exception {
        var t = submit("report");
        run(claim());
        var question = question(t);
        sql.update("UPDATE business.commands SET status='queued' WHERE id=?", question.commandId());
        assertThatThrownBy(() -> answer(t, question, approve(question)))
                .hasMessage("stale_interaction");
        assertThat(db.getInteractionById(question.id()).status()).isEqualTo("pending");
        assertThat(db.getInteractionById(question.id()).responseHash()).isNull();
        assertThat(turn(t).status()).isEqualTo("waiting");
    }

    @Test
    void answerParticipatesInAnOuterSpringTransaction() throws Exception {
        var t = submit("report");
        run(claim());
        var question = question(t);
        tx.executeWithoutResult(
                status -> {
                    answer(t, question, approve(question));
                    assertThat(db.getInteractionById(question.id()).status()).isEqualTo("answered");
                    assertThat(turn(t).status()).isEqualTo("resuming");
                    status.setRollbackOnly();
                });
        assertPendingAnswerAndDoneCommand(question);
        assertThat(turn(t).status()).isEqualTo("waiting");
    }

    private void assertPendingAnswerAndDoneCommand(Interaction question) {
        assertThat(db.getInteractionById(question.id()).status()).isEqualTo("pending");
        assertThat(db.getInteractionById(question.id()).responseHash()).isNull();
        assertThat(
                        sql.queryForObject(
                                "SELECT status FROM business.commands WHERE id=?",
                                String.class,
                                question.commandId()))
                .isEqualTo("done");
        assertThat(
                        sql.queryForObject(
                                "SELECT resume_id FROM business.commands WHERE id=?",
                                String.class,
                                question.commandId()))
                .isNull();
    }

    @Test
    void nativeAndDirectToolDispatchValidateEffectiveInputBeforeBudgetOrArtifactWrites() {
        var t = submit("catalog");
        var claim = claim();
        try (var tools =
                new GovernedTools(
                        tasks.policy(),
                        tasks.releases(),
                        tasks.credentials(),
                        governance.artifacts(),
                        governance.reports(),
                        claim)) {
            var malformed =
                    io.agentscope.core.message.ToolUseBlock.builder()
                            .id("raw-schema-check")
                            .name("search_catalog")
                            .input(Map.of("city", "上海"))
                            .content("{\"city\":42}")
                            .build();
            assertThat(
                            tools.toolkit
                                    .callTool(
                                            io.agentscope.core.tool.ToolCallParam.builder()
                                                    .toolUseBlock(malformed)
                                                    .input(Map.of("city", "上海"))
                                                    .build())
                                    .block()
                                    .getState())
                    .isEqualTo(io.agentscope.core.message.ToolResultState.ERROR);
            var call =
                    io.agentscope.core.message.ToolUseBlock.builder()
                            .id("schema-check")
                            .name("search_catalog")
                            .input(Map.of("city", "上海"))
                            .content("{\"city\":\"上海\"}")
                            .build();
            var invalid =
                    io.agentscope.core.tool.ToolCallParam.builder()
                            .toolUseBlock(call)
                            .input(Map.of("city", 42))
                            .build();
            assertThat(tools.toolkit.callTool(invalid).block().getState())
                    .isEqualTo(io.agentscope.core.message.ToolResultState.ERROR);
            assertThatThrownBy(
                            () ->
                                    tools.toolkit
                                            .getTool("search_catalog")
                                            .callAsync(invalid)
                                            .block())
                    .hasMessage("invalid_tool_input");
            tools.toolkit
                    .registration()
                    .agentTool(tools.toolkit.getTool("search_catalog"))
                    .presetParameters(Map.of("search_catalog", Map.of("city", 42)))
                    .apply();
            assertThat(
                            tools.toolkit
                                    .callTool(
                                            io.agentscope.core.tool.ToolCallParam.builder()
                                                    .toolUseBlock(call)
                                                    .build())
                                    .block()
                                    .getState())
                    .isEqualTo(io.agentscope.core.message.ToolResultState.ERROR);
            assertThat(db.getTurnPolicy(t.turnId()).toolCalls()).isZero();
            assertThat(
                            sql.queryForObject(
                                    "SELECT count(*) FROM business.artifacts WHERE turn_id=?",
                                    Integer.class,
                                    t.turnId()))
                    .isZero();
        }
    }

    @Test
    void questionGenerationUsesTheSamePendingStatesAsOutcomeAndAnswerBinding() {
        var t = submit("report");
        var claim = claim();
        try (var tools =
                new GovernedTools(
                        tasks.policy(),
                        tasks.releases(),
                        tasks.credentials(),
                        governance.artifacts(),
                        governance.reports(),
                        claim)) {
            var approval =
                    io.agentscope.core.message.ToolUseBlock.builder()
                            .id("approval")
                            .name("create_report")
                            .input(Map.of("title", "test"))
                            .content("{\"title\":\"test\"}")
                            .state(io.agentscope.core.message.ToolCallState.ASKING)
                            .build();
            var input =
                    io.agentscope.core.message.ToolUseBlock.builder()
                            .id("city")
                            .name("request_city")
                            .input(Map.of())
                            .content("{}")
                            .state(io.agentscope.core.message.ToolCallState.SUBMITTED)
                            .build();
            var state =
                    io.agentscope.core.state.AgentState.builder()
                            .addMessage(
                                    io.agentscope.core.message.Msg.builder()
                                            .role(io.agentscope.core.message.MsgRole.ASSISTANT)
                                            .content(List.of(approval, input))
                                            .build())
                            .addMessage(
                                    io.agentscope.core.message.Msg.builder()
                                            .role(io.agentscope.core.message.MsgRole.TOOL)
                                            .content(
                                                    List.of(
                                                            io.agentscope.core.message
                                                                    .ToolResultBlock.of(
                                                                            "approval",
                                                                            "create_report",
                                                                            List.of())
                                                                    .withState(
                                                                            io.agentscope.core
                                                                                    .message
                                                                                    .ToolResultState
                                                                                    .SUCCESS),
                                                            io.agentscope.core.message
                                                                    .ToolResultBlock.of(
                                                                            "city",
                                                                            "request_city",
                                                                            List.of())
                                                                    .withState(
                                                                            io.agentscope.core
                                                                                    .message
                                                                                    .ToolResultState
                                                                                    .RUNNING)))
                                            .build())
                            .build();
            assertThat(TaskAgentRuntime.classify(state, 1).status()).isEqualTo("waiting");
            assertThat(tools.pending(state))
                    .extracting(Pending::kind)
                    .containsExactly("approval", "input");
            assertThat(tools.pending(state))
                    .extracting(Pending::callId)
                    .containsExactly("approval", "city");
            assertThat(db.getTurnPolicy(t.turnId()).toolCalls()).isZero();
            assertThat(reports()).isZero();
        }
    }

    @Test
    void grantUpdatesAreOwnedVersionedAndIdempotent() {
        var t = submit("report");
        var first =
                governance.changeGrant(
                        owner, t.conversationId(), "change", 1, List.of("catalog:read"));
        assertThat(first.revision()).isEqualTo(2);
        assertThat(
                        governance.changeGrant(
                                owner, t.conversationId(), "change", 1, List.of("catalog:read")))
                .isEqualTo(first);
        assertThatThrownBy(
                        () ->
                                governance.changeGrant(
                                        owner, t.conversationId(), "change", 1, List.of()))
                .hasMessage("idempotency_conflict");
        assertThatThrownBy(
                        () ->
                                governance.changeGrant(
                                        owner, t.conversationId(), "next", 1, List.of()))
                .hasMessage("stale_grant");
        assertThatThrownBy(
                        () ->
                                governance.changeGrant(
                                        owner, t.conversationId(), "next", 2, List.of("admin")))
                .hasMessage("invalid_scopes");
    }

    @Test
    void historyContainsOnlyCompletedSameReleaseAndGrantTurns() throws Exception {
        var t = submit("catalog");
        run(claim());
        assertThat(turn(t).status()).isEqualTo("completed");
        var next = tasks.submit(owner, t.conversationId(), "next", "new message");
        var c = claim();
        assertThat(tasks.history(c)).extracting(Turn::id).containsExactly(t.turnId());
        tasks.cancel(owner, next.conversationId(), next.turnId(), "cancel");
        tasks.acknowledgeCancel(c);
        governance.changeGrant(owner, t.conversationId(), "revoke", 1, List.of("catalog:read"));
        tasks.submit(owner, t.conversationId(), "third", "new scope");
        assertThat(tasks.history(claim())).isEmpty();
    }

    @Test
    void httpResponseContractRejectsUnknownFieldsAndAcceptsPinnedApproval() throws Exception {
        var t = submit("report");
        run(claim());
        var i = question(t);
        var headers = new HttpHeaders();
        headers.setBearerAuth("m2-test-token");
        headers.setContentType(MediaType.APPLICATION_JSON);
        headers.set("Idempotency-Key", "http-reply");
        String body =
                PayloadJson.canonical(
                        Map.of(
                                "kind",
                                "approval",
                                "revision",
                                1,
                                "decision",
                                "approve",
                                "action_hash",
                                i.actionHash(),
                                "tenant",
                                "forged"));
        String url = "/v1/interactions/" + i.id() + "/responses";
        assertThat(
                        http.postForEntity(url, new HttpEntity<>(body, headers), String.class)
                                .getStatusCode()
                                .value())
                .isEqualTo(422);
        body =
                PayloadJson.canonical(
                        Map.of(
                                "kind",
                                "approval",
                                "revision",
                                1,
                                "decision",
                                "approve",
                                "action_hash",
                                i.actionHash()));
        assertThat(
                        http.postForEntity(url, new HttpEntity<>(body, headers), String.class)
                                .getStatusCode()
                                .value())
                .isEqualTo(202);
        run(claim());
        assertThat(reports()).isOne();
    }

    @Test
    void delegatedToolsCanOnlyNarrowScopesAndConsumeTheRootBudget() {
        var t = submit("catalog");
        var c = claim();
        try (var root =
                        new GovernedTools(
                                tasks.policy(),
                                tasks.releases(),
                                tasks.credentials(),
                                governance.artifacts(),
                                governance.reports(),
                                c);
                var child = root.narrow(List.of("catalog:read"))) {
            assertThat(child.toolkit.getToolNames())
                    .doesNotContain("create_report", "read_artifact");
            assertThatThrownBy(() -> child.narrow(List.of("report:write")))
                    .hasMessage("scope_expansion_denied");
            var input = Map.<String, Object>of("city", "上海");
            var call =
                    io.agentscope.core.message.ToolUseBlock.builder()
                            .id("child-call")
                            .name("search_catalog")
                            .input(input)
                            .content(PayloadJson.canonical(input))
                            .build();
            child.toolkit
                    .getTool("search_catalog")
                    .callAsync(
                            io.agentscope.core.tool.ToolCallParam.builder()
                                    .toolUseBlock(call)
                                    .input(input)
                                    .build())
                    .block();
            assertThat(db.getTurnPolicy(t.turnId()).toolCalls()).isOne();
        }
    }

    void fork(String scenario) throws Exception {
        Path directory = Path.of("target/m2");
        Files.createDirectories(directory);
        Path config = Files.createTempFile(directory, "connection-", ".properties");
        var p = new Properties();
        p.setProperty("url", PG.getJdbcUrl());
        p.setProperty("user", PG.getUsername());
        p.setProperty("password", PG.getPassword());
        try (var out = Files.newOutputStream(config)) {
            p.store(out, "ephemeral test");
        }
        Path log = directory.resolve("process-" + UUID.randomUUID() + ".log");
        try {
            var process =
                    new ProcessBuilder(
                                    Path.of(System.getProperty("java.home"), "bin/java").toString(),
                                    "-cp",
                                    System.getProperty(
                                            "surefire.test.class.path",
                                            System.getProperty("java.class.path")),
                                    ForkedGovernance.class.getName(),
                                    config.toString())
                            .redirectErrorStream(true)
                            .redirectOutput(log.toFile())
                            .start();
            if (!process.waitFor(30, TimeUnit.SECONDS)) {
                process.destroyForcibly();
                fail("Child timed out: " + log);
            }
            assertThat(process.exitValue()).withFailMessage("Child failed: %s", log).isZero();
            Files.writeString(
                    log.resolveSibling(log.getFileName() + ".exit.json"),
                    PayloadJson.canonical(Map.of("scenario", scenario, "exit", 0)));
        } finally {
            Files.deleteIfExists(config);
        }
    }
}
