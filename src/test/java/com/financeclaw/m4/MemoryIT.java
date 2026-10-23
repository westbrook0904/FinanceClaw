package com.financeclaw.m4;

import static org.assertj.core.api.Assertions.*;
import static org.mockito.ArgumentMatchers.*;
import static org.mockito.Mockito.*;

import com.financeclaw.app.memory.*;
import com.financeclaw.app.memory.MemoryService;
import com.financeclaw.app.memory.port.MemoryRepository;
import com.financeclaw.bootstrap.FinanceClawApplication;
import com.financeclaw.domain.Memory.*;
import com.financeclaw.domain.Task.*;
import com.financeclaw.domain.memory.EmbeddingVector;
import com.financeclaw.domain.memory.MemoryPolicy;
import com.financeclaw.fixture.TaskFixture;
import com.financeclaw.infrastructure.agentscope.execution.TaskAgentRuntime;
import com.financeclaw.infrastructure.memory.*;
import com.financeclaw.infrastructure.memory.MemoryMapper;
import com.financeclaw.support.PayloadJson;
import java.util.*;
import java.util.concurrent.*;
import java.util.concurrent.atomic.*;
import javax.sql.DataSource;
import org.junit.jupiter.api.*;
import org.junit.jupiter.params.ParameterizedTest;
import org.junit.jupiter.params.provider.ValueSource;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.boot.test.context.SpringBootTest;
import org.springframework.context.annotation.Import;
import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.jdbc.datasource.DriverManagerDataSource;
import org.springframework.test.context.*;
import org.springframework.transaction.support.TransactionSynchronizationManager;
import org.springframework.transaction.support.TransactionTemplate;

@org.springframework.boot.test.autoconfigure.web.servlet.AutoConfigureMockMvc
@Import(TaskFixture.class)
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
    @Autowired TaskFixture tasks;
    @Autowired org.springframework.context.ApplicationContext taskContext;
    @Autowired MemoryService memory;
    @Autowired MemoryMapper db;
    @Autowired MemoryRepository repository;
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
                java.nio.file.Files.readString(
                        java.nio.file.Path.of("ops/memory-vector-index.sql")));
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
                                        "SELECT extversion FROM pg_extension WHERE extname='vector'",
                                        String.class),
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
                        taskContext, runtime, false, 1, 60000, 2000)) {
            worker.execute(tasks.claim("memory-test", 60000));
        }
        assertThat(tasks.get(alice, t.conversationId(), t.turnId()).status())
                .isEqualTo("completed");
    }

    static EmbeddingVector embedding() {
        double[] v = new double[1024];
        v[0] = 1;
        return EmbeddingVector.of(v);
    }

    MemoryProcessingService processor(java.util.function.Function<String, Extraction> extractor) {
        return new MemoryProcessingService(
                repository, tx, extractor::apply, text -> embedding(), "test-v1");
    }

    MemoryProcessingService explicit() {
        return processor(
                text ->
                        new Extraction(
                                List.of(
                                        Objects.requireNonNull(
                                                com.financeclaw.domain.Memory.Proposal.explicit(
                                                        text)))));
    }

    @Test
    void historyBatchChecksOwnerEpochAndMissingSourcesUsingOnlyIds() {
        var first = submit(alice, "first private message");
        var second = submit(alice, "second private message");
        var other = submit(bob, "other private message");
        var firstTurn = tasks.get(alice, first.conversationId(), first.turnId());
        var secondTurn = tasks.get(alice, second.conversationId(), second.turnId());
        var otherTurn = tasks.get(bob, other.conversationId(), other.turnId());
        var candidates = List.of(secondTurn, otherTurn, firstTurn);

        assertThat(memory.filterHistory(secondTurn, candidates))
                .containsExactly(secondTurn, firstTurn);
        assertThat(repository.currentSources(alice, 1, 0, List.of(other.turnId(), "missing")))
                .isEmpty();
        assertThat(repository.currentSources(alice, 2, 0, List.of(first.turnId(), second.turnId())))
                .isEmpty();
        assertThat(repository.currentSources(alice, 1, 0, List.of())).isEmpty();

        memory.forget(alice, 1, "history-forget");

        assertThat(memory.filterHistory(secondTurn, candidates)).isEmpty();
        assertThat(memory.filterHistory(otherTurn, List.of(otherTurn))).containsExactly(otherTurn);
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
        assertThat(db.getSourceById(t.turnId()).status()).isEqualTo("admitted");
        tasks.finish(claim);
        assertThat(sql.queryForObject("SELECT count(*) FROM memory.jobs", Integer.class))
                .isEqualTo(1);
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
                            java.nio.file.Path.of(System.getProperty("java.home"), "bin/java")
                                    .toString(),
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
                        && sql.queryForObject(
                                        "SELECT count(*) FROM memory.projections", Integer.class)
                                == 0) Thread.sleep(100);
                assertThat(
                                sql.queryForObject(
                                        "SELECT count(*) FROM memory.projections", Integer.class))
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

    private Map<String, List<String>> memoryRows() {
        var rows = new TreeMap<String, List<String>>();
        for (String table :
                List.of(
                        "owners",
                        "sources",
                        "facts",
                        "recalls",
                        "jobs",
                        "projections",
                        "audit",
                        "operations"))
            rows.put(
                    table,
                    sql.queryForList(
                            "SELECT row_to_json(t)::text FROM memory."
                                    + table
                                    + " t ORDER BY row_to_json(t)::text",
                            String.class));
        return rows;
    }

    @ParameterizedTest
    @ValueSource(strings = {"confirm", "reject", "forget", "rebuild"})
    void operationReceiptFailureRollsBackEveryMemoryMutation(String operation) throws Exception {
        preference();
        explicit().poll();
        var turn = submit(alice, "English");
        finish(turn);
        processor(
                        text ->
                                new Extraction(
                                        List.of(
                                                new Proposal(
                                                        "response_language",
                                                        "English",
                                                        "English"))))
                .poll();
        var candidate =
                memory.list(alice).stream()
                        .filter(f -> f.status().equals("candidate"))
                        .findFirst()
                        .orElseThrow();
        var failing = spy(new MyBatisMemoryRepository(db));
        doThrow(new IllegalStateException("receipt-insert-failed"))
                .when(failing)
                .recordOperation(any(), anyString(), anyString(), anyString());
        var service =
                new MemoryService(
                        failing,
                        tx,
                        new MemoryPolicy(true, false, "test-v1", false),
                        new com.financeclaw.app.memory.MemoryForgetService(
                                failing,
                                tx,
                                new com.financeclaw.infrastructure.memory
                                        .JdbcForgetRecoveryRepository(sql.getDataSource()),
                                org.mockito.Mockito.mock(
                                        com.financeclaw.app.memory.port.ForgetLedger.class),
                                taskContext.getBean(com.financeclaw.app.task.TaskEventHub.class)));
        var before = memoryRows();
        assertThatThrownBy(
                        () -> {
                            switch (operation) {
                                case "confirm" ->
                                        service.decide(
                                                alice,
                                                candidate.id(),
                                                candidate.revision(),
                                                true,
                                                "rollback-confirm");
                                case "reject" ->
                                        service.decide(
                                                alice,
                                                candidate.id(),
                                                candidate.revision(),
                                                false,
                                                "rollback-reject");
                                case "forget" -> service.forget(alice, 1, "rollback-forget");
                                case "rebuild" -> service.rebuild(alice, "rollback-rebuild");
                                default -> throw new AssertionError(operation);
                            }
                        })
                .hasMessage("receipt-insert-failed");
        assertThat(memoryRows()).isEqualTo(before);
    }

    @Test
    void persistedOperationReceiptsReplayBeforeCurrentStateChecks() throws Exception {
        com.fasterxml.jackson.databind.JsonNode hashes;
        try (var in = getClass().getResourceAsStream("/architecture/memory-contract.json")) {
            hashes =
                    new com.fasterxml.jackson.databind.ObjectMapper()
                            .readTree(in)
                            .path("operation_hashes");
        }
        memory.privacy(alice);
        var results =
                Map.of("confirm", "active", "reject", "rejected", "forget", "4", "rebuild", "7");
        results.forEach(
                (key, result) ->
                        db.insertOperationReceipt(alice, key, hashes.path(key).asText(), result));
        var service =
                new MemoryService(
                        repository,
                        tx,
                        new MemoryPolicy(true, false, "embedding-v1", false),
                        new com.financeclaw.app.memory.MemoryForgetService(
                                repository,
                                tx,
                                new com.financeclaw.infrastructure.memory
                                        .JdbcForgetRecoveryRepository(sql.getDataSource()),
                                org.mockito.Mockito.mock(
                                        com.financeclaw.app.memory.port.ForgetLedger.class),
                                taskContext.getBean(com.financeclaw.app.task.TaskEventHub.class)));
        var before = memoryRows();
        assertThat(service.decide(alice, "fact", 2, true, "confirm")).isEqualTo("active");
        assertThat(service.decide(alice, "fact", 2, false, "reject")).isEqualTo("rejected");
        assertThat(service.forget(alice, 3, "forget")).isEqualTo(4);
        assertThat(service.rebuild(alice, "rebuild")).isEqualTo(7);
        assertThatThrownBy(() -> service.decide(alice, "fact", 2, false, "confirm"))
                .hasMessage("idempotency_conflict");
        assertThat(memoryRows()).isEqualTo(before);
    }

    @Test
    void searchUsesModelOutsideTransactionAndDiscardsResultsAfterForget() throws Exception {
        var fact = preference();
        explicit().poll();
        var forgetDuringEmbedding = new AtomicBoolean(false);
        var search =
                new MemorySearchService(
                        repository,
                        text -> {
                            assertThat(
                                            TransactionSynchronizationManager
                                                    .isActualTransactionActive())
                                    .isFalse();
                            if (forgetDuringEmbedding.get())
                                memory.forget(alice, 1, "search-forget");
                            return embedding();
                        },
                        tx,
                        new MemoryPolicy(true, true, "test-v1", false));
        assertThat(search.search(alice, "language"))
                .extracting(Fact::id)
                .containsExactly(fact.id());
        assertThat(search.search(bob, "language")).isEmpty();
        forgetDuringEmbedding.set(true);
        assertThat(search.search(alice, "language")).isEmpty();
        assertThat(memory.privacy(alice).epoch()).isEqualTo(2);
    }

    @Test
    void ambientTransactionsCannotEncloseMemoryModelWork() throws Exception {
        var turn = submit(alice, "我住在上海");
        finish(turn);
        var calls = new AtomicInteger();
        var processing =
                processor(
                        text -> {
                            calls.incrementAndGet();
                            return new Extraction(List.of());
                        });
        var job = processing.claim();
        var search =
                new MemorySearchService(
                        repository,
                        text -> {
                            calls.incrementAndGet();
                            return embedding();
                        },
                        tx,
                        new MemoryPolicy(true, true, "test-v1", false));
        var before = memoryRows();
        assertThatThrownBy(() -> tx.executeWithoutResult(s -> processing.process(job)))
                .hasMessage("Memory model work requires no ambient transaction");
        assertThatThrownBy(() -> tx.executeWithoutResult(s -> search.search(alice, "city")))
                .hasMessage("Memory model work requires no ambient transaction");
        assertThat(calls).hasValue(0);
        assertThat(memoryRows()).isEqualTo(before);
        processing.process(job);
        assertThat(calls).hasValue(1);
        assertThat(db.getJobForUpdate(job.id()).status()).isEqualTo("done");
    }

    @Test
    void onlyCompletedTrustedUserInputEnqueuesExtraction() throws Exception {
        var t = submit(alice, "/remember response_language=中文");
        assertThat(sql.queryForObject("SELECT count(*) FROM memory.jobs", Integer.class)).isZero();
        finish(t);
        assertThat(db.getSourceById(t.turnId()).content())
                .isEqualTo("/remember response_language=中文");
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
        assertThat(db.listActiveFacts(alice)).hasSize(1);
        assertThat(db.listActiveFacts(alice).getFirst().content()).isEqualTo("English");
        assertThat(
                        sql.queryForObject(
                                "SELECT count(*) FROM memory.facts WHERE status='superseded'",
                                Integer.class))
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
        assertThat(db.listFrozenRecallFacts(another.turnId(), bob)).isEmpty();
        assertThat(db.listFrozenRecallFacts(t.turnId(), bob)).isEmpty();
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
        assertThat(tasks.get(alice, t.conversationId(), t.turnId()).status())
                .isEqualTo("completed");
        assertThat(
                        sql.queryForObject(
                                "SELECT count(*) FROM business.commands WHERE status='queued'",
                                Integer.class))
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
                        org.springframework.test.web.servlet.request.MockMvcRequestBuilders.get(
                                "/v1/memory"))
                .andExpect(
                        org.springframework.test.web.servlet.result.MockMvcResultMatchers.status()
                                .isUnauthorized());
        http.perform(
                        org.springframework.test.web.servlet.request.MockMvcRequestBuilders.get(
                                        "/v1/memory")
                                .header("Authorization", "Bearer m4-memory-token"))
                .andExpect(
                        org.springframework.test.web.servlet.result.MockMvcResultMatchers.content()
                                .json("[]"));
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
        assertThat(db.getFactById(fact.id()).status()).isEqualTo("candidate");
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
                new MemoryProcessingService(
                        repository,
                        tx,
                        text -> {
                            throw new AssertionError();
                        },
                        text -> EmbeddingVector.of(new double[] {1}),
                        "test-v1");
        broken.poll();
        assertThat(db.getFactById(f.id()).status()).isEqualTo("active");
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
        var original = db.getSourceById(job.keyId());
        assertThat(memory.forget(alice, 1, "forget")).isEqualTo(2);
        assertThat(memory.forget(alice, 1, "forget")).isEqualTo(2);
        explicit()
                .commitExtraction(
                        job,
                        original,
                        new Extraction(
                                List.of(new Proposal("response_style", "简洁", original.content()))));
        assertThat(memory.list(alice)).isEmpty();
        assertThat(db.getFactById(f.id()).content()).isNull();
        assertThat(db.getSourceById(job.keyId()).content()).isNull();
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
                new MemoryProcessingService(
                        repository,
                        tx,
                        text -> null,
                        text -> {
                            entered.countDown();
                            try {
                                if (!release.await(10, TimeUnit.SECONDS))
                                    throw new IllegalStateException("Embedding wait timed out");
                            } catch (InterruptedException e) {
                                Thread.currentThread().interrupt();
                                throw new IllegalStateException(e);
                            }
                            return embedding();
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
    void atomicClaimsSkipLockedRowsAndReturnTheUpdatedLease() throws Exception {
        long first = queuedJob("locked-job");
        long second = queuedJob("available-job");
        var worker = explicit();
        try (var connection = source.getConnection()) {
            connection.setAutoCommit(false);
            try (var statement =
                    connection.prepareStatement(
                            "SELECT id FROM memory.jobs WHERE id=? FOR UPDATE")) {
                statement.setLong(1, first);
                statement.executeQuery().close();
            }
            var claimed = worker.claim();
            assertThat(claimed.id()).isEqualTo(second);
            assertThat(claimed.status()).isEqualTo("running");
            assertThat(claimed.attempts()).isEqualTo(1);
            assertThat(claimed.leaseToken()).isNotBlank();
            assertThat(worker.claim()).isNull();
            connection.commit();
        }
        assertThat(worker.claim().id()).isEqualTo(first);
        assertThat(worker.claim()).isNull();
    }

    @Test
    void concurrentAtomicClaimsGiveEachJobToOneWorker() throws Exception {
        Set<Long> expected = new HashSet<>();
        for (int n = 0; n < 12; n++) expected.add(queuedJob("parallel-" + n));
        try (var pool = Executors.newVirtualThreadPerTaskExecutor()) {
            var futures = new ArrayList<Future<Job>>();
            for (int n = 0; n < 12; n++) futures.add(pool.submit(() -> explicit().claim()));
            var claimed = new ArrayList<Job>();
            for (var future : futures) claimed.add(future.get(10, TimeUnit.SECONDS));
            assertThat(claimed).extracting(Job::id).containsExactlyInAnyOrderElementsOf(expected);
            assertThat(claimed).allSatisfy(job -> assertThat(job.attempts()).isEqualTo(1));
            assertThat(claimed.stream().map(Job::leaseToken).distinct()).hasSize(12);
            assertThat(explicit().claim()).isNull();
        }
    }

    @Test
    void lockedLeaseQueryRejectsForeignTokensAndChangedJobMetadata() {
        queuedJob("lease-check");
        var job = explicit().claim();
        assertThat((Boolean) tx.execute(s -> repository.hasLiveJobLease(job))).isTrue();
        var foreign =
                new Job(
                        job.id(),
                        job.kind(),
                        job.keyId(),
                        job.tenant(),
                        "another-owner",
                        job.epoch(),
                        job.revision(),
                        job.modelRevision(),
                        job.status(),
                        job.attempts(),
                        job.leaseToken());
        assertThat((Boolean) tx.execute(s -> repository.hasLiveJobLease(foreign))).isFalse();
        sql.update("UPDATE memory.jobs SET lease_token='new-worker' WHERE id=?", job.id());
        assertThat((Boolean) tx.execute(s -> repository.hasLiveJobLease(job))).isFalse();
        sql.update(
                "UPDATE memory.jobs SET lease_token=?,epoch=epoch+1 WHERE id=?",
                job.leaseToken(),
                job.id());
        assertThat((Boolean) tx.execute(s -> repository.hasLiveJobLease(job))).isFalse();
    }

    @Test
    void leaseExpiryDuringJobLockWaitIsCheckedAfterTheLockIsAcquired() throws Exception {
        db.insertOwnerIfAbsent(alice);
        queuedJob("expires-while-waiting");
        var job = explicit().claim();
        sql.update(
                "UPDATE memory.jobs SET lease_until=clock_timestamp()+interval '2 seconds' WHERE id=?",
                job.id());
        var entered = new CountDownLatch(1);
        try (var pool = Executors.newVirtualThreadPerTaskExecutor();
                var connection = source.getConnection()) {
            connection.setAutoCommit(false);
            try (var statement =
                    connection.prepareStatement(
                            "SELECT id FROM memory.jobs WHERE id=? FOR UPDATE")) {
                statement.setLong(1, job.id());
                statement.executeQuery().close();
            }
            var checked =
                    pool.submit(
                            () ->
                                    tx.execute(
                                            s -> {
                                                db.getOwnerPrivacyForUpdate(alice);
                                                entered.countDown();
                                                return repository.hasLiveJobLease(job);
                                            }));
            assertThat(entered.await(10, TimeUnit.SECONDS)).isTrue();
            long deadline = System.nanoTime() + TimeUnit.SECONDS.toNanos(10);
            while (sql.queryForObject(
                                    "SELECT count(*) FROM pg_stat_activity WHERE pid<>pg_backend_pid()"
                                            + " AND wait_event_type='Lock' AND query LIKE '%WITH locked AS MATERIALIZED%'",
                                    Integer.class)
                            == 0
                    && System.nanoTime() < deadline) Thread.sleep(10);
            assertThat(
                            sql.queryForObject(
                                    "SELECT count(*) FROM pg_stat_activity WHERE pid<>pg_backend_pid()"
                                            + " AND wait_event_type='Lock' AND query LIKE '%WITH locked AS MATERIALIZED%'",
                                    Integer.class))
                    .isPositive();
            assertThat(checked.isDone()).isFalse();
            while (!sql.queryForObject(
                            "SELECT lease_until<=clock_timestamp() FROM memory.jobs WHERE id=?",
                            Boolean.class,
                            job.id())
                    && System.nanoTime() < deadline) Thread.sleep(10);
            assertThat(
                            sql.queryForObject(
                                    "SELECT lease_until<=clock_timestamp() FROM memory.jobs WHERE id=?",
                                    Boolean.class,
                                    job.id()))
                    .isTrue();
            connection.commit();
            assertThat(checked.get(10, TimeUnit.SECONDS)).isFalse();
        }
    }

    private long queuedJob(String key) {
        return sql.queryForObject(
                "INSERT INTO memory.jobs(kind,key_id,tenant,subject,epoch) VALUES('extract',?,?,?,1) RETURNING id",
                Long.class,
                key,
                alice.tenant(),
                alice.subject());
    }

    @Test
    void vectorSearchChecksOwnerEpochRevisionAndPinnedModel() throws Exception {
        var f = preference();
        explicit().poll();
        String vector = MemoryVectorCodec.query(embedding());
        assertThat(db.listSimilarActiveFacts(alice, "test-v1", vector))
                .extracting(Fact::id)
                .containsExactly(f.id());
        assertThat(db.listSimilarActiveFacts(bob, "test-v1", vector)).isEmpty();
        assertThat(db.listSimilarActiveFacts(alice, "other-model", vector)).isEmpty();
        sql.update("UPDATE memory.projections SET revision=revision+1");
        assertThat(db.listSimilarActiveFacts(alice, "test-v1", vector)).isEmpty();
        memory.rebuild(alice, "rebuild-1");
        explicit().poll();
        assertThat(db.listSimilarActiveFacts(alice, "test-v1", vector)).hasSize(1);
        memory.forget(alice, 1, "erase");
        assertThat(db.listSimilarActiveFacts(alice, "test-v1", vector)).isEmpty();
    }

    @Test
    void memoryDatabaseRoleCannotReadBusinessStateOrChannel() throws Exception {
        sql.execute(
                "DO $$ BEGIN IF NOT EXISTS(SELECT FROM pg_roles WHERE rolname='financeclaw_memory') THEN CREATE ROLE financeclaw_memory LOGIN PASSWORD 'test-memory-only'; END IF; END $$");
        sql.execute(
                java.nio.file.Files.readString(
                                java.nio.file.Path.of("ops/memory-worker-grants.sql"))
                        .replace("ON DATABASE financeclaw_java", "ON DATABASE m4"));
        var restricted =
                new JdbcTemplate(
                        new DriverManagerDataSource(
                                M4Database.PG.getJdbcUrl(),
                                "financeclaw_memory",
                                "test-memory-only"));
        assertThat(restricted.queryForObject("SELECT count(*) FROM memory.jobs", Integer.class))
                .isZero();
        for (var table : List.of("business.turns", "agent_runtime.states", "channel.bindings"))
            assertThatThrownBy(() -> restricted.queryForList("SELECT * FROM " + table))
                    .isInstanceOf(org.springframework.dao.DataAccessException.class);
        assertThatThrownBy(() -> restricted.execute("CREATE TABLE memory.forbidden(id int)"))
                .isInstanceOf(org.springframework.dao.DataAccessException.class);
    }
}
