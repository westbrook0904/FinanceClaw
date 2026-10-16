package com.financeclaw.m5;

import static org.assertj.core.api.Assertions.*;
import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.*;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.*;

import com.financeclaw.app.operations.AdmissionService;
import com.financeclaw.app.operations.OperationsQueryService;
import com.financeclaw.application.*;
import com.financeclaw.bootstrap.FinanceClawApplication;
import com.financeclaw.domain.Governance.Answer;
import com.financeclaw.domain.Task.*;
import com.financeclaw.infrastructure.agentscope.execution.TaskAgentRuntime;
import com.financeclaw.infrastructure.state.RuntimeMapper;
import com.financeclaw.m2.ScriptedRuntime;
import java.util.*;
import java.util.concurrent.*;
import javax.sql.DataSource;
import org.junit.jupiter.api.*;
import org.junit.jupiter.params.ParameterizedTest;
import org.junit.jupiter.params.provider.ValueSource;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.boot.test.autoconfigure.web.servlet.AutoConfigureMockMvc;
import org.springframework.boot.test.context.SpringBootTest;
import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.test.context.*;
import org.springframework.test.web.servlet.MockMvc;
import org.springframework.transaction.support.TransactionTemplate;
import org.testcontainers.containers.PostgreSQLContainer;

@SpringBootTest(
    classes = FinanceClawApplication.class,
    properties = {
      "financeclaw.worker.enabled=false",
      "financeclaw.integrations.enabled=false",
      "financeclaw.auth.token=m5b-test-only"
    })
@AutoConfigureMockMvc
class AdmissionIT {
  static final PostgreSQLContainer<?> PG = new PostgreSQLContainer<>("postgres:16.10-alpine");

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
  @Autowired org.springframework.context.ApplicationContext taskContext;
  @Autowired GovernanceService governance;
  @Autowired AdmissionService admissions;
  @Autowired OperationsQueryService operations;
  @Autowired TaskAgentRuntime runtime;
  @Autowired RuntimeMapper states;
  @Autowired TransactionTemplate tx;
  @Autowired DataSource source;
  @Autowired MockMvc http;
  final Owner owner = new Owner("local", "developer");
  JdbcTemplate sql;

  @BeforeEach
  void reset() {
    sql = new JdbcTemplate(source);
    sql.execute("TRUNCATE business.conversations,business.admission CASCADE");
    sql.execute("TRUNCATE memory.owners,memory.jobs,memory.operations,memory.audit CASCADE");
  }

  Accepted submit(String message) {
    return tasks.submit(
        owner, tasks.createConversation(owner), UUID.randomUUID().toString(), message);
  }

  void run(TaskAgentRuntime agent) throws Exception {
    try (var worker =
        com.financeclaw.m1.TaskWorkerFixture.create(
            taskContext, agent, false, false, 1, 60000, 2000)) {
      worker.execute(Objects.requireNonNull(tasks.claim("m5b-test", 60000)));
    }
  }

  void project() {
    while (tasks.projectNext()) {}
  }

  int count(String table) {
    return sql.queryForObject("SELECT count(*) FROM " + table, Integer.class);
  }

  @Test
  void authenticatedApiRejectsIdentityAndTypeInjectionAndDoesNotChangeReadiness() throws Exception {
    http.perform(get("/v1/operations/admission")).andExpect(status().isUnauthorized());
    http.perform(get("/v1/operations/drain")).andExpect(status().isUnauthorized());
    http.perform(
            put("/v1/operations/admission")
                .contentType("application/json")
                .content("{\"paused\":true,\"revision\":0}"))
        .andExpect(status().isUnauthorized());
    http.perform(get("/v1/operations/admission").header("Authorization", "Bearer m5b-test-only"))
        .andExpect(status().isOk())
        .andExpect(jsonPath("$.paused").value(false))
        .andExpect(jsonPath("$.revision").value(0));
    assertThat(count("business.admission")).isZero();
    for (String body :
        List.of(
            "{}",
            "{\"paused\":\"true\",\"revision\":0}",
            "{\"paused\":true,\"revision\":0.5}",
            "{\"paused\":true,\"revision\":-1}",
            "{\"paused\":true,\"revision\":9223372036854775808}",
            "{\"paused\":true,\"revision\":0,\"tenant\":\"other\"}")) {
      http.perform(
              put("/v1/operations/admission")
                  .header("Authorization", "Bearer m5b-test-only")
                  .header("Idempotency-Key", "invalid")
                  .contentType("application/json")
                  .content(body))
          .andExpect(status().isUnprocessableEntity());
    }
    http.perform(
            put("/v1/operations/admission")
                .header("Authorization", "Bearer m5b-test-only")
                .contentType("application/json")
                .content("{\"paused\":true,\"revision\":0}"))
        .andExpect(status().isUnprocessableEntity());
    http.perform(
            put("/v1/operations/admission")
                .header("Authorization", "Bearer m5b-test-only")
                .header("Idempotency-Key", "pause")
                .contentType("application/json")
                .content("{\"paused\":true,\"revision\":0}"))
        .andExpect(status().isOk())
        .andExpect(jsonPath("$.revision").value(1));
    String c = tasks.createConversation(owner);
    http.perform(
            post("/v1/conversations/" + c + "/turns")
                .header("Authorization", "Bearer m5b-test-only")
                .header("Idempotency-Key", "new")
                .contentType("application/json")
                .content("{\"message\":\"hello\"}"))
        .andExpect(status().isServiceUnavailable())
        .andExpect(jsonPath("$.error").value("admission_paused"));
    http.perform(get("/actuator/health/readiness")).andExpect(status().isOk());
    assertThat(count("business.turns")).isZero();
    assertThat(count("business.commands")).isZero();
    assertThat(count("business.journal")).isZero();
  }

  @Test
  void controlReceiptsAreIdempotentAndStaleChangesCannotReopenAdmission() {
    var paused = admissions.change(owner, "pause", true, 0);
    assertThat(paused.revision()).isEqualTo(1);
    assertThat(admissions.change(owner, "pause", true, 0).idempotentReplay()).isTrue();
    assertThatThrownBy(() -> admissions.change(owner, "pause", false, 0))
        .hasMessage("idempotency_conflict");
    assertThatThrownBy(() -> admissions.change(owner, "stale", false, 0))
        .hasMessage("admission_revision_changed");
    admissions.change(owner, "resume", false, 1);
    // An old pause receipt remains the old result; it does not pause a second time.
    assertThat(admissions.change(owner, "pause", true, 0).paused()).isTrue();
    assertThat(admissions.admission(owner).paused()).isFalse();
    assertThat(admissions.admission(owner).revision()).isEqualTo(2);
    assertThat(count("business.admission_changes")).isEqualTo(2);
    assertThat(operations.drain(owner).drained()).isFalse();
    submit("accepted after resume");
  }

  @Test
  void duplicateSubmissionStillReturnsOriginalReceiptAfterPauseAndCancellation() {
    String c = tasks.createConversation(owner);
    var first = tasks.submit(owner, c, "original", "same-message");
    admissions.change(owner, "pause", true, 0);
    assertThat(tasks.submit(owner, c, "original", "same-message").turnId())
        .isEqualTo(first.turnId());
    assertThatThrownBy(() -> tasks.submit(owner, c, "original", "changed"))
        .hasMessage("idempotency_conflict");
    tasks.cancel(owner, c, first.turnId(), "cancel");
    assertThat(tasks.submit(owner, c, "original", "same-message").idempotentReplay()).isTrue();
    assertThat(count("business.commands")).isEqualTo(1);
    project();
    assertThat(operations.drain(owner).drained()).isTrue();
  }

  @Test
  void ownerScopedPauseAndReportDoNotBlockOrRevealOtherOwners() {
    admissions.change(owner, "pause", true, 0);
    for (var other : List.of(new Owner("local", "other"), new Owner("other", "developer"))) {
      tasks.submit(other, tasks.createConversation(other), "same-key", "private-other-message");
      assertThat(admissions.admission(other).paused()).isFalse();
      assertThat(operations.drain(other).counts()).containsEntry("queued", 1L);
    }
    assertThat(operations.drain(owner).counts().values()).allMatch(n -> n == 0);
    assertThat(operations.drain(owner).drained()).isTrue();
    assertThat(operations.drain(owner).scope()).isEqualTo("current_owner");
  }

  @Test
  void queuedWorkContinuesAndCompletionRequiresOutboxDrain() throws Exception {
    var accepted = submit("offline-work");
    admissions.change(owner, "pause", true, 0);
    assertThat(operations.drain(owner).counts())
        .containsEntry("queued", 1L)
        .containsEntry("active_commands", 1L);
    run(runtime);
    assertThat(tasks.get(owner, accepted.conversationId(), accepted.turnId()).status())
        .isEqualTo("completed");
    assertThat(operations.drain(owner).counts()).containsEntry("pending_outbox", 1L);
    assertThat(operations.drain(owner).drained()).isFalse();
    project();
    assertThat(operations.drain(owner).drained()).isTrue();
  }

  @ParameterizedTest
  @ValueSource(strings = {"city", "write"})
  void nativeClarificationAndApprovalCanFinishWhileNewTasksArePaused(String message)
      throws Exception {
    var accepted = submit(message);
    var scripted =
        new ScriptedRuntime(
            tasks,
            governance,
            states,
            taskContext.getBean(
                com.financeclaw.infrastructure.agentscope.capability.BusinessCapabilities.class));
    run(scripted);
    admissions.change(owner, "pause", true, 0);
    assertThat(operations.drain(owner).counts())
        .containsEntry("waiting", 1L)
        .containsEntry("pending_interactions", 1L);
    var question =
        governance.interactions(owner, accepted.conversationId(), accepted.turnId()).getFirst();
    var answer =
        message.equals("city")
            ? new Answer("input", question.revision(), null, null, Map.of("city", "上海"))
            : new Answer("approval", question.revision(), "approve", question.actionHash(), null);
    governance.answer(owner, question.id(), "answer", answer);
    assertThat(operations.drain(owner).counts()).containsEntry("queued", 1L);
    run(scripted);
    assertThat(tasks.get(owner, accepted.conversationId(), accepted.turnId()).status())
        .isEqualTo("completed");
    assertThat(count("business.turns")).isEqualTo(1);
    if (message.equals("write")) assertThat(count("business.reports")).isEqualTo(1);
    project();
    assertThat(operations.drain(owner).drained()).isTrue();
  }

  @Test
  void cancellationInProgressAndUnknownEffectsNeverCountAsDrained() {
    var t = submit("running");
    var claim = tasks.claim("test", 60000);
    admissions.change(owner, "pause", true, 0);
    tasks.cancel(owner, t.conversationId(), t.turnId(), "cancel");
    assertThat(operations.drain(owner).counts())
        .containsEntry("cancelling", 1L)
        .containsEntry("active_commands", 1L);
    tasks.acknowledgeCancel(claim);
    project();
    assertThat(operations.drain(owner).drained()).isTrue();
    sql.update(
        "INSERT INTO business.tool_calls(turn_id,call_id,action_hash,status) VALUES(?,?,?,'pending')",
        t.turnId(),
        "uncertain",
        "synthetic");
    assertThat(operations.drain(owner).counts()).containsEntry("uncertain_effects", 1L);
    assertThat(operations.drain(owner).drained()).isFalse();
  }

  @Test
  void unprojectedUnverifiedNotificationsAndMemoryWritesRemainVisible() {
    var t = submit("private-not-in-operating-report");
    admissions.change(owner, "pause", true, 0);
    tasks.cancel(owner, t.conversationId(), t.turnId(), "cancel");
    project();
    sql.update(
        "INSERT INTO channel.bindings(id,tenant_key,open_id,chat_id,tenant,subject,conversation_id,active_turn) VALUES('binding','synthetic','user','chat',?,?,?,?)",
        owner.tenant(),
        owner.subject(),
        t.conversationId(),
        t.turnId());
    assertThat(operations.drain(owner).counts()).containsEntry("unprojected_notifications", 1L);
    long revision = tasks.get(owner, t.conversationId(), t.turnId()).revision();
    sql.update(
        "INSERT INTO channel.notices(id,turn_id,binding_id,binding_revision,revision,payload,status) VALUES('notice',?,'binding',1,?,'private-notice','pending')",
        t.turnId(),
        revision);
    assertThat(operations.drain(owner).counts())
        .containsEntry("unprojected_notifications", 0L)
        .containsEntry("pending_notifications", 1L);
    for (String status : List.of("sending", "unknown", "submitted_unverified")) {
      sql.update("UPDATE channel.notices SET status=?", status);
      assertThat(operations.drain(owner).drained()).isFalse();
      assertThat(operations.drain(owner).counts())
          .containsEntry(
              status.equals("sending") ? "pending_notifications" : "unverified_notifications", 1L);
    }
    sql.update("UPDATE channel.notices SET status='suppressed'");
    assertThat(operations.drain(owner).counts()).containsEntry("unprojected_notifications", 1L);
    sql.update("UPDATE channel.bindings SET muted=true");
    assertThat(operations.drain(owner).drained()).isTrue();
    sql.update(
        "INSERT INTO memory.owners(tenant,subject,purge_pending) VALUES(?,?,true)",
        owner.tenant(),
        owner.subject());
    sql.update(
        "INSERT INTO memory.jobs(kind,key_id,tenant,subject,epoch) VALUES('extract','synthetic',?,?,1)",
        owner.tenant(),
        owner.subject());
    assertThat(operations.drain(owner).counts())
        .containsEntry("pending_memory_jobs", 1L)
        .containsEntry("pending_forget", 1L);
    assertThat(operations.drain(owner).drained()).isFalse();
  }

  @ParameterizedTest
  @ValueSource(booleans = {true, false})
  void pauseAndActualAdmissionTransactionsHaveOneOrdering(boolean pauseFirst) throws Exception {
    admissions.change(owner, "initial", false, 0);
    String c = tasks.createConversation(owner);
    var held = new CountDownLatch(1);
    var release = new CountDownLatch(1);
    try (var executor = Executors.newVirtualThreadPerTaskExecutor()) {
      var first =
          executor.submit(
              () ->
                  tx.execute(
                      s -> {
                        if (pauseFirst) admissions.change(owner, "pause", true, 1);
                        else tasks.submit(owner, c, "new", "message");
                        held.countDown();
                        await(release);
                        return true;
                      }));
      assertThat(held.await(10, TimeUnit.SECONDS)).isTrue();
      var second =
          executor.submit(
              () -> {
                if (!pauseFirst) return admissions.change(owner, "pause", true, 1);
                try {
                  return tasks.submit(owner, c, "new", "message");
                } catch (Problem problem) {
                  return problem.getMessage();
                }
              });
      try {
        long until = System.nanoTime() + TimeUnit.SECONDS.toNanos(2);
        boolean waiting = false;
        while (!waiting && System.nanoTime() < until) {
          waiting =
              Boolean.TRUE.equals(
                  sql.queryForObject(
                      "SELECT EXISTS(SELECT 1 FROM pg_stat_activity WHERE datname=current_database() AND wait_event_type='Lock' AND query LIKE '%business.admission%')",
                      Boolean.class));
          if (!waiting) Thread.sleep(10);
        }
        assertThat(waiting).as("real admission control lock conflict").isTrue();
        assertThat(second.isDone()).isFalse();
      } finally {
        release.countDown();
      }
      first.get(10, TimeUnit.SECONDS);
      Object result = second.get(10, TimeUnit.SECONDS);
      if (pauseFirst) assertThat(result).isEqualTo("admission_paused");
      assertThat(count("business.turns")).isEqualTo(pauseFirst ? 0 : 1);
      assertThat(admissions.admission(owner).paused()).isTrue();
      assertThatThrownBy(() -> submit("later")).hasMessage("admission_paused");
    } finally {
      release.countDown();
    }
  }

  static void await(CountDownLatch latch) {
    try {
      if (!latch.await(10, TimeUnit.SECONDS)) throw new AssertionError("barrier timeout");
    } catch (InterruptedException e) {
      throw new AssertionError(e);
    }
  }

  @Test
  void admissionUsesSharedLocksAcrossIndependentConversations() throws Exception {
    admissions.change(owner, "initial", false, 0);
    var held = new CountDownLatch(1);
    var release = new CountDownLatch(1);
    try (var executor = Executors.newVirtualThreadPerTaskExecutor()) {
      var first =
          executor.submit(
              () ->
                  tx.execute(
                      s -> {
                        submit("first");
                        held.countDown();
                        await(release);
                        return true;
                      }));
      assertThat(held.await(10, TimeUnit.SECONDS)).isTrue();
      try {
        assertThat(executor.submit(() -> submit("second")).get(2, TimeUnit.SECONDS)).isNotNull();
      } finally {
        release.countDown();
      }
      first.get(10, TimeUnit.SECONDS);
      assertThat(count("business.turns")).isEqualTo(2);
    } finally {
      release.countDown();
    }
  }

  @Test
  void concurrentControlRetriesWriteOneAuditReceipt() throws Exception {
    try (var executor = Executors.newVirtualThreadPerTaskExecutor()) {
      var futures = new ArrayList<Future<com.financeclaw.domain.Operations.Receipt>>();
      for (int i = 0; i < 6; i++)
        futures.add(executor.submit(() -> admissions.change(owner, "same", true, 0)));
      int replay = 0;
      for (var f : futures) if (f.get(10, TimeUnit.SECONDS).idempotentReplay()) replay++;
      assertThat(replay).isEqualTo(5);
      assertThat(count("business.admission_changes")).isEqualTo(1);
      assertThat(admissions.admission(owner).revision()).isEqualTo(1);
    }
  }

  @ParameterizedTest
  @ValueSource(booleans = {false, true})
  void receiptFailureRollsBackTheAdmissionRowAndItsRevision(boolean existing) {
    if (existing) admissions.change(owner, "initial", false, 0);
    long revision = existing ? 1 : 0;
    sql.execute(
        "ALTER TABLE business.admission_changes ADD CONSTRAINT r2_receipt_failure CHECK (idempotency_key <> 'fail-receipt')");
    try {
      assertThatThrownBy(() -> admissions.change(owner, "fail-receipt", true, revision))
          .isInstanceOf(org.springframework.dao.DataIntegrityViolationException.class);
      assertThat(admissions.admission(owner))
          .isEqualTo(new com.financeclaw.domain.Operations.Admission(false, revision));
      assertThat(count("business.admission")).isEqualTo(existing ? 1 : 0);
      assertThat(count("business.admission_changes")).isEqualTo(existing ? 1 : 0);
    } finally {
      sql.execute("ALTER TABLE business.admission_changes DROP CONSTRAINT r2_receipt_failure");
    }
    var retry = admissions.change(owner, "fail-receipt", true, revision);
    assertThat(retry.idempotentReplay()).isFalse();
    assertThat(retry.revision()).isEqualTo(revision + 1);
  }

  @Test
  void admissionAndReceiptJoinAnExistingSpringTransaction() {
    tx.executeWithoutResult(
        status -> {
          admissions.change(owner, "outer", true, 0);
          assertThat(admissions.admission(owner).paused()).isTrue();
          status.setRollbackOnly();
        });
    assertThat(count("business.admission")).isZero();
    assertThat(count("business.admission_changes")).isZero();
  }
}
