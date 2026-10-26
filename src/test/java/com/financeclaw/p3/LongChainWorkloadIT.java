package com.financeclaw.p3;

import static org.assertj.core.api.Assertions.*;

import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import com.financeclaw.app.agent.AgentService;
import com.financeclaw.app.agent.port.ResourceAdministration;
import com.financeclaw.app.release.ReleaseService;
import com.financeclaw.app.task.CancelTaskService;
import com.financeclaw.app.task.TaskEventHub;
import com.financeclaw.bootstrap.FinanceClawApplication;
import com.financeclaw.domain.ExecutionScope;
import com.financeclaw.domain.Task.*;
import com.financeclaw.domain.agent.AgentComposition;
import com.financeclaw.domain.release.*;
import com.financeclaw.infrastructure.agentscope.skills.PackagedSkills;
import com.financeclaw.infrastructure.task.HeartbeatResources;
import com.financeclaw.support.PayloadJson;
import java.net.URI;
import java.net.http.*;
import java.nio.file.*;
import java.time.Duration;
import java.util.*;
import java.util.concurrent.*;
import java.util.concurrent.atomic.*;
import javax.sql.DataSource;
import org.junit.jupiter.api.*;
import org.junit.jupiter.api.parallel.Execution;
import org.junit.jupiter.api.parallel.ExecutionMode;
import org.springframework.beans.factory.config.BeanPostProcessor;
import org.springframework.boot.WebApplicationType;
import org.springframework.boot.builder.SpringApplicationBuilder;
import org.springframework.boot.test.context.TestConfiguration;
import org.springframework.boot.web.servlet.context.ServletWebServerApplicationContext;
import org.springframework.context.ConfigurableApplicationContext;
import org.springframework.context.annotation.Bean;
import org.springframework.jdbc.core.JdbcTemplate;
import org.testcontainers.containers.PostgreSQLContainer;

/**
 * 固定长链和有界持续负载的实测，不声称生产容量或优化降幅。 PG 测试触发器记录每次已提交 JSONB 新行映像的 UTF-8 逻辑字节；不是 WAL、磁盘占用或 JDBC 网络字节。
 * 原始观察查询、触发器内部语句不计入客户端 JDBC execute 数，触发器与采样自身有测量开销。
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
@Execution(ExecutionMode.SAME_THREAD)
class LongChainWorkloadIT {
    private static final String MARKER = "WORKLOAD_INPUT_MARKER_";
    private static final List<Owner> OWNERS =
            java.util.stream.IntStream.range(0, 4)
                    .mapToObj(i -> new Owner("long-chain-load", "owner-" + i))
                    .toList();
    private final ObjectMapper json = new ObjectMapper().findAndRegisterModules();
    private final PostgreSQLContainer<?> postgres =
            new PostgreSQLContainer<>("postgres:16.10-alpine");
    private final List<Map<String, Object>> results = new ArrayList<>();
    private final List<Admitted> admitted = new CopyOnWriteArrayList<>();
    private ConfigurableApplicationContext application;
    private PrivateAgentLoopback model;
    private HttpClient http;
    private JdbcTemplate raw;
    private ApplicationJdbcRecorder recorder;
    private TaskEventHub events;
    private String api;
    private final AtomicLong excludedQueries = new AtomicLong();

    record Agent(String id, String hash) {}

    record Admitted(int ownerIndex, String conversation, String id) {}

    static final class RawSource {
        DataSource value;
    }

    @TestConfiguration(proxyBeanMethods = false)
    static class ProbeConfiguration {
        @Bean
        static ApplicationJdbcRecorder jdbcRecorder() {
            return new ApplicationJdbcRecorder(new ApplicationJdbcRecorder.Limits(100_000, 2048));
        }

        @Bean
        static RawSource rawSource() {
            return new RawSource();
        }

        @Bean
        static BeanPostProcessor recordJdbc(ApplicationJdbcRecorder recorder, RawSource raw) {
            return new BeanPostProcessor() {
                @Override
                public Object postProcessAfterInitialization(Object bean, String name) {
                    if (name.equals("dataSource") && bean instanceof DataSource source) {
                        raw.value = source;
                        return recorder.wrap(source);
                    }
                    if (bean instanceof HeartbeatResources resources)
                        return resources.decorate(recorder::wrap);
                    return bean;
                }
            };
        }
    }

    @BeforeAll
    void start() throws Exception {
        try {
            postgres.start();
            model = new PrivateAgentLoopback();
            http =
                    HttpClient.newBuilder()
                            .version(HttpClient.Version.HTTP_1_1)
                            .connectTimeout(Duration.ofSeconds(5))
                            .build();
            var identities = new ArrayList<Map<String, Object>>();
            for (int i = 0; i < OWNERS.size(); i++)
                identities.add(
                        Map.of(
                                "token",
                                token(i),
                                "tenant",
                                OWNERS.get(i).tenant(),
                                "subject",
                                OWNERS.get(i).subject(),
                                "roles",
                                List.of()));
            String credentials =
                    PayloadJson.canonical(
                            List.of(
                                    Map.of(
                                            "resourceId",
                                            "load-model",
                                            "reference",
                                            "@model",
                                            "tenant",
                                            OWNERS.getFirst().tenant(),
                                            "subject",
                                            OWNERS.getFirst().subject())));
            application =
                    new SpringApplicationBuilder(
                                    FinanceClawApplication.class, ProbeConfiguration.class)
                            .web(WebApplicationType.SERVLET)
                            .run(
                                    "--spring.datasource.url=" + postgres.getJdbcUrl(),
                                    "--spring.datasource.username=" + postgres.getUsername(),
                                    "--spring.datasource.password=" + postgres.getPassword(),
                                    "--spring.config.import=",
                                    "--server.port=0",
                                    "--spring.main.banner-mode=off",
                                    "--financeclaw.auth.token=",
                                    "--financeclaw.auth.identities-json="
                                            + PayloadJson.canonical(identities),
                                    "--financeclaw.auth.credential-resources-json=" + credentials,
                                    "--financeclaw.worker.enabled=true",
                                    "--financeclaw.worker.parallelism=4",
                                    "--financeclaw.scheduler.dispatch-enabled=true",
                                    "--financeclaw.worker.poll-ms=50",
                                    "--financeclaw.worker.idle-max-ms=100",
                                    "--financeclaw.worker.lease-ms=10000",
                                    "--financeclaw.worker.heartbeat-ms=200",
                                    "--financeclaw.integrations.enabled=false",
                                    "--financeclaw.memory.enabled=false",
                                    "--financeclaw.memory.profile-enabled=false",
                                    "--financeclaw.memory.forget-ledger.enabled=false",
                                    "--financeclaw.model.name=offline",
                                    "--financeclaw.model.api-key=synthetic-load-key",
                                    "--financeclaw.fallback.name=",
                                    "--financeclaw.feishu.enabled=false",
                                    "--financeclaw.context.work-context-bytes=65536",
                                    "--financeclaw.business.hotel.enabled=false",
                                    "--financeclaw.business.taibu.enabled=false",
                                    "--financeclaw.business.ziwei.enabled=false");
            raw = new JdbcTemplate(application.getBean(RawSource.class).value);
            recorder = application.getBean(ApplicationJdbcRecorder.class);
            events = application.getBean(TaskEventHub.class);
            api =
                    "http://127.0.0.1:"
                            + ((ServletWebServerApplicationContext) application)
                                    .getWebServer()
                                    .getPort();
            var access = application.getBean(ResourceAdministration.class);
            for (int i = 1; i < OWNERS.size(); i++)
                access.change(
                        OWNERS.getFirst(),
                        "credential",
                        "load-model",
                        OWNERS.get(i),
                        null,
                        0,
                        true);
            installWriteProbe();
        } catch (Exception | Error failure) {
            close();
            throw failure;
        }
    }

    @AfterEach
    void cleanup() throws Exception {
        if (application == null) return;
        model.releaseAll();
        for (var turn : admitted) {
            if (!read(turn).terminal())
                application
                        .getBean(CancelTaskService.class)
                        .cancel(
                                OWNERS.get(turn.ownerIndex()),
                                turn.conversation(),
                                turn.id(),
                                UUID.randomUUID().toString());
        }
        drain();
        model.awaitIdle(Duration.ofSeconds(10));
        assertThat(model.failures()).as("synthetic SSE protocol failures").isEmpty();
    }

    @AfterAll
    void reportAndClose() throws Exception {
        try {
            Path output = Path.of("target/p3a21/long-chain-load.json");
            Files.createDirectories(output.getParent());
            var report = new LinkedHashMap<String, Object>();
            report.put("schema", "long-chain-workload/1");
            report.put(
                    "runtime",
                    "real-spring-http-multi-identity-worker-sdk-postgresql-loopback-model");
            report.put(
                    "byte_metric",
                    "sum of committed JSONB row-image UTF-8 text bytes at each INSERT/UPDATE; payload and transcript separately");
            report.put(
                    "not_measured",
                    List.of(
                            "physical storage bytes",
                            "WAL bytes",
                            "network bytes",
                            "external model service",
                            "production capacity"));
            report.put(
                    "instrumentation",
                    "test-only PostgreSQL AFTER triggers and raw observer queries add overhead; rolled-back row writes are excluded");
            report.put(
                    "sql_metric",
                    "client JDBC execute attempts, excluding fixture observer and PostgreSQL trigger-internal statements");
            report.put("work_context_budget_bytes", 65536);
            report.put(
                    "context_measurement",
                    "fixed 64KiB work budget isolates repeated snapshot writes; context compression is outside this fixture");
            report.put("worker_parallelism", 4);
            report.put("lease_ms", 10000);
            report.put("heartbeat_ms", 200);
            report.put("fair_dispatch_enabled", true);
            report.put("observations", results);
            report.put("observer_queries_excluded", excludedQueries.get());
            json.writerWithDefaultPrettyPrinter().writeValue(output.toFile(), report);
        } finally {
            close();
        }
    }

    @Test
    void committedSdkAndWorkContextBytesAcrossOneEightAndTwentyFourToolChains() throws Exception {
        for (int steps : new int[] {1, 8, 24}) {
            reset();
            var agent = publish(0, steps);
            script(steps, 25);
            capture(
                    "chain_" + steps,
                    steps,
                    () -> {
                        execute(0, agent, 0);
                        assertThat(model.requests(PrivateAgentLoopback.PARENT)).hasSize(steps + 1);
                        assertTotals(1, steps);
                        assertOwnerIsolation(Map.of(0, agent));
                    });
        }
    }

    @Test
    void fourAuthenticatedOwnersSustainIndependentTurnsForSixSeconds() throws Exception {
        reset();
        int steps = 6;
        var targets = new LinkedHashMap<Integer, Agent>();
        for (int i = 0; i < OWNERS.size(); i++) targets.put(i, publish(i, steps));
        script(steps, 75);
        var ownerCompletions = new AtomicIntegerArray(OWNERS.size());
        capture(
                "four_owner_sustained",
                steps,
                () -> {
                    var barrier = new CyclicBarrier(OWNERS.size());
                    try (var pool = Executors.newFixedThreadPool(OWNERS.size())) {
                        var tasks = new ArrayList<Future<?>>();
                        for (int index = 0; index < OWNERS.size(); index++) {
                            int ownerIndex = index;
                            tasks.add(
                                    pool.submit(
                                            () -> {
                                                barrier.await(5, TimeUnit.SECONDS);
                                                long until =
                                                        System.nanoTime()
                                                                + Duration.ofSeconds(6).toNanos();
                                                do {
                                                    int sequence = ownerCompletions.get(ownerIndex);
                                                    if (sequence >= 32)
                                                        throw new AssertionError(
                                                                "bounded load safety limit reached");
                                                    execute(
                                                            ownerIndex,
                                                            targets.get(ownerIndex),
                                                            sequence);
                                                    ownerCompletions.incrementAndGet(ownerIndex);
                                                } while (System.nanoTime() < until
                                                        || ownerCompletions.get(ownerIndex) < 3);
                                                return null;
                                            }));
                        }
                        for (var task : tasks) task.get(90, TimeUnit.SECONDS);
                    }
                    for (int i = 0; i < OWNERS.size(); i++)
                        assertThat(ownerCompletions.get(i)).isGreaterThanOrEqualTo(3);
                    assertTotals(admitted.size(), steps);
                    assertOwnerIsolation(targets);
                });
        results.getLast()
                .put(
                        "completed_per_owner",
                        java.util.stream.IntStream.range(0, OWNERS.size())
                                .map(ownerCompletions::get)
                                .boxed()
                                .toList());
    }

    private void script(int steps, int modelDelayMs) {
        model.reset(
                request -> {
                    assertThat(request.model()).isEqualTo(PrivateAgentLoopback.PARENT);
                    assertThat(request.toolNames())
                            .containsExactlyInAnyOrder("market_snapshot", "read_artifact_range");
                    String serialized = request.serialized();
                    assertThat(
                                    OWNERS.stream()
                                            .filter(
                                                    owner ->
                                                            serialized.contains(
                                                                    "OWNER_MARKER_"
                                                                            + OWNERS.indexOf(
                                                                                    owner)))
                                            .count())
                            .isEqualTo(1);
                    long completed =
                            java.util.stream.StreamSupport.stream(
                                            request.payload().path("messages").spliterator(), false)
                                    .filter(
                                            m ->
                                                    m.path("role").asText().equals("tool")
                                                            && m.path("tool_call_id")
                                                                    .asText()
                                                                    .startsWith("workload-step-"))
                                    .count();
                    try {
                        Thread.sleep(modelDelayMs);
                    } catch (InterruptedException stop) {
                        Thread.currentThread().interrupt();
                        throw new IllegalStateException(stop);
                    }
                    if (completed < steps)
                        return PrivateAgentLoopback.Reply.tool(
                                "workload-step-" + (completed + 1),
                                "market_snapshot",
                                Map.of("symbol", "AAPL"));
                    assertThat(completed).isEqualTo(steps);
                    return PrivateAgentLoopback.Reply.text("WORKLOAD_COMPLETED");
                });
    }

    private Agent publish(int index, int steps) {
        var releases = application.getBean(ReleaseService.class);
        releases.publish(
                new ReleaseDefinition(
                        "write-load",
                        "finance_agent",
                        ReleaseDefinition.SCOPES,
                        new ModelProfile(
                                PrivateAgentLoopback.PARENT,
                                model.baseUrl(),
                                131072,
                                262144,
                                8192,
                                512,
                                false),
                        null,
                        steps + 3,
                        steps + 3,
                        new BusinessFeatures(
                                List.of(),
                                PackagedSkills.hash(),
                                PayloadJson.canonical(
                                        Map.of(
                                                "AAPL",
                                                Map.of(
                                                        "price",
                                                        "123.45",
                                                        "currency",
                                                        "USD",
                                                        "provider",
                                                        "load-fixture",
                                                        "as_of",
                                                        "2026-10-26T00:00:00Z"))),
                                null)));
        var agents = application.getBean(AgentService.class);
        String id = UUID.randomUUID().toString();
        agents.create(
                OWNERS.get(index),
                id,
                "Load fixture",
                "Use the selected market tool then answer.",
                new AgentComposition("primary", List.of("function:market_snapshot")));
        return new Agent(id, agents.publish(OWNERS.get(index), id, 1, 0).releaseHash());
    }

    private void execute(int ownerIndex, Agent agent, int sequence) throws Exception {
        String conversation =
                post(ownerIndex, "/v1/conversations", Map.of("agent_id", agent.id()), 201)
                        .path("conversation_id")
                        .asText();
        String message =
                MARKER
                        + " OWNER_MARKER_"
                        + ownerIndex
                        + " sequence="
                        + sequence
                        + " "
                        + "测量输入".repeat(512);
        var response =
                post(
                        ownerIndex,
                        "/v1/conversations/" + conversation + "/turns",
                        Map.of("message", message),
                        202);
        var turn = new Admitted(ownerIndex, conversation, response.path("turn_id").asText());
        admitted.add(turn);
        var current = read(turn);
        try (var subscription = events.subscribe(current, 1024)) {
            var terminal =
                    subscription
                            .events()
                            .filter(
                                    e ->
                                            List.of("completed", "blocked", "failed", "cancelled")
                                                    .contains(e.status()))
                            .next()
                            .toFuture();
            var afterSubscribe = read(turn);
            if (!afterSubscribe.terminal())
                assertThat(terminal.get(30, TimeUnit.SECONDS).status()).isEqualTo("completed");
        }
        var completed = read(turn);
        assertThat(completed.status()).isEqualTo("completed");
        assertThat(completed.result()).isEqualTo("WORKLOAD_COMPLETED");
        assertThat(completed.releaseHash()).isEqualTo(agent.hash());
        assertThat(completed.agentId()).isEqualTo(agent.id());
        assertThat(completed.tenant()).isEqualTo(OWNERS.get(ownerIndex).tenant());
        assertThat(completed.subject()).isEqualTo(OWNERS.get(ownerIndex).subject());
    }

    @FunctionalInterface
    interface Work {
        void run() throws Exception;
    }

    private void capture(String phase, int steps, Work work) throws Exception {
        var row = new LinkedHashMap<String, Object>();
        row.put("phase", phase);
        row.put("tool_steps_per_turn", steps);
        long started = System.nanoTime(), before = excludedQueries.get();
        var window = recorder.begin(phase);
        Throwable failure = null;
        var sampling = new AtomicBoolean(true);
        var samples = new CopyOnWriteArrayList<Map<String, Object>>();
        var sampler = Executors.newSingleThreadExecutor();
        var samplerTask =
                sampler.submit(
                        () -> {
                            while (sampling.get()) {
                                samples.add(
                                        query(
                                                "SELECT count(*) AS running,min(extract(epoch FROM (lease_until-clock_timestamp()))*1000) AS minimum_lease_ms FROM business.commands WHERE status='running'"));
                                var lock =
                                        query(
                                                "SELECT count(*) AS lock_waiters FROM pg_stat_activity WHERE datname=current_database() AND wait_event_type='Lock'");
                                samples.getLast().put("lock_waiters", lock.get("lock_waiters"));
                                Thread.sleep(50);
                            }
                            return null;
                        });
        try {
            work.run();
            drain();
            assertThat(model.failures()).isEmpty();
        } catch (Exception | Error problem) {
            failure = problem;
        } finally {
            sampling.set(false);
            try {
                samplerTask.get(10, TimeUnit.SECONDS);
            } catch (Exception problem) {
                if (failure == null) failure = problem;
                else failure.addSuppressed(problem);
            }
            sampler.shutdownNow();
            try {
                var measured = window.finish(Duration.ofSeconds(10));
                row.put(
                        "sql",
                        Map.of(
                                "statements",
                                measured.statements(),
                                "families",
                                measured.families(),
                                "roles",
                                measured.roles(),
                                "commits",
                                measured.commits(),
                                "rollbacks",
                                measured.rollbacks(),
                                "borrow",
                                measured.borrows()));
                row.put(
                        "heartbeat_jdbc_nanos",
                        latency(
                                measured.executions().stream()
                                        .filter(e -> e.family().equals("heartbeat_renew"))
                                        .map(ApplicationJdbcRecorder.SqlExecution::elapsedNanos)
                                        .toList()));
                assertThat(measured.executions())
                        .allMatch(ApplicationJdbcRecorder.SqlExecution::success);
                assertThat(measured.commitFailures()).isZero();
                assertThat(measured.rollbackFailures()).isZero();
                excludedQueries.incrementAndGet();
                row.put(
                        "row_writes",
                        raw.queryForList(
                                "SELECT relation,count(*) AS rewrites,sum(payload_bytes) AS payload_utf8_bytes,sum(transcript_bytes) AS transcript_utf8_bytes,sum(input_marker_copies) AS input_marker_copies FROM workload_probe.writes GROUP BY relation ORDER BY relation"));
                long written =
                        number(
                                "SELECT COALESCE(sum(payload_bytes+transcript_bytes),0) FROM workload_probe.writes");
                long retained =
                        number(
                                        "SELECT COALESCE(sum(octet_length(convert_to(payload::text,'UTF8'))),0) FROM agent_runtime.states WHERE NOT deleted")
                                + number(
                                        "SELECT COALESCE(sum(octet_length(convert_to(payload::text,'UTF8'))+octet_length(convert_to(transcript::text,'UTF8'))),0) FROM agent_runtime.work_contexts");
                row.put("committed_logical_bytes_written", written);
                row.put("retained_logical_bytes_after", retained);
                row.put(
                        "written_to_retained_ratio",
                        retained == 0 ? 0d : (double) written / retained);
                assertThat(written).isPositive().isGreaterThanOrEqualTo(retained);
                assertThat(retained).isPositive();
                assertThat(number("SELECT count(DISTINCT relation) FROM workload_probe.writes"))
                        .isEqualTo(2);
                assertThat(number("SELECT sum(input_marker_copies) FROM workload_probe.writes"))
                        .isPositive();
                var margins =
                        samples.stream()
                                .map(s -> s.get("minimum_lease_ms"))
                                .filter(Objects::nonNull)
                                .map(v -> ((Number) v).doubleValue())
                                .toList();
                row.put("lease_margin_samples", margins.size());
                if (!margins.isEmpty()) {
                    row.put("minimum_sampled_lease_margin_ms", Collections.min(margins));
                    assertThat(Collections.min(margins)).isPositive();
                }
                row.put(
                        "max_sampled_running",
                        samples.stream()
                                .mapToLong(s -> ((Number) s.get("running")).longValue())
                                .max()
                                .orElse(0));
                row.put(
                        "max_sampled_lock_waiters",
                        samples.stream()
                                .mapToLong(
                                        s ->
                                                ((Number) s.getOrDefault("lock_waiters", 0))
                                                        .longValue())
                                .max()
                                .orElse(0));
            } catch (Exception | Error problem) {
                if (failure == null) failure = problem;
                else failure.addSuppressed(problem);
            }
            row.put("admitted_turns", admitted.size());
            try {
                row.put(
                        "completed_turns",
                        number("SELECT count(*) FROM business.turns WHERE status='completed'"));
            } catch (Exception problem) {
                if (failure == null) failure = problem;
                else failure.addSuppressed(problem);
            }
            row.put("elapsed_nanos", System.nanoTime() - started);
            row.put("observer_queries_excluded", excludedQueries.get() - before);
            row.put("completed", failure == null);
            if (failure != null) row.put("failure_type", failure.getClass().getSimpleName());
            results.add(row);
        }
        if (failure instanceof Error error) throw error;
        if (failure instanceof Exception problem) throw problem;
    }

    private void assertTotals(int turns, int steps) {
        assertThat(number("SELECT count(*) FROM business.turns WHERE status='completed'"))
                .isEqualTo(turns);
        assertThat(number("SELECT count(*) FROM business.commands WHERE status='done'"))
                .isEqualTo(turns);
        assertThat(number("SELECT count(*) FROM business.attempts WHERE outcome='completed'"))
                .isEqualTo(turns);
        assertThat(number("SELECT sum(model_calls) FROM business.turn_policies"))
                .isEqualTo((long) turns * (steps + 1));
        assertThat(number("SELECT sum(tool_calls) FROM business.turn_policies"))
                .isEqualTo((long) turns * steps);
        assertThat(number("SELECT count(*) FROM agent_runtime.unsafe_execution_data()")).isZero();
    }

    private void assertOwnerIsolation(Map<Integer, Agent> targets) {
        for (var item : targets.entrySet()) {
            int index = item.getKey();
            var owner = OWNERS.get(index);
            var agent = item.getValue();
            var scope =
                    new ExecutionScope(
                            owner.tenant(),
                            owner.subject(),
                            "namespace-only",
                            agent.id(),
                            agent.hash());
            excludedQueries.incrementAndGet();
            String sdk =
                    String.join(
                            " ",
                            raw.queryForList(
                                    "SELECT payload::text FROM agent_runtime.states WHERE namespace=? AND user_id=? AND NOT deleted",
                                    String.class,
                                    scope.namespace(),
                                    scope.userId()));
            excludedQueries.incrementAndGet();
            String work =
                    String.join(
                            " ",
                            raw.queryForList(
                                    "SELECT payload::text || transcript::text FROM agent_runtime.work_contexts WHERE tenant=? AND subject=?",
                                    String.class,
                                    owner.tenant(),
                                    owner.subject()));
            assertThat(sdk).contains("OWNER_MARKER_" + index);
            assertThat(work).contains("OWNER_MARKER_" + index);
            for (int other : targets.keySet())
                if (other != index) {
                    assertThat(sdk).doesNotContain("OWNER_MARKER_" + other);
                    assertThat(work).doesNotContain("OWNER_MARKER_" + other);
                }
        }
    }

    private void installWriteProbe() {
        raw.execute("CREATE SCHEMA workload_probe");
        raw.execute(
                "CREATE TABLE workload_probe.writes(relation text NOT NULL,payload_bytes bigint NOT NULL,transcript_bytes bigint NOT NULL,input_marker_copies bigint NOT NULL)");
        raw.execute(
                """
            CREATE FUNCTION workload_probe.record_write() RETURNS trigger LANGUAGE plpgsql AS $$
            DECLARE payload_text text; transcript_text text; marker text := 'WORKLOAD_INPUT_MARKER_';
            BEGIN
              payload_text := COALESCE(NEW.payload::text,'');
              IF TG_TABLE_NAME='work_contexts' THEN transcript_text := COALESCE(NEW.transcript::text,''); ELSE transcript_text := ''; END IF;
              IF payload_text <> '' THEN
                INSERT INTO workload_probe.writes VALUES (TG_TABLE_NAME,octet_length(convert_to(payload_text,'UTF8')),
                  octet_length(convert_to(transcript_text,'UTF8')),
                  ((length(payload_text)-length(replace(payload_text,marker,'')))+
                   (length(transcript_text)-length(replace(transcript_text,marker,''))))/length(marker));
              END IF;
              RETURN NEW;
            END $$
            """);
        for (String table : List.of("states", "work_contexts"))
            raw.execute(
                    "CREATE TRIGGER logical_write_probe AFTER INSERT OR UPDATE ON agent_runtime."
                            + table
                            + " FOR EACH ROW EXECUTE FUNCTION workload_probe.record_write()");
    }

    private void reset() throws Exception {
        drain();
        admitted.clear();
        model.awaitIdle(Duration.ofSeconds(5));
        raw.execute(
                "TRUNCATE agent_runtime.states,business.conversations,memory.owners,memory.jobs,memory.operations,memory.audit CASCADE");
        raw.update(
                "UPDATE memory.privacy_ledger_binding SET lineage=?,active=false",
                UUID.randomUUID().toString());
        raw.execute("TRUNCATE workload_probe.writes");
    }

    private JsonNode post(int owner, String path, Object body, int status) throws Exception {
        var response =
                http.send(
                        HttpRequest.newBuilder(URI.create(api + path))
                                .timeout(Duration.ofSeconds(15))
                                .header("Authorization", "Bearer " + token(owner))
                                .header("Content-Type", "application/json")
                                .header("Idempotency-Key", UUID.randomUUID().toString())
                                .POST(
                                        HttpRequest.BodyPublishers.ofByteArray(
                                                json.writeValueAsBytes(body)))
                                .build(),
                        HttpResponse.BodyHandlers.ofByteArray());
        assertThat(response.statusCode()).isEqualTo(status);
        return json.readTree(response.body());
    }

    private Turn read(Admitted turn) {
        excludedQueries.incrementAndGet();
        return raw.queryForObject(
                "SELECT * FROM business.turns WHERE id=?",
                (r, n) ->
                        new Turn(
                                r.getString("id"),
                                r.getString("conversation_id"),
                                r.getString("tenant"),
                                r.getString("subject"),
                                r.getString("idempotency_key"),
                                r.getString("request_hash"),
                                r.getString("message"),
                                r.getString("release_hash"),
                                r.getString("status"),
                                r.getLong("revision"),
                                r.getString("result"),
                                r.getString("reason"),
                                r.getString("agent_id")),
                turn.id());
    }

    private Map<String, Object> query(String sql) {
        excludedQueries.incrementAndGet();
        return raw.queryForMap(sql);
    }

    private long number(String sql) {
        excludedQueries.incrementAndGet();
        return raw.queryForObject(sql, Long.class);
    }

    private static Map<String, Long> latency(List<Long> values) {
        if (values.isEmpty()) return Map.of("count", 0L);
        var sorted = values.stream().sorted().toList();
        return Map.of(
                "count",
                (long) sorted.size(),
                "p50",
                sorted.get((sorted.size() - 1) / 2),
                "p95",
                sorted.get((int) Math.ceil(sorted.size() * .95) - 1),
                "max",
                sorted.getLast());
    }

    private static String token(int index) {
        return "synthetic-workload-owner-token-" + index;
    }

    private void drain() throws InterruptedException {
        if (raw == null) return;
        long until = System.nanoTime() + Duration.ofSeconds(30).toNanos();
        while (number("SELECT count(*) FROM business.commands WHERE status<>'done'") != 0) {
            if (System.nanoTime() > until) throw new AssertionError("commands did not drain");
            Thread.sleep(30);
        }
    }

    private void close() {
        if (model != null) model.releaseAll();
        try {
            if (application != null) application.close();
        } finally {
            try {
                if (http != null) http.close();
            } finally {
                try {
                    if (model != null) model.close();
                } finally {
                    postgres.stop();
                }
            }
        }
    }
}
