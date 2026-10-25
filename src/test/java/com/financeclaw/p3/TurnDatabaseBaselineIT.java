package com.financeclaw.p3;

import static org.assertj.core.api.Assertions.*;

import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import com.fasterxml.jackson.databind.SerializationFeature;
import com.financeclaw.app.agent.AgentService;
import com.financeclaw.app.memory.UserProfileService;
import com.financeclaw.app.release.ReleaseService;
import com.financeclaw.app.task.CancelTaskService;
import com.financeclaw.app.task.SubmitTaskService;
import com.financeclaw.app.task.TaskEventHub;
import com.financeclaw.bootstrap.FinanceClawApplication;
import com.financeclaw.domain.ExecutionScope;
import com.financeclaw.domain.Task.Event;
import com.financeclaw.domain.Task.Owner;
import com.financeclaw.domain.Task.Turn;
import com.financeclaw.domain.agent.AgentComposition;
import com.financeclaw.domain.agent.PrivateAgentTarget;
import com.financeclaw.domain.release.BusinessFeatures;
import com.financeclaw.domain.release.ModelProfile;
import com.financeclaw.domain.release.ReleaseDefinition;
import com.financeclaw.infrastructure.agentscope.capability.MarketToolSchemas;
import com.financeclaw.infrastructure.agentscope.skills.PackagedSkills;
import com.financeclaw.support.PayloadDigest;
import com.financeclaw.support.PayloadJson;
import com.financeclaw.worker.TaskWorker;
import java.net.URI;
import java.net.http.HttpClient;
import java.net.http.HttpRequest;
import java.net.http.HttpResponse;
import java.nio.file.Files;
import java.nio.file.Path;
import java.time.Duration;
import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.UUID;
import java.util.concurrent.CompletableFuture;
import java.util.concurrent.CopyOnWriteArrayList;
import java.util.concurrent.CountDownLatch;
import java.util.concurrent.Executors;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.atomic.AtomicLong;
import java.util.concurrent.atomic.AtomicReference;
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
 * 整个 Turn 的应用 SQL 基线：正常 HTTP/可信应用受理、真实 Worker、SDK 和本机 SSE 模型协议。 观察器覆盖应用的所有线程；独立原始 DataSource 上的
 * fixture/事实核查不计入业务 SQL，另报核查次数。 这不是外部模型、生产延迟、多用户 HTTP 认证或 PostgreSQL 独立锁等待的验收。
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
@Execution(ExecutionMode.SAME_THREAD)
class TurnDatabaseBaselineIT {
    private static final Owner ALICE = new Owner("turn-sql-baseline", "alice");
    private static final Owner BOB = new Owner("turn-sql-baseline", "bob");
    private static final String TOKEN = "synthetic-turn-sql-token";
    private static final String CALL = "baseline-delegate";
    private static final String SNAPSHOT_CALL = "baseline-snapshot";
    private static final String CHILD_MESSAGE = "BASELINE_EXPLICIT_MARKET_MESSAGE";
    private static final String CHILD_ANSWER = "BASELINE_FROZEN_MARKET_ANSWER";
    private static final String SYMBOL = "AAPL";
    private static final Map<String, Object> MARKET_A =
            Map.of(
                    "price",
                    "123.45",
                    "currency",
                    "USD",
                    "provider",
                    "BASELINE_TARGET_PROVIDER",
                    "as_of",
                    "2026-10-20T01:02:03Z");
    private static final Map<String, Object> MARKET_P =
            Map.of(
                    "price",
                    "987.65",
                    "currency",
                    "USD",
                    "provider",
                    "BASELINE_PARENT_PROVIDER",
                    "as_of",
                    "2026-10-21T02:03:04Z");
    private final ObjectMapper json =
            new ObjectMapper()
                    .findAndRegisterModules()
                    .disable(SerializationFeature.WRITE_DATES_AS_TIMESTAMPS)
                    .disable(SerializationFeature.WRITE_DURATIONS_AS_TIMESTAMPS);
    private final PostgreSQLContainer<?> postgres =
            new PostgreSQLContainer<>("postgres:16.10-alpine");
    private final List<Map<String, Object>> measurements = new ArrayList<>();
    private final List<Admitted> admitted = new CopyOnWriteArrayList<>();
    private final AtomicLong excludedQueries = new AtomicLong();
    private ConfigurableApplicationContext application;
    private PrivateAgentLoopback model;
    private HttpClient http;
    private JdbcTemplate raw;
    private ApplicationJdbcRecorder recorder;
    private SubmitTaskService submission;
    private TaskEventHub events;
    private AgentService agents;
    private ReleaseService releases;
    private String api;

    record Agent(String id, String hash) {}

    record Admitted(Owner owner, String conversation, String id) {}

    record Expected(
            Owner owner,
            Agent agent,
            String conversation,
            String prompt,
            String profile,
            String question,
            String answer) {}

    record Observed(TaskEventHub.Subscription subscription, CompletableFuture<Event> terminal)
            implements AutoCloseable {
        @Override
        public void close() {
            subscription.close();
        }
    }

    /** 保存真实池引用，仅供测试核查；生产事务和 MyBatis 必须共享包装后的同一个资源身份。 */
    static final class RawSource {
        private DataSource source;
    }

    @TestConfiguration(proxyBeanMethods = false)
    static class CountingConfiguration {
        @Bean
        static ApplicationJdbcRecorder applicationJdbcRecorder() {
            return new ApplicationJdbcRecorder();
        }

        @Bean
        static RawSource rawSource() {
            return new RawSource();
        }

        @Bean
        static BeanPostProcessor countingDataSource(
                ApplicationJdbcRecorder recorder, RawSource raw) {
            return new BeanPostProcessor() {
                @Override
                public Object postProcessAfterInitialization(Object bean, String name) {
                    if (name.equals("dataSource") && bean instanceof DataSource source) {
                        raw.source = source;
                        return recorder.wrap(source);
                    }
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
            application =
                    new SpringApplicationBuilder(
                                    FinanceClawApplication.class, CountingConfiguration.class)
                            .web(WebApplicationType.SERVLET)
                            .run(
                                    "--spring.datasource.url=" + postgres.getJdbcUrl(),
                                    "--spring.datasource.username=" + postgres.getUsername(),
                                    "--spring.datasource.password=" + postgres.getPassword(),
                                    "--spring.config.import=",
                                    "--server.port=0",
                                    "--spring.main.banner-mode=off",
                                    "--financeclaw.worker.enabled=true",
                                    "--financeclaw.worker.parallelism=4",
                                    "--financeclaw.worker.poll-ms=100",
                                    "--financeclaw.worker.idle-max-ms=100",
                                    "--financeclaw.worker.lease-ms=60000",
                                    "--financeclaw.worker.heartbeat-ms=1000",
                                    "--financeclaw.integrations.enabled=false",
                                    "--financeclaw.memory.enabled=false",
                                    "--financeclaw.memory.profile-enabled=true",
                                    "--financeclaw.memory.profile-directory=",
                                    "--financeclaw.memory.forget-ledger.enabled=false",
                                    "--financeclaw.auth.token=" + TOKEN,
                                    "--financeclaw.auth.tenant=" + ALICE.tenant(),
                                    "--financeclaw.auth.subject=" + ALICE.subject(),
                                    "--financeclaw.model.name=offline",
                                    "--financeclaw.model.api-key=synthetic-loopback-key",
                                    "--financeclaw.fallback.name=",
                                    "--financeclaw.feishu.enabled=false",
                                    "--financeclaw.business.hotel.enabled=false",
                                    "--financeclaw.business.taibu.enabled=false",
                                    "--financeclaw.business.ziwei.enabled=false");
            recorder = application.getBean(ApplicationJdbcRecorder.class);
            var original = application.getBean(RawSource.class).source;
            assertThat(original).isNotNull().isNotSameAs(application.getBean(DataSource.class));
            raw = new JdbcTemplate(original);
            assertThat(
                            application
                                    .getEnvironment()
                                    .getProperty(
                                            "spring.datasource.hikari.maximum-pool-size",
                                            Integer.class))
                    .isEqualTo(8);
            assertThat(application.getBean(TaskWorker.class)).isNotNull();
            submission = application.getBean(SubmitTaskService.class);
            events = application.getBean(TaskEventHub.class);
            agents = application.getBean(AgentService.class);
            releases = application.getBean(ReleaseService.class);
            api =
                    "http://127.0.0.1:"
                            + ((ServletWebServerApplicationContext) application)
                                    .getWebServer()
                                    .getPort();
        } catch (Exception | Error failure) {
            closeResources();
            throw failure;
        }
    }

    @BeforeEach
    void reset() throws Exception {
        waitCommandsFinished();
        admitted.clear();
        raw.execute(
                "TRUNCATE agent_runtime.states,business.conversations,memory.owners,"
                        + "memory.jobs,memory.operations,memory.audit CASCADE");
        raw.update(
                "UPDATE memory.privacy_ledger_binding SET lineage=?,active=false",
                UUID.randomUUID().toString());
        model.reset(
                request -> {
                    throw new AssertionError("Unconfigured model request");
                });
    }

    @AfterEach
    void cleanup() throws Exception {
        // 异常路径也只走正常取消入口，不改 command/fence/attempt 以帮助测试过关。
        model.releaseAll();
        var cancellation = application.getBean(CancelTaskService.class);
        for (var turn : admitted) {
            if (!readTurn(turn).terminal()) {
                cancellation.cancel(
                        turn.owner(), turn.conversation(), turn.id(), UUID.randomUUID().toString());
            }
        }
        waitCommandsFinished();
        model.awaitIdle(Duration.ofSeconds(5));
        assertThat(model.failures()).as("SSE fixture failures").isEmpty();
    }

    @AfterAll
    void stopAndWriteEvidence() throws Exception {
        try {
            Path output = Path.of("target/p3a8/turn-sql-baseline.json");
            Files.createDirectories(output.getParent());
            var report = new LinkedHashMap<String, Object>();
            report.put("schema", "turn-sql-baseline/1");
            report.put("scope", "owned-spring-real-http-worker-loopback-sse");
            report.put("worker_parallelism", 4);
            report.put("application_pool_maximum", 8);
            report.put(
                    "measurement_boundary",
                    "admission-through-after-commit-terminal-and-connection-drain");
            report.put(
                    "sql_semantics",
                    "completed-jdbc-execute-attempts-including-background-and-failures");
            report.put("observer_queries_excluded_total", excludedQueries.get());
            report.put("scenarios", measurements);
            json.writerWithDefaultPrettyPrinter().writeValue(output.toFile(), report);
        } finally {
            closeResources();
        }
    }

    @Test
    void plainHttpTurnRecordsAllApplicationSql() throws Exception {
        String prompt = "BASELINE_PLAIN_PROMPT";
        String profile = "BASELINE_PLAIN_PROFILE";
        String question = "BASELINE_PLAIN_QUESTION";
        String answer = "BASELINE_PLAIN_ANSWER";
        updateProfile(ALICE, profile);
        publishBase(PrivateAgentLoopback.PARENT, 1, Map.of());
        Agent agent = publishAgent(ALICE, prompt, List.of());
        String conversation = createHttpConversation(agent);
        var gateHolder = new AtomicReference<PrivateAgentLoopback.Gate>();
        model.reset(
                request -> {
                    assertThat(request.model()).isEqualTo(PrivateAgentLoopback.PARENT);
                    assertThat(request.toolNames()).isEmpty();
                    assertThat(request.serialized()).contains(prompt, profile, question);
                    return PrivateAgentLoopback.Reply.text(answer).held(gateHolder.get());
                });
        var gate = model.gate();
        gateHolder.set(gate);
        var captured = new AtomicReference<Admitted>();
        measure(
                "plain_http",
                "http-bearer-single-owner",
                window -> {
                    Admitted turn = submitHttp(conversation, question);
                    captured.set(turn);
                    assertThat(gate.awaitEntered(Duration.ofSeconds(5))).isTrue();
                    try (var observer = observe(turn)) {
                        gate.release();
                        awaitCompleted(observer);
                    }
                });
        assertCompleted(captured.get(), agent, answer, 1, 0);
        assertThat(model.requests(PrivateAgentLoopback.PARENT)).hasSize(1);
        assertThat(model.requests(PrivateAgentLoopback.LEAF)).isEmpty();
        assertThat(integer("SELECT count(*) FROM business.child_invocations")).isZero();
        assertHealthyMeasurements();
    }

    @Test
    void marketHttpTurnRecordsParentAndLeafSql() throws Exception {
        updateProfile(ALICE, "BASELINE_MARKET_PARENT_PROFILE");
        publishBase(PrivateAgentLoopback.LEAF, 32, MARKET_A);
        Agent leaf =
                publishAgent(
                        ALICE, "BASELINE_FROZEN_LEAF_PROMPT", List.of("function:market_snapshot"));
        var target =
                new PrivateAgentTarget(
                        leaf.id(), leaf.hash(), PrivateAgentTarget.Profile.MARKET_SNAPSHOT);
        publishBase(PrivateAgentLoopback.PARENT, 4, MARKET_P);
        Agent parent =
                publishAgent(ALICE, "BASELINE_MARKET_PARENT_PROMPT", List.of("agent:" + leaf.id()));
        String conversation = createHttpConversation(parent);
        var gateHolder = new AtomicReference<PrivateAgentLoopback.Gate>();
        model.reset(
                request -> {
                    if (request.model().equals(PrivateAgentLoopback.LEAF)) {
                        assertThat(request.toolNames()).containsExactly("market_snapshot");
                        assertThat(request.toolSchema("market_snapshot"))
                                .isEqualTo(json.valueToTree(MarketToolSchemas.SNAPSHOT));
                        assertThat(request.serialized())
                                .contains(CHILD_MESSAGE)
                                .doesNotContain(
                                        "BASELINE_MARKET_PARENT_PROFILE",
                                        "BASELINE_MARKET_PARENT_PROMPT");
                        if (request.round() == 1) {
                            return PrivateAgentLoopback.Reply.fragmentedTool(
                                    SNAPSHOT_CALL, "market_snapshot", Map.of("symbol", SYMBOL));
                        }
                        assertThat(request.round()).isEqualTo(2);
                        assertFrozenMarket(request.lastToolResult(SNAPSHOT_CALL), leaf.hash());
                        return PrivateAgentLoopback.Reply.text(CHILD_ANSWER);
                    }
                    if (request.round() == 1) {
                        return PrivateAgentLoopback.Reply.tool(
                                        CALL,
                                        PrivateAgentTarget.toolName(target.agentId()),
                                        Map.of("message", CHILD_MESSAGE))
                                .held(gateHolder.get());
                    }
                    assertThat(request.round()).isEqualTo(2);
                    assertThat(request.lastToolResult(CALL)).contains(CHILD_ANSWER);
                    return PrivateAgentLoopback.Reply.text("BASELINE_MARKET_PARENT_ANSWER");
                });
        var gate = model.gate();
        gateHolder.set(gate);
        var captured = new AtomicReference<Admitted>();
        measure(
                "market_http",
                "http-bearer-single-owner",
                window -> {
                    Admitted turn = submitHttp(conversation, "BASELINE_MARKET_PARENT_QUESTION");
                    captured.set(turn);
                    assertThat(gate.awaitEntered(Duration.ofSeconds(5))).isTrue();
                    try (var observer = observe(turn)) {
                        gate.release();
                        awaitCompleted(observer);
                    }
                });
        var turn = captured.get();
        assertCompleted(turn, parent, "BASELINE_MARKET_PARENT_ANSWER", 4, 2);
        assertThat(model.requests(PrivateAgentLoopback.PARENT)).hasSize(2);
        assertThat(model.requests(PrivateAgentLoopback.LEAF)).hasSize(2);
        var child =
                one(
                        "SELECT target_agent_id,target_release_hash,model_calls,tool_calls,status "
                                + "FROM business.child_invocations WHERE root_turn_id=?",
                        turn.id());
        assertThat(child.get("target_agent_id")).isEqualTo(leaf.id());
        assertThat(child.get("target_release_hash")).isEqualTo(leaf.hash());
        assertThat(child.get("model_calls")).isEqualTo(2);
        assertThat(child.get("tool_calls")).isEqualTo(1);
        assertThat(child.get("status")).isEqualTo("completed");
        assertThat(
                        integer(
                                "SELECT count(*) FROM business.capability_steps WHERE turn_id=? "
                                        + "AND namespace=?",
                                turn.id(),
                                "child:" + CALL))
                .isEqualTo(2);
        assertFrozenMarket(
                string(
                        "SELECT result FROM business.capability_steps WHERE turn_id=? "
                                + "AND namespace=? AND step=?",
                        turn.id(),
                        "child:" + CALL,
                        "tool:" + SNAPSHOT_CALL),
                leaf.hash());
        assertHealthyMeasurements();
    }

    @Test
    void twoOwnersFourConversationsOverlapAndRenewIndependently() throws Exception {
        // BearerIdentity 固定单个 owner；本场景明确使用可信应用服务入口，验多 owner 业务隔离而非 HTTP 认证。
        String aliceProfile = "BASELINE_OWNER_A_PROFILE";
        String bobProfile = "BASELINE_OWNER_B_PROFILE";
        updateProfile(ALICE, aliceProfile);
        updateProfile(BOB, bobProfile);
        publishBase(PrivateAgentLoopback.PARENT, 1, Map.of());
        Agent alice = publishAgent(ALICE, "BASELINE_OWNER_A_PROMPT", List.of());
        Agent bob = publishAgent(BOB, "BASELINE_OWNER_B_PROMPT", List.of());
        var expected = new ArrayList<Expected>();
        for (int i = 0; i < 4; i++) {
            Owner owner = i < 2 ? ALICE : BOB;
            Agent agent = i < 2 ? alice : bob;
            expected.add(
                    new Expected(
                            owner,
                            agent,
                            submission.createConversation(owner, agent.id()),
                            i < 2 ? "BASELINE_OWNER_A_PROMPT" : "BASELINE_OWNER_B_PROMPT",
                            i < 2 ? aliceProfile : bobProfile,
                            "BASELINE_ISOLATED_QUESTION_" + i,
                            "BASELINE_ISOLATED_ANSWER_" + i));
        }
        var gates = new ArrayList<PrivateAgentLoopback.Gate>();
        model.reset(
                request -> {
                    var matching =
                            expected.stream()
                                    .filter(e -> request.serialized().contains(e.question()))
                                    .toList();
                    assertThat(matching).hasSize(1);
                    var own = matching.getFirst();
                    assertThat(request.model()).isEqualTo(PrivateAgentLoopback.PARENT);
                    assertThat(request.toolNames()).isEmpty();
                    assertThat(request.serialized())
                            .contains(own.prompt(), own.profile(), own.question());
                    for (var other : expected) {
                        if (other != own)
                            assertThat(request.serialized())
                                    .doesNotContain(other.question(), other.answer());
                        if (!other.owner().equals(own.owner())) {
                            assertThat(request.serialized())
                                    .doesNotContain(other.profile(), other.prompt());
                        }
                    }
                    return PrivateAgentLoopback.Reply.text(own.answer())
                            .held(gates.get(expected.indexOf(own)));
                });
        for (int i = 0; i < 4; i++) gates.add(model.gate());
        var turns = new ArrayList<Admitted>();
        measure(
                "two_owner_four_conversations",
                "trusted-submit-service-two-owners",
                window -> {
                    // 不把 HTTP 包进事务。四个正常受理事务竞争真实 owner 锁，Worker 自行领取并创建 attempt。
                    var start = new CountDownLatch(1);
                    long overlapDeadline = System.nanoTime() + Duration.ofSeconds(5).toNanos();
                    try (var submitters = Executors.newFixedThreadPool(4)) {
                        var futures =
                                expected.stream()
                                        .map(
                                                e ->
                                                        submitters.submit(
                                                                () -> {
                                                                    assertThat(
                                                                                    start.await(
                                                                                            5,
                                                                                            TimeUnit
                                                                                                    .SECONDS))
                                                                            .isTrue();
                                                                    var accepted =
                                                                            submission.submit(
                                                                                    e.owner(),
                                                                                    e
                                                                                            .conversation(),
                                                                                    UUID.randomUUID()
                                                                                            .toString(),
                                                                                    e.question());
                                                                    var turn =
                                                                            new Admitted(
                                                                                    e.owner(),
                                                                                    e
                                                                                            .conversation(),
                                                                                    accepted
                                                                                            .turnId());
                                                                    admitted.add(turn);
                                                                    return turn;
                                                                }))
                                        .toList();
                        start.countDown();
                        for (var future : futures) {
                            turns.add(
                                    future.get(
                                            remaining(overlapDeadline).toNanos(),
                                            TimeUnit.NANOSECONDS));
                        }
                    }
                    // 一个共同截止时间，避免顺序 await 每个都重新获得完整超时；模型 gate 原有 8 秒上限不变。
                    for (var gate : gates)
                        assertThat(gate.awaitEntered(remaining(overlapDeadline))).isTrue();
                    assertThat(model.requests(PrivateAgentLoopback.PARENT)).hasSize(4);
                    assertThat(window.awaitSqlFamily("heartbeat_renew", 4, Duration.ofSeconds(2)))
                            .isTrue();
                    assertFourAttemptsRenewed(turns);
                    var observers = new ArrayList<Observed>();
                    try {
                        for (var turn : turns) observers.add(observe(turn));
                        gates.forEach(PrivateAgentLoopback.Gate::release);
                        for (var observer : observers) awaitCompleted(observer);
                    } finally {
                        observers.forEach(Observed::close);
                    }
                });
        for (int i = 0; i < expected.size(); i++) {
            var own = expected.get(i);
            var turn = turns.get(i);
            assertCompleted(turn, own.agent(), own.answer(), 1, 0);
            var snapshot =
                    one(
                            "SELECT tenant,subject,context FROM memory.turn_snapshots WHERE turn_id=?",
                            turn.id());
            assertThat(snapshot.get("tenant")).isEqualTo(own.owner().tenant());
            assertThat(snapshot.get("subject")).isEqualTo(own.owner().subject());
            assertThat(snapshot.get("context").toString()).contains(own.profile());
            var work =
                    one(
                            "SELECT tenant,subject,conversation_id,payload::text,transcript::text "
                                    + "FROM agent_runtime.work_contexts WHERE turn_id=?",
                            turn.id());
            assertThat(work.get("tenant")).isEqualTo(own.owner().tenant());
            assertThat(work.get("subject")).isEqualTo(own.owner().subject());
            assertThat(work.get("conversation_id")).isEqualTo(own.conversation());
            String workText = work.get("payload") + " " + work.get("transcript");
            assertThat(workText).contains(own.question(), own.answer());
            String sdk = sdkPayload(own.owner(), own.agent());
            for (var other : expected) {
                if (!other.owner().equals(own.owner())) {
                    assertThat(snapshot.get("context").toString()).doesNotContain(other.profile());
                    assertThat(sdk)
                            .doesNotContain(
                                    other.profile(),
                                    other.prompt(),
                                    other.question(),
                                    other.answer());
                }
                if (other != own)
                    assertThat(workText).doesNotContain(other.question(), other.answer());
            }
        }
        assertThat(model.requests()).hasSize(4);
        assertThat(integer("SELECT count(*) FROM business.child_invocations")).isZero();
        assertThat(integer("SELECT count(*) FROM agent_runtime.unsafe_execution_data()")).isZero();
        assertHealthyMeasurements();
    }

    @FunctionalInterface
    interface CapturedWork {
        void run(ApplicationJdbcRecorder.Window window) throws Exception;
    }

    /** 每个场景无论断言是否失败都先保留原始测量；异常类别可导出，正文/参数绝不进入报告。 */
    private void measure(String phase, String entrypoint, CapturedWork work) throws Exception {
        long before = excludedQueries.get();
        var window = recorder.begin(phase);
        Throwable failure = null;
        ApplicationJdbcRecorder.Measurement measurement = null;
        try {
            work.run(window);
        } catch (Exception | Error problem) {
            failure = problem;
            model.releaseAll();
        } finally {
            try {
                measurement = window.finish(Duration.ofSeconds(10));
            } catch (Exception | Error finishFailure) {
                if (failure == null) failure = finishFailure;
                else failure.addSuppressed(finishFailure);
            }
            var row = new LinkedHashMap<String, Object>();
            row.put("phase", phase);
            row.put("entrypoint", entrypoint);
            row.put("cohort_size", phase.equals("two_owner_four_conversations") ? 4 : 1);
            row.put("observer_queries_excluded_in_window", excludedQueries.get() - before);
            row.put("completed", failure == null);
            row.put("connection_drain_completed", measurement != null);
            if (measurement != null) row.put("measurement", measurement);
            if (failure != null) row.put("failure_type", failure.getClass().getSimpleName());
            measurements.add(row);
        }
        if (failure instanceof Error error) throw error;
        if (failure instanceof Exception exception) throw exception;
    }

    private void assertHealthyMeasurements() {
        var row = measurements.getLast();
        var measurement = (ApplicationJdbcRecorder.Measurement) row.get("measurement");
        assertThat(row.get("completed")).isEqualTo(true);
        assertThat(row.get("connection_drain_completed")).isEqualTo(true);
        assertThat(measurement.statements()).isPositive();
        assertThat(measurement.executions())
                .allMatch(ApplicationJdbcRecorder.SqlExecution::success);
        assertThat(measurement.commitFailures()).isZero();
        assertThat(measurement.rollbackFailures()).isZero();
        assertThat(measurement.roles()).containsKey("task_worker");
    }

    /** 订阅前读取真实、已受理 Turn；gate 尚未放行，所以终态不能在订阅建立前丢失。 */
    private Observed observe(Admitted turn) {
        var subscription = events.subscribe(readTurn(turn), 256);
        var terminal =
                subscription
                        .events()
                        .filter(
                                e ->
                                        List.of("completed", "failed", "cancelled", "blocked")
                                                .contains(e.status()))
                        .next()
                        .toFuture();
        return new Observed(subscription, terminal);
    }

    private void awaitCompleted(Observed observer) throws Exception {
        var event = observer.terminal().get(25, TimeUnit.SECONDS);
        assertThat(event).isNotNull();
        assertThat(event.status()).as("real afterCommit terminal event").isEqualTo("completed");
    }

    /** 聚合 heartbeat 次数不够证明归属：读取四个具体 attempt 的提交后 lease 值。 */
    private void assertFourAttemptsRenewed(List<Admitted> turns) throws Exception {
        long deadline = System.nanoTime() + Duration.ofMillis(750).toNanos();
        List<Boolean> renewed;
        do {
            renewed = new ArrayList<>();
            for (var turn : turns) {
                excludedQueries.incrementAndGet();
                renewed.add(
                        raw.queryForObject(
                                "SELECT c.status='running' AND c.attempt_id=a.id "
                                        + "AND c.lease_until>a.started_at+interval '60 seconds' "
                                        + "FROM business.commands c JOIN business.attempts a ON a.id=c.attempt_id "
                                        + "WHERE c.turn_id=?",
                                Boolean.class,
                                turn.id()));
            }
            if (renewed.stream().allMatch(Boolean.TRUE::equals)) break;
            Thread.sleep(20);
        } while (System.nanoTime() < deadline);
        assertThat(renewed)
                .as("all four distinct attempts have a successfully committed renewal")
                .containsExactly(true, true, true, true);
    }

    private void assertCompleted(
            Admitted admittedTurn, Agent agent, String answer, int models, int tools) {
        var turn = readTurn(admittedTurn);
        assertThat(turn.status()).isEqualTo("completed");
        assertThat(turn.result()).isEqualTo(answer);
        assertThat(turn.agentId()).isEqualTo(agent.id());
        assertThat(turn.releaseHash()).isEqualTo(agent.hash());
        var policy =
                one(
                        "SELECT model_calls,tool_calls FROM business.turn_policies WHERE turn_id=?",
                        turn.id());
        assertThat(policy.get("model_calls")).isEqualTo(models);
        assertThat(policy.get("tool_calls")).isEqualTo(tools);
        assertThat(
                        integer(
                                "SELECT count(*) FROM business.commands WHERE turn_id=? AND status='done'",
                                turn.id()))
                .isOne();
        assertThat(
                        integer(
                                "SELECT count(*) FROM business.attempts a JOIN business.commands c "
                                        + "ON c.id=a.command_id WHERE c.turn_id=?",
                                turn.id()))
                .isOne();
        assertThat(
                        integer(
                                "SELECT count(*) FROM business.journal WHERE turn_id=? AND role='assistant'",
                                turn.id()))
                .isOne();
        assertThat(
                        string(
                                "SELECT content FROM business.journal WHERE turn_id=? AND role='assistant'",
                                turn.id()))
                .isEqualTo(answer);
        assertThat(
                        integer(
                                "SELECT count(*) FROM memory.sources WHERE id=? AND tenant=? AND subject=?",
                                turn.id(),
                                admittedTurn.owner().tenant(),
                                admittedTurn.owner().subject()))
                .isOne();
    }

    private String sdkPayload(Owner owner, Agent agent) {
        var scope =
                new ExecutionScope(
                        owner.tenant(),
                        owner.subject(),
                        "namespace-only",
                        agent.id(),
                        agent.hash());
        excludedQueries.incrementAndGet();
        var rows =
                raw.queryForList(
                        "SELECT payload::text FROM agent_runtime.states "
                                + "WHERE namespace=? AND user_id=? AND NOT deleted",
                        String.class,
                        scope.namespace(),
                        scope.userId());
        assertThat(rows).isNotEmpty();
        return String.join(" ", rows);
    }

    private Turn readTurn(Admitted turn) {
        excludedQueries.incrementAndGet();
        return raw.queryForObject(
                "SELECT id,conversation_id,tenant,subject,idempotency_key,request_hash,"
                        + "message,release_hash,status,revision,result,reason,agent_id FROM business.turns "
                        + "WHERE id=? AND conversation_id=? AND tenant=? AND subject=?",
                (r, ignored) ->
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
                turn.id(),
                turn.conversation(),
                turn.owner().tenant(),
                turn.owner().subject());
    }

    private Agent publishAgent(Owner owner, String prompt, List<String> capabilities) {
        String id = UUID.randomUUID().toString();
        agents.create(
                owner,
                id,
                "Turn SQL baseline agent",
                prompt,
                new AgentComposition("primary", capabilities));
        return new Agent(id, agents.publish(owner, id, 1, 0).releaseHash());
    }

    private void publishBase(String modelName, int maxModels, Map<String, Object> market) {
        releases.publish(
                new ReleaseDefinition(
                        "turn-sql-baseline",
                        "finance_agent",
                        ReleaseDefinition.SCOPES,
                        new ModelProfile(
                                modelName, model.baseUrl(), 65536, 131072, 8192, 512, false),
                        null,
                        maxModels,
                        64,
                        new BusinessFeatures(
                                List.of(),
                                PackagedSkills.hash(),
                                market.isEmpty()
                                        ? "{}"
                                        : PayloadJson.canonical(Map.of(SYMBOL, market)),
                                null)));
    }

    private void updateProfile(Owner owner, String text) {
        var service = application.getBean(UserProfileService.class);
        var current = service.read(owner);
        service.update(owner, current.epoch(), current.revision(), text);
    }

    private String createHttpConversation(Agent agent) throws Exception {
        return post("/v1/conversations", Map.of("agent_id", agent.id()), 201)
                .path("conversation_id")
                .asText();
    }

    private Admitted submitHttp(String conversation, String question) throws Exception {
        var response =
                post(
                        "/v1/conversations/" + conversation + "/turns",
                        Map.of("message", question),
                        202);
        var turn = new Admitted(ALICE, conversation, response.path("turn_id").asText());
        assertThat(turn.id()).isNotBlank();
        admitted.add(turn);
        return turn;
    }

    private JsonNode post(String path, Object body, int status) throws Exception {
        var request =
                HttpRequest.newBuilder(URI.create(api + path))
                        .timeout(Duration.ofSeconds(15))
                        .header("Authorization", "Bearer " + TOKEN)
                        .header("Content-Type", "application/json")
                        .header("Idempotency-Key", UUID.randomUUID().toString())
                        .POST(HttpRequest.BodyPublishers.ofByteArray(json.writeValueAsBytes(body)))
                        .build();
        var response = http.send(request, HttpResponse.BodyHandlers.ofByteArray());
        assertThat(response.statusCode()).as("normal HTTP admission").isEqualTo(status);
        return json.readTree(response.body());
    }

    private void assertFrozenMarket(String value, String hash) {
        try {
            var result = json.readTree(value);
            assertThat(result.path("symbol").asText()).isEqualTo(SYMBOL);
            for (var key : List.of("price", "currency", "provider", "as_of")) {
                assertThat(result.path(key).asText()).isEqualTo(MARKET_A.get(key));
            }
            assertThat(result.path("source_release_hash").asText()).isEqualTo(hash);
            assertThat(result.path("input_hash").asText())
                    .isEqualTo(
                            PayloadDigest.sha256(
                                    PayloadJson.canonical(
                                            Map.of(
                                                    "symbol",
                                                    SYMBOL,
                                                    "source_release_hash",
                                                    hash))));
            assertThat(result.path("tool_version").asText()).isEqualTo("1.0.0");
            assertThat(value).doesNotContain((String) MARKET_P.get("provider"));
        } catch (java.io.IOException failure) {
            throw new AssertionError("Invalid market result JSON", failure);
        }
    }

    private Map<String, Object> one(String query, Object... arguments) {
        excludedQueries.incrementAndGet();
        return raw.queryForMap(query, arguments);
    }

    private int integer(String query, Object... arguments) {
        excludedQueries.incrementAndGet();
        return raw.queryForObject(query, Integer.class, arguments);
    }

    private String string(String query, Object... arguments) {
        excludedQueries.incrementAndGet();
        return raw.queryForObject(query, String.class, arguments);
    }

    private static Duration remaining(long deadline) {
        return Duration.ofNanos(Math.max(1, deadline - System.nanoTime()));
    }

    private void waitCommandsFinished() throws InterruptedException {
        if (raw == null) return;
        long deadline = System.nanoTime() + Duration.ofSeconds(15).toNanos();
        while (integer("SELECT count(*) FROM business.commands WHERE status<>'done'") != 0) {
            if (System.nanoTime() >= deadline)
                throw new AssertionError("Worker command did not finish");
            Thread.sleep(50);
        }
    }

    private void closeResources() {
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
