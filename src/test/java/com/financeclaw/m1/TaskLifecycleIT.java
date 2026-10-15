package com.financeclaw.m1;

import static org.assertj.core.api.Assertions.*;

import com.financeclaw.agent.TaskAgentRuntime;
import com.financeclaw.application.TaskService;
import com.financeclaw.bootstrap.FinanceClawApplication;
import com.financeclaw.domain.Task.*;
import com.financeclaw.infrastructure.state.*;
import com.financeclaw.infrastructure.task.TaskMapper;
import com.financeclaw.worker.TaskWorker;
import io.agentscope.core.message.*;
import io.agentscope.core.model.*;
import io.agentscope.core.state.AgentState;
import java.nio.file.*;
import java.util.*;
import java.util.concurrent.*;
import javax.sql.DataSource;
import org.junit.jupiter.api.*;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.boot.test.context.SpringBootTest;
import org.springframework.boot.test.web.client.TestRestTemplate;
import org.springframework.http.*;
import org.springframework.test.context.DynamicPropertyRegistry;
import org.springframework.test.context.DynamicPropertySource;
import org.testcontainers.containers.PostgreSQLContainer;
import reactor.core.publisher.Flux;

@SpringBootTest(
    classes = FinanceClawApplication.class,
    webEnvironment = SpringBootTest.WebEnvironment.RANDOM_PORT,
    properties = {
      "financeclaw.worker.enabled=false",
      "financeclaw.integrations.enabled=false",
      "financeclaw.auth.token=m1-test-token",
      "financeclaw.sse.timeout-ms=500",
      "financeclaw.sse.poll-ms=20"
    })
class TaskLifecycleIT {
  static final PostgreSQLContainer<?> PG =
      new PostgreSQLContainer<>("postgres:16.10-alpine")
          .withDatabaseName("m1")
          .withUsername("m1")
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
  @Autowired com.financeclaw.application.GovernanceService governance;
  @Autowired TaskAgentRuntime runtime;
  @Autowired TaskWorker worker;
  @Autowired RuntimeMapper states;
  @Autowired TaskMapper mapper;
  @Autowired DataSource source;
  @Autowired TestRestTemplate http;
  LifecycleProbeMapper probe;
  final Owner owner = new Owner("local", "developer");

  @BeforeEach
  void reset() {
    probe = new LifecycleDatabase(source).session.getMapper(LifecycleProbeMapper.class);
    probe.reset();
  }

  Accepted submit(String conversation, String key) {
    return tasks.submit(owner, conversation, key, "hello");
  }

  Accepted submit() {
    return submit(tasks.createConversation(owner), UUID.randomUUID().toString());
  }

  Turn turn(Accepted t) {
    return tasks.get(owner, t.conversationId(), t.turnId());
  }

  Claim claim() {
    return tasks.claim("test-worker", 60000);
  }

  void run(Claim c) {
    worker.execute(c);
  }

  @Test
  void T01_T02_replaysOriginalAdmissionAndRejectsChangedBodyUnderRace() throws Exception {
    String conversation = tasks.createConversation(owner);
    try (var pool = Executors.newFixedThreadPool(8)) {
      List<Future<Accepted>> results = new ArrayList<>();
      for (int i = 0; i < 8; i++) results.add(pool.submit(() -> submit(conversation, "same")));
      Set<String> ids = new HashSet<>();
      for (var r : results) ids.add(r.get().turnId());
      assertThat(ids).hasSize(1);
      assertThat(probe.commands()).isEqualTo(1);
    }
    run(claim());
    var replay = submit(conversation, "same");
    assertThat(replay.idempotentReplay()).isTrue();
    assertThat(replay.status()).isEqualTo("queued");
    assertThat(replay.revision()).isEqualTo(1);
    assertThatThrownBy(() -> tasks.submit(owner, conversation, "same", "changed"))
        .isInstanceOf(Problem.class)
        .hasMessage("idempotency_conflict");
    assertThat(probe.answers()).isEqualTo(1);
  }

  @Test
  void T03_sameConversationSerializesAdmissionDifferentConversationsExecuteInParallel()
      throws Exception {
    String conversation = tasks.createConversation(owner);
    try (var pool = Executors.newFixedThreadPool(2)) {
      CyclicBarrier barrier = new CyclicBarrier(2);
      Callable<Boolean> attempt =
          () -> {
            barrier.await();
            try {
              submit(conversation, UUID.randomUUID().toString());
              return true;
            } catch (Problem p) {
              assertThat(p.getMessage()).isEqualTo("conversation_busy");
              return false;
            }
          };
      var a = pool.submit(attempt);
      var b = pool.submit(attempt);
      assertThat(List.of(a.get(), b.get())).containsExactlyInAnyOrder(true, false);
      submit();
      Claim c1 = claim(), c2 = claim();
      CountDownLatch both = new CountDownLatch(2);
      TaskAgentRuntime concurrent =
          new TaskAgentRuntime(tasks, governance, states, 5000) {
            @Override
            protected Model model(Claim c) {
              return scripted(
                  () -> {
                    both.countDown();
                    try {
                      if (!both.await(3, TimeUnit.SECONDS))
                        throw new IllegalStateException("not parallel");
                    } catch (InterruptedException e) {
                      throw new RuntimeException(e);
                    }
                  },
                  "done");
            }
          };
      var one = pool.submit(() -> execute(concurrent, c1));
      var two = pool.submit(() -> execute(concurrent, c2));
      one.get();
      two.get();
      assertThat(mapper.turn(c1.turn().id()).status()).isEqualTo("completed");
      assertThat(mapper.turn(c2.turn().id()).status()).isEqualTo("completed");
      assertThat(c1.scope().sessionId()).isNotEqualTo(c2.scope().sessionId());
    }
  }

  @Test
  void T04_queuedCommandSurvivesNewJvm() throws Exception {
    var t = submit();
    fork("normal", 0);
    assertThat(turn(t).status()).isEqualTo("completed");
    assertThat(probe.answers()).isEqualTo(1);
  }

  @Test
  void T05_claimedCrashBlocksAndFencesLateSnapshotAndCompletion() throws Exception {
    var t = submit();
    fork("after-claim", 73);
    var c = mapper.lockTurnCommand(t.turnId());
    var old = new Claim(c, turn(t));
    probe.expire();
    fork("recover", 0);
    assertThat(turn(t).status()).isEqualTo("blocked");
    assertThatThrownBy(
            () ->
                tasks.fenced(
                    old,
                    () ->
                        new MyBatisAgentStateStore(states, old.scope())
                            .saveIfVersion(
                                old.scope().userId(),
                                old.scope().sessionId(),
                                "agent_state",
                                AgentState.builder().build(),
                                0)))
        .isInstanceOf(Fenced.class);
    assertThatThrownBy(() -> tasks.checkDispatch(old)).isInstanceOf(Fenced.class);
    assertThatThrownBy(() -> tasks.fail(old)).isInstanceOf(Fenced.class);
    assertThat(probe.answers()).isZero();
    assertThat(probe.attempts()).isEqualTo(1);
  }

  @Test
  void T08_sealedSnapshotSurvivesCrashAndCompletesWithoutModelReplay() throws Exception {
    var t = submit();
    fork("after-seal", 73);
    assertThat(turn(t).status()).isEqualTo("running");
    probe.expire();
    fork("recover", 0);
    fork("recover", 0);
    assertThat(turn(t).status()).isEqualTo("completed");
    assertThat(turn(t).result()).isEqualTo("已收到：hello");
    assertThat(probe.attempts()).isEqualTo(1);
    assertThat(probe.answers()).isEqualTo(1);
    assertThat(probe.outbox()).isEqualTo(1);
    assertThat(tasks.claim("other", 60000)).isNull();
  }

  @Test
  void unsealedSdkSaveIsConservativelyBlockedWithoutReplay() throws Exception {
    var t = submit();
    fork("after-save", 73);
    probe.expire();
    fork("recover", 0);
    assertThat(turn(t).status()).isEqualTo("blocked");
    assertThat(probe.answers()).isZero();
    assertThat(probe.attempts()).isEqualTo(1);
  }

  @Test
  void T07_pendingToolEvenWithAssistantTextCannotComplete() {
    var t = submit();
    var c = claim();
    var use =
        ToolUseBlock.builder().id("pending").name("approval").input(Map.of()).content("{}").build();
    var state =
        AgentState.builder()
            .userId(c.scope().userId())
            .sessionId(c.scope().sessionId())
            .addMessage(
                Msg.builder()
                    .role(MsgRole.ASSISTANT)
                    .content(use, TextBlock.builder().text("looks done").build())
                    .build())
            .build();
    long version =
        tasks.fenced(
            c,
            () ->
                new MyBatisAgentStateStore(states, c.scope())
                    .saveIfVersion(
                        c.scope().userId(), c.scope().sessionId(), "agent_state", state, 0));
    var outcome = TaskAgentRuntime.classify(state, version);
    assertThat(outcome.status()).isEqualTo("waiting");
    tasks.seal(c, outcome);
    tasks.finish(c);
    assertThat(turn(t).status()).isEqualTo("waiting");
    assertThat(probe.answers()).isZero();
    assertThat(probe.outbox()).isZero();
  }

  @Test
  void T09_cancelAndCompletionRaceHasOneTerminalAndOneOutbox() throws Exception {
    for (int i = 0; i < 8; i++) {
      var t = submit();
      var c = claim();
      tasks.seal(c, runtime.execute(c));
      try (var pool = Executors.newFixedThreadPool(2)) {
        CyclicBarrier barrier = new CyclicBarrier(2);
        var a =
            pool.submit(
                () -> {
                  barrier.await();
                  tasks.cancel(owner, t.conversationId(), t.turnId(), "cancel");
                  return null;
                });
        var b =
            pool.submit(
                () -> {
                  barrier.await();
                  tasks.finish(c);
                  return null;
                });
        a.get();
        b.get();
      }
      assertThat(turn(t).status()).isIn("completed", "cancelled");
      assertThat(
              tasks.events(owner, t.conversationId(), t.turnId(), 0).stream()
                  .filter(e -> Set.of("completed", "cancelled").contains(e.status()))
                  .count())
          .isEqualTo(1);
    }
    assertThat(probe.outbox()).isEqualTo(8);
  }

  @Test
  void runningCancellationInterruptsOnlyItsSdkAndWaitsForAcknowledgement() throws Exception {
    var t = submit();
    var c = claim();
    CountDownLatch started = new CountDownLatch(1);
    TaskAgentRuntime slow =
        new TaskAgentRuntime(tasks, governance, states, 5000) {
          @Override
          protected Model model(Claim claim) {
            return new Model() {
              public String getModelName() {
                return "slow";
              }

              public Flux<ChatResponse> stream(
                  List<Msg> m, List<ToolSchema> tools, GenerateOptions options) {
                started.countDown();
                return Flux.never();
              }
            };
          }
        };
    try (var w = new TaskWorker(tasks, slow, false, false, 1, 60000, 2000);
        var pool = Executors.newFixedThreadPool(1)) {
      var execution = pool.submit(() -> w.execute(c));
      assertThat(started.await(3, TimeUnit.SECONDS)).isTrue();
      tasks.cancel(owner, t.conversationId(), t.turnId(), "stop");
      assertThat(turn(t).status()).isEqualTo("cancelling");
      assertThat(tasks.heartbeat(c, 60000)).isTrue();
      slow.interrupt(c);
      execution.get(2, TimeUnit.SECONDS);
      assertThat(turn(t).status()).isEqualTo("cancelled");
    }
  }

  @Test
  void T10_T12_terminalFencesOldEventsAndNextTurnStartsCleanButRetainsJournal() {
    var t = submit();
    var c = claim();
    tasks.cancel(owner, t.conversationId(), t.turnId(), "stop");
    tasks.fail(c);
    var next = submit(t.conversationId(), "new");
    var c2 = claim();
    assertThatThrownBy(() -> tasks.finish(c)).isInstanceOf(Fenced.class);
    assertThatThrownBy(() -> tasks.seal(c, new Outcome("completed", "late", 1)))
        .isInstanceOf(Fenced.class);
    assertThat(c.scope().sessionId()).isNotEqualTo(c2.scope().sessionId());
    var store = new MyBatisAgentStateStore(states, c2.scope());
    assertThat(store.exists(c2.scope().userId(), c2.scope().sessionId())).isFalse();
    run(c2);
    assertThat(turn(next).status()).isEqualTo("completed");
    assertThat(tasks.messages(owner, t.conversationId(), 0, 100))
        .extracting(Journal::role)
        .containsExactly("user", "user", "assistant");
  }

  @Test
  void failedExecutionCanStartNewTurnAndCancellationReplayIsStable() {
    var t = submit();
    var c = claim();
    TaskAgentRuntime broken =
        new TaskAgentRuntime(tasks, governance, states, 5000) {
          @Override
          protected Model model(Claim claim) {
            return scripted(
                () -> {
                  throw new IllegalStateException("sensitive provider failure");
                },
                "never");
          }
        };
    try (var w = new TaskWorker(tasks, broken, false, false, 1, 60000, 2000)) {
      w.execute(c);
    } catch (InterruptedException e) {
      throw new RuntimeException(e);
    }
    assertThat(turn(t).status()).isEqualTo("failed");
    assertThat(turn(t).reason()).isEqualTo("execution_failed");
    var next = submit(t.conversationId(), "next");
    var accepted = tasks.cancel(owner, next.conversationId(), next.turnId(), "key");
    var replay = tasks.cancel(owner, next.conversationId(), next.turnId(), "key");
    assertThat(replay.revision()).isEqualTo(accepted.revision());
    assertThat(replay.idempotentReplay()).isTrue();
  }

  @Test
  void T11_twoIndependentServiceInstancesClaimAndProjectExactlyOnce() throws Exception {
    for (int i = 0; i < 6; i++) submit();
    var second = new LifecycleDatabase(source).service;
    try (var pool = Executors.newFixedThreadPool(2)) {
      var a = pool.submit(() -> drain(tasks));
      var b = pool.submit(() -> drain(second));
      a.get();
      b.get();
      var p1 =
          pool.submit(
              () -> {
                while (tasks.projectNext()) {}
              });
      var p2 =
          pool.submit(
              () -> {
                while (second.projectNext()) {}
              });
      p1.get();
      p2.get();
    }
    assertThat(probe.commands()).isEqualTo(6);
    assertThat(probe.attempts()).isEqualTo(6);
    assertThat(probe.answers()).isEqualTo(6);
    assertThat(probe.projections()).isEqualTo(6);
    assertThat(probe.pending()).isZero();
    assertThat(tasks.projectNext()).isFalse();
  }

  void drain(TaskService service) {
    Claim c;
    while ((c = service.claim("replica", 60000)) != null) run(c);
  }

  @Test
  void httpContractsOwnerIsolationAndT06_sseReconnectDoNotExecuteAgain() {
    assertThat(
            http.getForEntity("/v1/conversations/x/messages", String.class).getStatusCode().value())
        .isEqualTo(401);
    String conversation = tasks.createConversation(owner);
    String path = "/v1/conversations/" + conversation;
    assertThat(
            request(HttpMethod.POST, path + "/turns", Map.of("message", "hi"), null, null)
                .getStatusCode()
                .value())
        .isEqualTo(422);
    assertThat(
            request(
                    HttpMethod.POST,
                    path + "/turns",
                    Map.of("message", "hi", "thread_id", "injected"),
                    "x",
                    null)
                .getStatusCode()
                .value())
        .isEqualTo(422);
    var response = request(HttpMethod.POST, path + "/turns", Map.of("message", "hi"), "x", null);
    assertThat(response.getStatusCode().value()).isEqualTo(202);
    assertThat(response.getBody())
        .contains("conversation_id", "turn_id", "idempotent_replay")
        .doesNotContain("command_id", "thread_id", "attempt_id");
    var t = mapper.replay(conversation, "x");
    String turnPath = path + "/turns/" + t.id();
    // A finite observer disconnects while work remains queued, then reconnects after execution.
    var first = request(HttpMethod.GET, turnPath + "/events", null, null, "0");
    assertThat(first.getBody()).contains("id:1", "queued");
    assertThat(probe.attempts()).isZero();
    run(claim());
    var stream = request(HttpMethod.GET, turnPath + "/events", null, null, "1");
    assertThat(stream.getBody()).contains("id:2", "id:3", "completed").doesNotContain("id:1\n");
    assertThat(request(HttpMethod.GET, turnPath + "/events", null, null, "1").getBody())
        .isEqualTo(stream.getBody());
    assertThat(probe.attempts()).isEqualTo(1);
    assertThat(probe.answers()).isEqualTo(1);
    assertThat(
            request(HttpMethod.GET, turnPath + "/events", null, null, "999")
                .getStatusCode()
                .value())
        .isEqualTo(422);
    String foreign = tasks.createConversation(new Owner("another-tenant", "developer"));
    assertThat(
            request(HttpMethod.GET, "/v1/conversations/" + foreign + "/messages", null, null, null)
                .getStatusCode()
                .value())
        .isEqualTo(404);
    assertThatThrownBy(() -> tasks.get(new Owner("local", "other"), conversation, t.id()))
        .isInstanceOf(Problem.class)
        .hasMessage("not_found");
    assertThat(request(HttpMethod.GET, path + "/messages?limit=1", null, null, null).getBody())
        .contains("user")
        .doesNotContain("assistant");
    assertThat(
            http.getForEntity("/actuator/health/readiness", String.class).getStatusCode().value())
        .isEqualTo(200);
  }

  @Test
  void cancelBeforeDispatchAcknowledgesWithoutStartingModel() {
    var t = submit();
    var c = claim();
    tasks.cancel(owner, t.conversationId(), t.turnId(), "cancel");
    run(c);
    assertThat(turn(t).status()).isEqualTo("cancelled");
    assertThat(
            new MyBatisAgentStateStore(states, c.scope())
                .exists(c.scope().userId(), c.scope().sessionId()))
        .isFalse();
  }

  @Test
  void completionTransactionFailurePreservesReceiptForRecovery() throws Exception {
    var t = submit();
    var c = claim();
    var failing =
        new TaskService(
            mapper,
            states,
            new org.springframework.transaction.support.TransactionTemplate(
                new org.springframework.jdbc.datasource.DataSourceTransactionManager(source)),
            tasks.governance(),
            tasks.releases()) {
          @Override
          public void finish(Claim claim) {
            throw new IllegalStateException("injected commit failure");
          }
        };
    try (var w = new TaskWorker(failing, runtime, false, false, 1, 60000, 2000)) {
      w.execute(c);
    }
    assertThat(turn(t).status()).isEqualTo("running");
    assertThat(mapper.receipt(c.command().attemptId()).outcome()).isEqualTo("completed");
    probe.expire();
    tasks.recoverExpired();
    assertThat(turn(t).status()).isEqualTo("completed");
    assertThat(probe.answers()).isEqualTo(1);
  }

  @Test
  void workerPollIsAsynchronousAndBoundedAndHeartbeatsCancellation() throws Exception {
    var first = submit();
    var second = submit();
    CountDownLatch started = new CountDownLatch(1);
    TaskAgentRuntime slow =
        new TaskAgentRuntime(tasks, governance, states, 30000) {
          @Override
          protected Model model(Claim claim) {
            return new Model() {
              public String getModelName() {
                return "slow-poll";
              }

              public Flux<ChatResponse> stream(
                  List<Msg> messages, List<ToolSchema> tools, GenerateOptions options) {
                started.countDown();
                return Flux.never();
              }
            };
          }
        };
    try (var w = new TaskWorker(tasks, slow, true, true, 1, 60000, 2000)) {
      w.poll();
      assertThat(started.await(3, TimeUnit.SECONDS)).isTrue();
      assertThat(turn(first).status()).isEqualTo("running");
      assertThat(turn(second).status()).isEqualTo("queued");
      tasks.cancel(owner, first.conversationId(), first.turnId(), "stop");
      w.heartbeat();
      long deadline = System.nanoTime() + TimeUnit.SECONDS.toNanos(4);
      while (!turn(first).terminal() && System.nanoTime() < deadline) Thread.sleep(20);
      assertThat(turn(first).status()).isEqualTo("cancelled");
      tasks.cancel(owner, second.conversationId(), second.turnId(), "stop");
      w.poll();
      assertThat(probe.projections()).isEqualTo(2);
    }
  }

  @Test
  void modelTimeoutFailsAndReleasesTheConversation() throws Exception {
    var t = submit();
    var c = claim();
    TaskAgentRuntime timeout =
        new TaskAgentRuntime(tasks, governance, states, 150) {
          @Override
          protected Model model(Claim claim) {
            return new Model() {
              public String getModelName() {
                return "timeout";
              }

              public Flux<ChatResponse> stream(
                  List<Msg> messages, List<ToolSchema> tools, GenerateOptions options) {
                return Flux.never();
              }
            };
          }
        };
    try (var w = new TaskWorker(tasks, timeout, false, false, 1, 60000, 2000)) {
      w.execute(c);
    }
    assertThat(turn(t).status()).isEqualTo("failed");
    assertThat(submit(t.conversationId(), "next").status()).isEqualTo("queued");
  }

  ResponseEntity<String> request(
      HttpMethod method, String path, Object body, String key, String cursor) {
    HttpHeaders h = new HttpHeaders();
    h.setBearerAuth("m1-test-token");
    h.setContentType(MediaType.APPLICATION_JSON);
    if (key != null) h.set("Idempotency-Key", key);
    if (cursor != null) h.set("Last-Event-ID", cursor);
    return http.exchange(path, method, new HttpEntity<>(body, h), String.class);
  }

  void execute(TaskAgentRuntime agent, Claim c) {
    var result = agent.execute(c);
    tasks.seal(c, result);
    tasks.finish(c);
  }

  static Model scripted(Runnable before, String text) {
    return new Model() {
      public String getModelName() {
        return "scripted-m1";
      }

      public Flux<ChatResponse> stream(
          List<Msg> m, List<ToolSchema> tools, GenerateOptions options) {
        return Flux.defer(
            () -> {
              before.run();
              return Flux.just(
                  ChatResponse.builder()
                      .id("test")
                      .content(List.of(TextBlock.builder().text(text).build()))
                      .build());
            });
      }
    };
  }

  void fork(String mode, int expected) throws Exception {
    Path directory = Path.of("target/m1");
    Files.createDirectories(directory);
    Path config = Files.createTempFile(directory, "connection-", ".properties");
    Properties p = new Properties();
    p.setProperty("url", PG.getJdbcUrl());
    p.setProperty("user", PG.getUsername());
    p.setProperty("password", PG.getPassword());
    try (var out = Files.newOutputStream(config)) {
      p.store(out, "ephemeral test connection");
    }
    Path log = directory.resolve(mode + "-" + UUID.randomUUID() + ".log");
    try {
      Process process =
          new ProcessBuilder(
                  Path.of(System.getProperty("java.home"), "bin/java").toString(),
                  "-cp",
                  System.getProperty(
                      "surefire.test.class.path", System.getProperty("java.class.path")),
                  ForkedTask.class.getName(),
                  config.toString(),
                  mode)
              .redirectErrorStream(true)
              .redirectOutput(log.toFile())
              .start();
      if (!process.waitFor(30, TimeUnit.SECONDS)) {
        process.destroyForcibly();
        fail("Child timed out: " + mode);
      }
      assertThat(process.exitValue())
          .withFailMessage(() -> "Child " + mode + " failed; see " + log)
          .isEqualTo(expected);
      Files.writeString(
          log.resolveSibling(log.getFileName() + ".exit.json"),
          "{\"mode\":\"" + mode + "\",\"exit\":" + process.exitValue() + "}");
    } finally {
      Files.deleteIfExists(config);
    }
  }
}
