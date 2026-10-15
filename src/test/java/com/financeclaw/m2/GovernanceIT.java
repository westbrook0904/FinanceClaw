package com.financeclaw.m2;

import static org.assertj.core.api.Assertions.*;

import com.financeclaw.agent.*;
import com.financeclaw.application.*;
import com.financeclaw.bootstrap.FinanceClawApplication;
import com.financeclaw.domain.Governance.*;
import com.financeclaw.domain.Task.*;
import com.financeclaw.infrastructure.governance.GovernanceMapper;
import com.financeclaw.infrastructure.state.RuntimeMapper;
import com.financeclaw.tools.BusinessCatalog;
import com.financeclaw.worker.TaskWorker;
import java.nio.file.*;
import java.util.*;
import java.util.concurrent.*;
import javax.sql.DataSource;
import org.junit.jupiter.api.*;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.boot.test.context.SpringBootTest;
import org.springframework.boot.test.web.client.TestRestTemplate;
import org.springframework.http.*;
import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.test.context.DynamicPropertyRegistry;
import org.springframework.test.context.DynamicPropertySource;
import org.testcontainers.containers.PostgreSQLContainer;

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

  @Autowired TaskService tasks;
  @Autowired GovernanceService governance;
  @Autowired GovernanceMapper db;
  @Autowired RuntimeMapper states;
  @Autowired ReleaseCatalog releases;
  @Autowired DataSource source;
  @Autowired TestRestTemplate http;
  JdbcTemplate sql;
  final Owner owner = new Owner("local", "developer");

  @BeforeEach
  void reset() {
    sql = new JdbcTemplate(source);
    sql.execute("TRUNCATE business.conversations CASCADE");
    releases.publish(
        new ReleaseCatalog.Release(
            "m2-v1", "finance_agent", ReleaseCatalog.SCOPES, ReleaseCatalog.OFFLINE, null, 12, 16));
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
        new TaskWorker(
            tasks, new ScriptedRuntime(tasks, governance, states), false, false, 1, 60000, 2000)) {
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
    assertThat(answer(t, i, approve(i)).idempotentReplay()).isFalse();
    assertThat(answer(t, i, approve(i)).idempotentReplay()).isTrue();
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
            () -> answer(t, i, new Answer("input", 2, null, i.actionHash(), Map.of("city", "上海"))))
        .hasMessage("interaction_mismatch");
    assertThatThrownBy(
            () -> answer(t, i, new Answer("input", 1, null, "forged", Map.of("city", "上海"))))
        .hasMessage("invalid_answer");
    for (Object value :
        List.of(Map.of("city", 42), Map.of("city", ""), Map.of("city", "上海", "tenant", "forged")))
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
      governance.changeGrant(owner, t.conversationId(), "revoke", grant.revision(), List.of());
      if (before)
        assertThatThrownBy(() -> answer(t, i, approve(i))).hasMessage("authorization_changed");
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
        new ReleaseCatalog.Release(
            "m2-new", "finance_agent", List.of(), ReleaseCatalog.OFFLINE, null, 3, 3));
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
    assertThatThrownBy(() -> governance.read(owner, a.id(), 0, 20)).hasMessage("artifact_denied");
  }

  @Test
  void unknownWriteOutcomeBlocksCancellationAndReplay() {
    var t = submit("report");
    var c = claim();
    db.beginEffect(t.turnId(), "uncertain", "hash");
    assertThatThrownBy(() -> tasks.checkDispatch(c)).hasMessage("reconciliation_required");
    assertThatThrownBy(() -> tasks.cancel(owner, t.conversationId(), t.turnId(), "cancel"))
        .hasMessage("reconciliation_required");
  }

  @Test
  void toolProgressNeverContainsArgumentsResultsOrIntermediateReasoning() throws Exception {
    var t = submit("report");
    run(claim());
    var i = question(t);
    answer(t, i, approve(i));
    run(claim());
    var events = tasks.events(owner, t.conversationId(), t.turnId(), 0);
    assertThat(events).anyMatch(e -> e.kind().startsWith("tool_"));
    assertThat(ResumeInputs.canonical(events))
        .doesNotContain("PRIVATE_REPORT_PARAMETER", "PRIVATE_INTERMEDIATE_TEXT", "report_id");
    assertThat(events.stream().filter(e -> e.kind().startsWith("tool_")).toList())
        .allMatch(e -> e.text() == null && GovernedTools.NAMES.contains(e.tool()));
  }

  @Test
  void serverIdentityIsFixedAndExecutionBudgetSharedAcrossAttempts() throws Exception {
    releases.publish(
        new ReleaseCatalog.Release(
            "limited", "finance_agent", ReleaseCatalog.SCOPES, ReleaseCatalog.OFFLINE, null, 1, 2));
    var t = submit("report");
    var c = claim();
    var identity = tasks.identity(c);
    assertThat(identity.tenant()).isEqualTo("local");
    assertThat(identity.startedAt()).isNotBlank();
    run(c);
    var i = question(t);
    answer(t, i, approve(i));
    run(claim());
    assertThat(db.policy(t.turnId()).modelCalls()).isEqualTo(1);
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
            "/v1/conversations/" + t.conversationId() + "/turns/" + t.turnId() + "/interactions",
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
          new ReleaseCatalog.Release(
              "mock-mcp",
              "finance_agent",
              ReleaseCatalog.SCOPES,
              ReleaseCatalog.OFFLINE,
              null,
              12,
              16,
              new BusinessCatalog.Features(
                  List.of(
                      new BusinessCatalog.Remote(
                          "hotel",
                          server.url(),
                          List.of(java.net.URI.create(server.url()).getAuthority()),
                          "",
                          false,
                          List.of("local"),
                          List.of("hotels"),
                          2000)),
                  BusinessCatalog.skillHash(),
                  "{}")));
      var t = submit("mcp");
      run(claim());
      assertThat(turn(t).status()).isEqualTo("completed");
      assertThat(server.calls).hasValue(1);
      String id =
          sql.queryForObject(
              "SELECT id FROM business.artifacts WHERE turn_id=?", String.class, t.turnId());
      assertThat(governance.read(owner, id, 100, 1).data())
          .isEqualTo(Map.of("hotels", List.of(Map.of("city", "上海", "name", "fixture"))));
      assertThat(ResumeInputs.canonical(tasks.events(owner, t.conversationId(), t.turnId(), 0)))
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
  void grantUpdatesAreOwnedVersionedAndIdempotent() {
    var t = submit("report");
    var first =
        governance.changeGrant(owner, t.conversationId(), "change", 1, List.of("catalog:read"));
    assertThat(first.revision()).isEqualTo(2);
    assertThat(
            governance.changeGrant(owner, t.conversationId(), "change", 1, List.of("catalog:read")))
        .isEqualTo(first);
    assertThatThrownBy(
            () -> governance.changeGrant(owner, t.conversationId(), "change", 1, List.of()))
        .hasMessage("idempotency_conflict");
    assertThatThrownBy(
            () -> governance.changeGrant(owner, t.conversationId(), "next", 1, List.of()))
        .hasMessage("stale_grant");
    assertThatThrownBy(
            () -> governance.changeGrant(owner, t.conversationId(), "next", 2, List.of("admin")))
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
        ResumeInputs.canonical(
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
        ResumeInputs.canonical(
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
    try (var root = new GovernedTools(tasks, governance, c);
        var child = root.narrow(List.of("catalog:read"))) {
      assertThat(child.toolkit.getToolNames()).doesNotContain("create_report", "read_artifact");
      assertThatThrownBy(() -> child.narrow(List.of("report:write")))
          .hasMessage("scope_expansion_denied");
      var input = Map.<String, Object>of("city", "上海");
      var call =
          io.agentscope.core.message.ToolUseBlock.builder()
              .id("child-call")
              .name("search_catalog")
              .input(input)
              .content(ResumeInputs.canonical(input))
              .build();
      child
          .toolkit
          .getTool("search_catalog")
          .callAsync(
              io.agentscope.core.tool.ToolCallParam.builder()
                  .toolUseBlock(call)
                  .input(input)
                  .build())
          .block();
      assertThat(db.policy(t.turnId()).toolCalls()).isOne();
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
                      "surefire.test.class.path", System.getProperty("java.class.path")),
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
          ResumeInputs.canonical(Map.of("scenario", scenario, "exit", 0)));
    } finally {
      Files.deleteIfExists(config);
    }
  }
}
