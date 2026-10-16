package com.financeclaw.m4;

import static org.assertj.core.api.Assertions.*;

import com.financeclaw.application.*;
import com.financeclaw.bootstrap.FinanceClawApplication;
import com.financeclaw.domain.Memory.*;
import com.financeclaw.domain.Task.*;
import com.financeclaw.infrastructure.agentscope.execution.TaskAgentRuntime;
import com.financeclaw.infrastructure.memory.MemoryMapper;
import com.financeclaw.memory.*;
import com.financeclaw.support.PayloadJson;
import io.agentscope.core.embedding.EmbeddingModel;
import java.util.*;
import java.util.concurrent.*;
import java.util.concurrent.atomic.*;
import javax.sql.DataSource;
import org.junit.jupiter.api.*;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.boot.test.context.SpringBootTest;
import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.jdbc.datasource.DriverManagerDataSource;
import org.springframework.test.context.*;
import org.springframework.transaction.support.TransactionTemplate;
import reactor.core.publisher.Mono;

@org.springframework.boot.test.autoconfigure.web.servlet.AutoConfigureMockMvc
@SpringBootTest(
    classes = FinanceClawApplication.class,
    properties = {
      "financeclaw.worker.enabled=false",
      "financeclaw.integrations.enabled=false",
      "financeclaw.memory.enabled=true",
      "financeclaw.auth.token=m4-memory-token",
      "financeclaw.memory.embedding.revision=test-v1"
    })
class MemoryIT {
  @DynamicPropertySource
  static void database(DynamicPropertyRegistry r) {
    r.add("spring.datasource.url", M4Database.PG::getJdbcUrl);
    r.add("spring.datasource.username", M4Database.PG::getUsername);
    r.add("spring.datasource.password", M4Database.PG::getPassword);
  }

  @Autowired org.springframework.test.web.servlet.MockMvc http;
  @Autowired TaskService tasks;
  @Autowired org.springframework.context.ApplicationContext taskContext;
  @Autowired MemoryService memory;
  @Autowired MemoryMapper db;
  @Autowired TransactionTemplate tx;
  @Autowired TaskAgentRuntime runtime;
  @Autowired DataSource source;
  JdbcTemplate sql;
  final Owner alice = new Owner("local", "alice"), bob = new Owner("local", "bob");

  @BeforeEach
  void reset() throws Exception {
    sql = new JdbcTemplate(source);
    sql.execute("TRUNCATE business.conversations CASCADE");
    sql.execute(
        "TRUNCATE memory.owners,memory.jobs,memory.operations,memory.audit,memory.embedding_profiles CASCADE");
    sql.execute(
        java.nio.file.Files.readString(java.nio.file.Path.of("ops/memory-vector-index.sql")));
    java.nio.file.Files.createDirectories(java.nio.file.Path.of("target/m4"));
    java.nio.file.Files.writeString(
        java.nio.file.Path.of("target/m4/environment.json"),
        PayloadJson.canonical(
            Map.of(
                "postgres_image",
                M4Database.PG.getDockerImageName(),
                "postgres_version",
                sql.queryForObject("SHOW server_version", String.class),
                "pgvector",
                sql.queryForObject(
                    "SELECT extversion FROM pg_extension WHERE extname='vector'", String.class),
                "dimensions",
                1024)));
  }

  Accepted submit(Owner owner, String message) {
    return tasks.submit(
        owner, tasks.createConversation(owner), UUID.randomUUID().toString(), message);
  }

  void finish(Accepted t) throws Exception {
    try (var worker =
        com.financeclaw.m1.TaskWorkerFixture.create(
            taskContext, runtime, false, false, 1, 60000, 2000)) {
      worker.execute(tasks.claim("memory-test", 60000));
    }
    assertThat(tasks.get(alice, t.conversationId(), t.turnId()).status()).isEqualTo("completed");
  }

  static EmbeddingModel embedding() {
    return new EmbeddingModel() {
      public String getModelName() {
        return "test";
      }

      public int getDimensions() {
        return 1024;
      }

      public Mono<double[]> embed(io.agentscope.core.message.ContentBlock block) {
        double[] v = new double[1024];
        v[0] = 1;
        return Mono.just(v);
      }
    };
  }

  MemoryProcessor processor(java.util.function.Function<String, Extraction> extractor) {
    return new MemoryProcessor(db, tx, extractor, MemoryIT::embedding, "test-v1");
  }

  MemoryProcessor explicit() {
    return processor(
        text -> new Extraction(List.of(Objects.requireNonNull(MemoryProcessor.explicit(text)))));
  }

  @Test
  void completionRollbackCannotCreateMemoryIntent() throws Exception {
    var t = submit(alice, "/remember response_style=简洁");
    var claim = tasks.claim("atomic", 60000);
    var result = runtime.execute(claim);
    tasks.seal(claim, result);
    assertThatThrownBy(
            () ->
                tx.executeWithoutResult(
                    s -> {
                      tasks.finish(claim);
                      throw new IllegalStateException("rollback");
                    }))
        .hasMessage("rollback");
    assertThat(sql.queryForObject("SELECT count(*) FROM memory.jobs", Integer.class)).isZero();
    assertThat(db.sourceById(t.turnId()).status()).isEqualTo("admitted");
    tasks.finish(claim);
    assertThat(sql.queryForObject("SELECT count(*) FROM memory.jobs", Integer.class)).isEqualTo(1);
  }

  @Test
  void independentRestrictedWorkerProcessesDurableJobsAfterApiCompletion() throws Exception {
    memoryDatabaseRoleCannotReadBusinessStateOrChannel();
    var t = submit(alice, "/remember response_style=简洁");
    finish(t);
    try (var remote = new MockMemoryModels()) {
      var log = java.nio.file.Path.of("target/m4/memory-worker-process.log");
      java.nio.file.Files.createDirectories(log.getParent());
      var builder =
          new ProcessBuilder(
              java.nio.file.Path.of(System.getProperty("java.home"), "bin/java").toString(),
              "-Dloader.main=com.financeclawmemory.MemoryWorkerApplication",
              "-cp",
              "target/financeclaw-java-0.1.0-SNAPSHOT.jar",
              "org.springframework.boot.loader.launch.PropertiesLauncher");
      var env = builder.environment();
      env.put("FINANCECLAW_MEMORY_DATABASE_URL", M4Database.PG.getJdbcUrl());
      env.put("FINANCECLAW_MEMORY_DATABASE_USER", "financeclaw_memory");
      env.put("FINANCECLAW_MEMORY_DATABASE_PASSWORD", "test-memory-only");
      env.put("FINANCECLAW_EMBEDDING_MODEL", "embedding-test");
      env.put("FINANCECLAW_EMBEDDING_REVISION", "test-v1");
      env.put("FINANCECLAW_EMBEDDING_BASE_URL", remote.url());
      env.put("FINANCECLAW_EMBEDDING_API_KEY", "mock-key");
      builder.redirectErrorStream(true).redirectOutput(log.toFile());
      var process = builder.start();
      try {
        long deadline = System.nanoTime() + TimeUnit.SECONDS.toNanos(30);
        while (process.isAlive()
            && System.nanoTime() < deadline
            && sql.queryForObject("SELECT count(*) FROM memory.projections", Integer.class) == 0)
          Thread.sleep(100);
        assertThat(sql.queryForObject("SELECT count(*) FROM memory.projections", Integer.class))
            .withFailMessage(java.nio.file.Files.readString(log))
            .isEqualTo(1);
        assertThat(memory.list(alice)).hasSize(1);
        java.nio.file.Files.writeString(
            java.nio.file.Path.of("target/m4/process.json"),
            "{\"worker_pid\":"
                + process.pid()
                + ",\"role\":\"financeclaw_memory\",\"projection_count\":1}\n");
      } finally {
        process.destroy();
        if (!process.waitFor(5, TimeUnit.SECONDS)) {
          process.destroyForcibly();
          process.waitFor(5, TimeUnit.SECONDS);
        }
      }
    }
  }

  Fact preference() throws Exception {
    var t = submit(alice, "/remember response_language=中文");
    finish(t);
    assertThat(explicit().poll()).isTrue();
    return memory.list(alice).getFirst();
  }

  @Test
  void onlyCompletedTrustedUserInputEnqueuesExtraction() throws Exception {
    var t = submit(alice, "/remember response_language=中文");
    assertThat(sql.queryForObject("SELECT count(*) FROM memory.jobs", Integer.class)).isZero();
    finish(t);
    assertThat(db.sourceById(t.turnId()).content()).isEqualTo("/remember response_language=中文");
    var captured = new AtomicReference<String>();
    var worker =
        processor(
            text -> {
              captured.set(text);
              return new Extraction(List.of());
            });
    assertThat(worker.poll()).isTrue();
    assertThat(worker.poll()).isFalse();
    assertThat(captured.get()).isEqualTo("/remember response_language=中文");
    assertThat(sql.queryForObject("SELECT count(*) FROM memory.sources", Integer.class))
        .isEqualTo(1);
  }

  @Test
  void olderExtractionCompletingLastCannotReplaceNewerExplicitPreference() throws Exception {
    var old = submit(alice, "/remember response_language=中文");
    finish(old);
    var newer = submit(alice, "/remember response_language=English");
    finish(newer);
    var worker = explicit();
    var first = worker.claim();
    var second = worker.claim();
    worker.process(second);
    worker.process(first);
    assertThat(db.active(alice)).hasSize(1);
    assertThat(db.active(alice).getFirst().content()).isEqualTo("English");
    assertThat(
            sql.queryForObject(
                "SELECT count(*) FROM memory.facts WHERE status='superseded'", Integer.class))
        .isEqualTo(1);
  }

  @Test
  void preferenceAutoConfirmationFreezesRecallAndIsolatesOwner() throws Exception {
    var f = preference();
    assertThat(f.status()).isEqualTo("active");
    assertThat(memory.list(bob)).isEmpty();
    var t = submit(alice, "hello");
    var claim = tasks.claim("recall", 60000);
    assertThat(tasks.memoryContext(claim)).contains("中文", f.id());
    var another = submit(bob, "hello");
    assertThat(db.recall(another.turnId(), bob)).isEmpty();
    assertThat(db.recall(t.turnId(), bob)).isEmpty();
  }

  @Test
  void candidateConfirmationIsIdempotentAndDoesNotResumeBusiness() throws Exception {
    var t = submit(alice, "我长期居住在上海");
    finish(t);
    processor(text -> new Extraction(List.of(new Proposal("home_city", "上海", text)))).poll();
    var f = memory.list(alice).getFirst();
    assertThat(f.status()).isEqualTo("candidate");
    assertThatThrownBy(() -> memory.decide(bob, f.id(), f.revision(), true, "x"))
        .isInstanceOf(Problem.class);
    assertThat(memory.decide(alice, f.id(), f.revision(), true, "confirm")).isEqualTo("active");
    assertThat(memory.decide(alice, f.id(), f.revision(), true, "confirm")).isEqualTo("active");
    assertThat(tasks.get(alice, t.conversationId(), t.turnId()).status()).isEqualTo("completed");
    assertThat(
            sql.queryForObject(
                "SELECT count(*) FROM business.commands WHERE status='queued'", Integer.class))
        .isZero();
    assertThatThrownBy(() -> memory.decide(alice, f.id(), f.revision(), false, "confirm"))
        .hasMessage("idempotency_conflict");
  }

  @Test
  void memoryHttpRequiresAuthenticationOwnerAndExplicitDecision() throws Exception {
    var t = submit(alice, "我住在上海");
    finish(t);
    processor(text -> new Extraction(List.of(new Proposal("home_city", "上海", text)))).poll();
    var fact = memory.list(alice).getFirst();
    http.perform(
            org.springframework.test.web.servlet.request.MockMvcRequestBuilders.get("/v1/memory"))
        .andExpect(
            org.springframework.test.web.servlet.result.MockMvcResultMatchers.status()
                .isUnauthorized());
    http.perform(
            org.springframework.test.web.servlet.request.MockMvcRequestBuilders.get("/v1/memory")
                .header("Authorization", "Bearer m4-memory-token"))
        .andExpect(
            org.springframework.test.web.servlet.result.MockMvcResultMatchers.content().json("[]"));
    http.perform(
            org.springframework.test.web.servlet.request.MockMvcRequestBuilders.post(
                    "/v1/memory/" + fact.id() + "/decision")
                .header("Authorization", "Bearer m4-memory-token")
                .header("Idempotency-Key", "api-missing-decision")
                .contentType("application/json")
                .content("{\"revision\":1}"))
        .andExpect(
            org.springframework.test.web.servlet.result.MockMvcResultMatchers.status()
                .isUnprocessableEntity());
    http.perform(
            org.springframework.test.web.servlet.request.MockMvcRequestBuilders.post(
                    "/v1/memory/" + fact.id() + "/decision")
                .header("Authorization", "Bearer m4-memory-token")
                .header("Idempotency-Key", "api-wrong-owner")
                .contentType("application/json")
                .content("{\"revision\":1,\"accept\":true}"))
        .andExpect(
            org.springframework.test.web.servlet.result.MockMvcResultMatchers.status()
                .isNotFound());
    assertThat(db.fact(fact.id()).status()).isEqualTo("candidate");
  }

  @Test
  void inventedFactsAndOversizedExtractionKeepRetryIntent() throws Exception {
    var t = submit(alice, "你好");
    finish(t);
    processor(text -> new Extraction(List.of(new Proposal("home_city", "北京", "北京")))).poll();
    assertThat(memory.list(alice)).isEmpty();
    assertThat(sql.queryForObject("SELECT error_code FROM memory.jobs", String.class))
        .isEqualTo("extraction_failed");
    assertThat(sql.queryForObject("SELECT status FROM memory.jobs", String.class))
        .isEqualTo("pending");
  }

  @Test
  void failedIndexCannotRollBackFactsAndCanRetryIndependently() throws Exception {
    var f = preference();
    var broken =
        new MemoryProcessor(
            db,
            tx,
            text -> {
              throw new AssertionError();
            },
            () ->
                new EmbeddingModel() {
                  public String getModelName() {
                    return "bad";
                  }

                  public int getDimensions() {
                    return 1024;
                  }

                  public Mono<double[]> embed(io.agentscope.core.message.ContentBlock b) {
                    return Mono.just(new double[] {1});
                  }
                },
            "test-v1");
    broken.poll();
    assertThat(db.fact(f.id()).status()).isEqualTo("active");
    assertThat(sql.queryForObject("SELECT count(*) FROM memory.projections", Integer.class))
        .isZero();
    sql.execute("UPDATE memory.jobs SET next_attempt_at=clock_timestamp() WHERE kind='index'");
    explicit().poll();
    assertThat(sql.queryForObject("SELECT dimensions FROM memory.projections", Integer.class))
        .isEqualTo(1024);
  }

  @Test
  void forgettingFencesLateExtractionAndSuspendedRecall() throws Exception {
    var f = preference();
    var t = submit(alice, "我喜欢简洁回答");
    finish(t); // Drain prior index before extraction claim.
    explicit().poll();
    var job = explicit().claim();
    var original = db.sourceById(job.keyId());
    assertThat(memory.forget(alice, 1, "forget")).isEqualTo(2);
    assertThat(memory.forget(alice, 1, "forget")).isEqualTo(2);
    explicit()
        .commitExtraction(
            job,
            original,
            new Extraction(List.of(new Proposal("response_style", "简洁", original.content()))));
    assertThat(memory.list(alice)).isEmpty();
    assertThat(db.fact(f.id()).content()).isNull();
    assertThat(db.sourceById(job.keyId()).content()).isNull();
    assertThat(sql.queryForObject("SELECT count(*) FROM memory.projections", Integer.class))
        .isZero();
    assertThat(memory.privacy(alice).purgePending()).isTrue();
    assertThatThrownBy(() -> submit(alice, "new during purge")).hasMessage("purge_pending");
    explicit().poll();
    assertThat(memory.privacy(alice).purgePending()).isFalse();
    assertThatThrownBy(() -> memory.check(tasks.get(alice, t.conversationId(), t.turnId())))
        .hasMessage("memory_privacy_changed");
    assertThat(memory.rebuild(alice, "rebuild")).isZero();
  }

  @Test
  void forgettingDuringEmbeddingCannotResurrectProjection() throws Exception {
    preference();
    var entered = new CountDownLatch(1);
    var release = new CountDownLatch(1);
    var worker =
        new MemoryProcessor(
            db,
            tx,
            text -> null,
            () ->
                new EmbeddingModel() {
                  public String getModelName() {
                    return "race";
                  }

                  public int getDimensions() {
                    return 1024;
                  }

                  public Mono<double[]> embed(io.agentscope.core.message.ContentBlock b) {
                    return Mono.fromCallable(
                        () -> {
                          entered.countDown();
                          release.await(10, TimeUnit.SECONDS);
                          double[] v = new double[1024];
                          v[0] = 1;
                          return v;
                        });
                  }
                },
            "test-v1");
    try (var pool = Executors.newVirtualThreadPerTaskExecutor()) {
      var future = pool.submit(worker::poll);
      assertThat(entered.await(10, TimeUnit.SECONDS)).isTrue();
      memory.forget(alice, 1, "forget-race");
      release.countDown();
      future.get(10, TimeUnit.SECONDS);
    }
    assertThat(sql.queryForObject("SELECT count(*) FROM memory.projections", Integer.class))
        .isZero();
  }

  @Test
  void claimedLeaseExpiresAndOnlyNewWorkerCanCommit() throws Exception {
    var t = submit(alice, "/remember response_style=简洁");
    finish(t);
    var worker = explicit();
    var first = worker.claim();
    sql.execute("UPDATE memory.jobs SET lease_until=clock_timestamp()-interval '1 second'");
    var second = worker.claim();
    worker.process(first);
    assertThat(memory.list(alice)).isEmpty();
    worker.process(second);
    assertThat(memory.list(alice)).hasSize(1);
    worker.process(first);
    assertThat(memory.list(alice)).hasSize(1);
  }

  @Test
  void vectorSearchChecksOwnerEpochRevisionAndPinnedModel() throws Exception {
    var f = preference();
    explicit().poll();
    String vector =
        MemoryProcessor.vector(embedding().embed(null).block()).replace('{', '[').replace('}', ']');
    assertThat(db.search(alice, "test-v1", vector)).extracting(Fact::id).containsExactly(f.id());
    assertThat(db.search(bob, "test-v1", vector)).isEmpty();
    assertThat(db.search(alice, "other-model", vector)).isEmpty();
    sql.update("UPDATE memory.projections SET revision=revision+1");
    assertThat(db.search(alice, "test-v1", vector)).isEmpty();
    memory.rebuild(alice, "rebuild-1");
    explicit().poll();
    assertThat(db.search(alice, "test-v1", vector)).hasSize(1);
    memory.forget(alice, 1, "erase");
    assertThat(db.search(alice, "test-v1", vector)).isEmpty();
  }

  @Test
  void memoryDatabaseRoleCannotReadBusinessStateOrChannel() throws Exception {
    sql.execute(
        "DO $$ BEGIN IF NOT EXISTS(SELECT FROM pg_roles WHERE rolname='financeclaw_memory') THEN CREATE ROLE financeclaw_memory LOGIN PASSWORD 'test-memory-only'; END IF; END $$");
    sql.execute(
        java.nio.file.Files.readString(java.nio.file.Path.of("ops/memory-worker-grants.sql"))
            .replace("ON DATABASE financeclaw_java", "ON DATABASE m4"));
    var restricted =
        new JdbcTemplate(
            new DriverManagerDataSource(
                M4Database.PG.getJdbcUrl(), "financeclaw_memory", "test-memory-only"));
    assertThat(restricted.queryForObject("SELECT count(*) FROM memory.jobs", Integer.class))
        .isZero();
    for (var table : List.of("business.turns", "agent_runtime.states", "channel.bindings"))
      assertThatThrownBy(() -> restricted.queryForList("SELECT * FROM " + table))
          .isInstanceOf(org.springframework.dao.DataAccessException.class);
    assertThatThrownBy(() -> restricted.execute("CREATE TABLE memory.forbidden(id int)"))
        .isInstanceOf(org.springframework.dao.DataAccessException.class);
  }
}
