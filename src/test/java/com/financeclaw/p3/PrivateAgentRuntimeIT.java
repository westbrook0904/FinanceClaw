package com.financeclaw.p3;

import static org.assertj.core.api.Assertions.*;

import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import com.financeclaw.app.agent.AgentService;
import com.financeclaw.app.release.ReleaseService;
import com.financeclaw.bootstrap.FinanceClawApplication;
import com.financeclaw.domain.ExecutionScope;
import com.financeclaw.domain.Task.Owner;
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
import java.nio.charset.StandardCharsets;
import java.time.Duration;
import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.UUID;
import java.util.concurrent.atomic.AtomicReference;
import java.util.function.Function;
import javax.sql.DataSource;
import org.junit.jupiter.api.*;
import org.junit.jupiter.api.parallel.Execution;
import org.junit.jupiter.api.parallel.ExecutionMode;
import org.junit.jupiter.params.ParameterizedTest;
import org.junit.jupiter.params.provider.ValueSource;
import org.springframework.boot.WebApplicationType;
import org.springframework.boot.builder.SpringApplicationBuilder;
import org.springframework.boot.web.servlet.context.ServletWebServerApplicationContext;
import org.springframework.context.ConfigurableApplicationContext;
import org.springframework.jdbc.core.JdbcTemplate;
import org.testcontainers.containers.PostgreSQLContainer;

/**
 * 真实 Spring 应用内的父子 ReAct 整链路：正常发布/HTTP 受理/自动 Worker/原生 SDK/真实 SSE transport。
 * 模型服务只有协议替身，不替换模型工厂、治理端口、租约或执行尝试；本类不是独立打包 JAR 的启动验收。
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
@Execution(ExecutionMode.SAME_THREAD)
class PrivateAgentRuntimeIT {
    private static final Owner OWNER = new Owner("private-runtime-test", "alice");
    private static final String TOKEN = "synthetic-private-runtime-token";
    private static final String PARENT_PROMPT = "PARENT_SYSTEM_PRIVATE_SENTINEL";
    private static final String LEAF_PROMPT = "LEAF_FROZEN_SYSTEM_SENTINEL";
    private static final String MESSAGE = "EXPLICIT_CHILD_MESSAGE_SENTINEL";
    private static final String ANSWER = "LEAF_ANSWER_SENTINEL";
    private static final String FINAL = "PARENT_FINAL_FROM_LEAF";
    private static final String CALL = "delegate-call";
    private static final String CITY_CALL = "city-resume-call";
    private static final String CITY = "CITY_RESUME_SENTINEL";
    private static final String SNAPSHOT_CALL = "leaf-market-snapshot";
    private static final String SYMBOL = "AAPL";
    private static final Map<String, Object> MARKET_A =
            market("101.11", "TARGET_FROZEN_PROVIDER_A", "2026-10-20T01:02:03Z");
    private static final Map<String, Object> MARKET_P =
            market("202.22", "PARENT_RELEASE_PROVIDER_P", "2026-10-21T02:03:04Z");
    private static final Map<String, Object> MARKET_B =
            market("303.33", "GLOBAL_CURRENT_PROVIDER_B", "2026-10-22T03:04:05Z");
    private static final String MARKET_ANSWER =
            ANSWER + "\nFrozen market evidence: " + PayloadJson.canonical(MARKET_A);
    private final ObjectMapper json = new ObjectMapper();
    private final PostgreSQLContainer<?> postgres =
            new PostgreSQLContainer<>("postgres:16.10-alpine");
    private final List<Turn> admitted = new ArrayList<>();
    private ConfigurableApplicationContext application;
    private PrivateAgentLoopback model;
    private HttpClient http;
    private JdbcTemplate sql;
    private AgentService agents;
    private ReleaseService releases;
    private String api;

    record Published(String parentId, PrivateAgentTarget leaf) {
        String tool() {
            return PrivateAgentTarget.toolName(leaf.agentId());
        }
    }

    record Turn(String conversation, String id) {
        String path() {
            return "/v1/conversations/" + conversation + "/turns/" + id;
        }
    }

    /** 保存暂停时的持久事实；后续只从 HTTP 回答恢复，不把这些值交给生产代码伪造 attempt。 */
    record Waiting(
            Published published,
            Turn turn,
            JsonNode question,
            String commandId,
            String attemptId,
            String releaseHash,
            String childReceipt,
            List<Map<String, Object>> sdkRows,
            Map<String, Object> memorySnapshot,
            int childModels,
            int childTools) {
        String answerPath() {
            return turn.path() + "/interactions/" + question.path("id").asText() + "/answer";
        }
    }

    @BeforeAll
    void startApplication() throws Exception {
        try {
            postgres.start();
            model = new PrivateAgentLoopback();
            http =
                    HttpClient.newBuilder()
                            .version(HttpClient.Version.HTTP_1_1)
                            .connectTimeout(Duration.ofSeconds(5))
                            .build();
            startSpring();

        } catch (Exception | Error failure) {
            stopApplication();
            throw failure;
        }
    }

    /** Spring 可以关闭再启动，PG、模型 HTTP 服务、请求记录及测试 HTTP client 都由外层生命周期持有。 */
    private void startSpring() {
        var properties = new LinkedHashMap<String, String>();
        properties.put("spring.datasource.url", postgres.getJdbcUrl());
        properties.put("spring.datasource.username", postgres.getUsername());
        properties.put("spring.datasource.password", postgres.getPassword());
        properties.put("spring.datasource.hikari.maximum-pool-size", "6");
        properties.put("spring.config.import", "");
        properties.put("server.port", "0");
        properties.put("spring.main.banner-mode", "off");
        properties.put("financeclaw.worker.enabled", "true");
        properties.put("financeclaw.worker.parallelism", "1");
        properties.put("financeclaw.worker.poll-ms", "100");
        properties.put("financeclaw.worker.idle-max-ms", "100");
        properties.put("financeclaw.worker.lease-ms", "60000");
        properties.put("financeclaw.worker.heartbeat-ms", "1000");
        properties.put("financeclaw.integrations.enabled", "false");
        properties.put("financeclaw.memory.enabled", "false");
        properties.put("financeclaw.memory.profile-enabled", "true");
        properties.put("financeclaw.memory.profile-directory", "");
        properties.put("financeclaw.memory.forget-ledger.enabled", "false");
        properties.put("financeclaw.auth.token", TOKEN);
        properties.put("financeclaw.auth.tenant", OWNER.tenant());
        properties.put("financeclaw.auth.subject", OWNER.subject());
        properties.put(
                "financeclaw.auth.credential-resources-json",
                PayloadJson.canonical(
                        List.of(
                                Map.of(
                                        "resourceId",
                                        "fixture-model",
                                        "reference",
                                        "@model",
                                        "tenant",
                                        OWNER.tenant(),
                                        "subject",
                                        OWNER.subject()))));
        properties.put("financeclaw.model.name", "offline");
        properties.put("financeclaw.model.api-key", "synthetic-loopback-key");
        properties.put("financeclaw.fallback.name", "");
        properties.put("financeclaw.feishu.enabled", "false");
        properties.put("financeclaw.business.hotel.enabled", "false");
        properties.put("financeclaw.business.taibu.enabled", "false");
        properties.put("financeclaw.business.ziwei.enabled", "false");
        application =
                new SpringApplicationBuilder(FinanceClawApplication.class)
                        .web(WebApplicationType.SERVLET)
                        .run(
                                properties.entrySet().stream()
                                        .map(e -> "--" + e.getKey() + "=" + e.getValue())
                                        .toArray(String[]::new));
        api =
                "http://127.0.0.1:"
                        + ((ServletWebServerApplicationContext) application)
                                .getWebServer()
                                .getPort();
        sql = new JdbcTemplate(application.getBean(DataSource.class));
        agents = application.getBean(AgentService.class);
        releases = application.getBean(ReleaseService.class);
        assertThat(application.getBean(TaskWorker.class)).isNotNull();
    }

    /** 只重启应用；调用方必须先进入持久 waiting/终态，不能靠关闭进程手工制造新 attempt。 */
    private void restartSpring() throws InterruptedException {
        waitCommandsFinished();
        model.awaitIdle(Duration.ofSeconds(5));
        var previous = application;
        previous.close();
        assertThat(previous.isActive()).isFalse();
        application = null;
        // 旧 Hikari 已随 context 关闭；后续所有 SQL 和管理用例必须重新取得新 context 的 bean。
        sql = null;
        agents = null;
        releases = null;
        startSpring();
        assertThat(application).isNotSameAs(previous);
    }

    @BeforeEach
    void reset() throws Exception {
        waitCommandsFinished();
        admitted.clear();
        sql.execute(
                "TRUNCATE agent_runtime.states,business.conversations,memory.owners,memory.jobs,memory.operations,memory.audit CASCADE");
        sql.update(
                "UPDATE memory.privacy_ledger_binding SET lineage=?,active=false",
                UUID.randomUUID().toString());
        model.reset(
                request -> {
                    throw new AssertionError("Unconfigured request: " + request.model());
                });
    }

    @AfterEach
    void finishRequests() throws Exception {
        model.releaseAll();
        for (var turn : admitted) {
            String status =
                    sql.queryForObject(
                            "SELECT status FROM business.turns WHERE id=?",
                            String.class,
                            turn.id());
            if (List.of("queued", "resuming", "running", "waiting", "cancelling")
                    .contains(status)) {
                send("POST", turn.path() + "/cancel", Map.of(), 202);
            }
        }
        waitCommandsFinished();
        model.awaitIdle(Duration.ofSeconds(5));
        assertThat(model.failures()).as("SSE protocol/script errors").isEmpty();
    }

    @AfterAll
    void stopApplication() {
        // 停止顺序归本 fixture 所有：先解除 HTTP gate，再关闭 Spring/Worker，最后停止模型 HTTP 服务和 PG。
        // 不使用 Spring 测试缓存，避免容器已停而旧 Hikari/定时 Worker 仍在后续 IT 中重连。
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

    @Test
    void nativeParentSelectsOnlyPublishedDelegateAndConsumesChildAnswer() throws Exception {
        var published = publish(32, false);
        happyScript(published);
        var turn = submit(published.parentId(), "Original parent request.");
        assertCompleted(turn, FINAL);
        assertThat(model.requests(PrivateAgentLoopback.PARENT)).hasSize(2);
        assertThat(model.requests(PrivateAgentLoopback.LEAF)).hasSize(1);
        assertThat(model.requests(PrivateAgentLoopback.PARENT).getFirst().toolNames())
                .containsExactlyInAnyOrder(published.tool(), "read_artifact_range");
        assertThat(model.requests(PrivateAgentLoopback.PARENT).getLast().toolResult(CALL))
                .contains(ANSWER);
        assertThat(model.requests(PrivateAgentLoopback.LEAF).getFirst().toolNames()).isEmpty();
        assertCounters(turn, 3, 1, 1);
        assertThat(integer("SELECT count(*) FROM business.turns")).isOne();
        assertThat(integer("SELECT count(*) FROM business.commands")).isOne();
        assertThat(integer("SELECT count(*) FROM business.attempts")).isOne();
        assertThat(integer("SELECT count(*) FROM memory.sources")).isOne();
        assertThat(integer("SELECT count(*) FROM agent_runtime.states WHERE NOT deleted"))
                .isEqualTo(2);
        assertThat(
                        integer(
                                "SELECT count(*) FROM business.journal WHERE turn_id=? AND role='assistant'",
                                turn.id()))
                .isOne();
    }

    @Test
    void leafReceivesExplicitMessageWithoutParentProfileHistoryOrToolTrace() throws Exception {
        var published = publish(32, true);
        String profile = "PARENT_PROFILE_PRIVATE_SENTINEL";
        String history = "PARENT_HISTORY_PRIVATE_SENTINEL";
        String trace = "PARENT_TOOL_ARGUMENT_PRIVATE_SENTINEL";
        String question = "PARENT_ORIGINAL_QUESTION_PRIVATE_SENTINEL";
        updateProfile(profile);
        model.reset(
                request -> {
                    if (request.model().equals(PrivateAgentLoopback.LEAF))
                        return PrivateAgentLoopback.Reply.text(ANSWER);
                    return switch (request.round()) {
                        case 1 -> PrivateAgentLoopback.Reply.text(history);
                        case 2 ->
                                PrivateAgentLoopback.Reply.tool(
                                        "catalog-call", "search_catalog", Map.of("city", trace));
                        case 3 ->
                                PrivateAgentLoopback.Reply.tool(
                                        CALL, published.tool(), Map.of("message", MESSAGE));
                        case 4 -> PrivateAgentLoopback.Reply.text(FINAL);
                        default -> throw new AssertionError("Unexpected parent request");
                    };
                });
        String conversation = conversation(published.parentId());
        assertCompleted(submitConversation(conversation, "PRIME_PARENT_HISTORY"), history);
        var turn = submitConversation(conversation, question);
        assertCompleted(turn, FINAL);
        var parents = model.requests(PrivateAgentLoopback.PARENT);
        assertThat(parents).hasSize(4);
        assertThat(parents.get(2).serialized())
                .contains(PARENT_PROMPT, profile, history, trace, question);
        assertThat(parents.get(2).toolResult("catalog-call")).isNotBlank();
        var leaf = model.requests(PrivateAgentLoopback.LEAF);
        assertThat(leaf).hasSize(1);
        assertThat(leaf.getFirst().serialized())
                .contains(LEAF_PROMPT, MESSAGE)
                .doesNotContain(
                        PARENT_PROMPT, profile, history, trace, question, "PRIME_PARENT_HISTORY");
        assertThat(leaf.getFirst().toolNames()).isEmpty();
        assertCounters(turn, 4, 2, 1);
    }

    @Test
    void parentUsesFrozenLeafPromptAfterRepublish() throws Exception {
        var published = publish(32, false);
        String changedPrompt = "NEW_LEAF_PUBLISHED_PROMPT_SENTINEL";
        agents.update(
                OWNER,
                published.leaf().agentId(),
                1,
                "Changed leaf",
                changedPrompt,
                new AgentComposition("primary", List.of()));
        var changed = agents.publish(OWNER, published.leaf().agentId(), 2, 1);
        assertThat(changed.releaseHash()).isNotEqualTo(published.leaf().releaseHash());
        happyScript(published);
        var turn = submit(published.parentId(), "Use the already published parent.");
        assertCompleted(turn, FINAL);
        assertThat(model.requests(PrivateAgentLoopback.LEAF)).hasSize(1);
        assertThat(model.requests(PrivateAgentLoopback.LEAF).getFirst().serialized())
                .contains(LEAF_PROMPT, MESSAGE)
                .doesNotContain(changedPrompt);
        assertThat(
                        sql.queryForObject(
                                "SELECT target_release_hash FROM business.child_invocations WHERE root_turn_id=?",
                                String.class,
                                turn.id()))
                .isEqualTo(published.leaf().releaseHash());
        assertCounters(turn, 3, 1, 1);
    }

    @ParameterizedTest
    @ValueSource(ints = {1, 2})
    void sharedRootModelBudgetBlocksAdditionalHttpRequests(int limit) throws Exception {
        var published = publish(limit, false);
        happyScript(published);
        var turn = submit(published.parentId(), "Delegate within a shared root budget.");
        var snapshot = awaitTerminal(turn);
        assertThat(snapshot.path("status").asText()).isEqualTo("blocked");
        assertThat(snapshot.path("reason").asText()).isEqualTo("execution_budget_exhausted");
        assertThat(model.requests(PrivateAgentLoopback.PARENT)).hasSize(1);
        assertThat(model.requests(PrivateAgentLoopback.LEAF)).hasSize(limit - 1);
        assertCounters(turn, limit, 1, limit - 1);
        assertThat(
                        sql.queryForObject(
                                "SELECT status FROM business.child_invocations WHERE root_turn_id=?",
                                String.class,
                                turn.id()))
                .isEqualTo(limit == 1 ? "pending" : "completed");
        assertNoAssistant(turn);
    }

    @ParameterizedTest
    @ValueSource(booleans = {false, true})
    void childBudgetDenialCannotCommitACompletedCheckpointButExactLimitCanFinish(
            boolean answerAtLimit) throws Exception {
        var published = publishControlled(List.of("function:search_catalog"), List.of());
        model.reset(
                request -> {
                    if (request.model().equals(PrivateAgentLoopback.PARENT)) {
                        if (request.round() == 1)
                            return PrivateAgentLoopback.Reply.tool(
                                    CALL, published.tool(), Map.of("message", MESSAGE));
                        return PrivateAgentLoopback.Reply.text(FINAL);
                    }
                    if (answerAtLimit && request.round() == 4)
                        return PrivateAgentLoopback.Reply.text(ANSWER);
                    return PrivateAgentLoopback.Reply.tool(
                            "catalog-" + request.round(), "search_catalog", Map.of("city", CITY));
                });
        var turn = submit(published.parentId(), "use the child model budget exactly");
        if (answerAtLimit) assertCompleted(turn, FINAL);
        else {
            var snapshot = awaitTerminal(turn);
            assertThat(snapshot.path("status").asText()).isEqualTo("blocked");
            assertThat(snapshot.path("reason").asText()).isEqualTo("child_model_budget_exceeded");
            assertNoAssistant(turn);
        }
        assertThat(model.requests(PrivateAgentLoopback.LEAF)).hasSize(4);
        assertThat(
                        integer(
                                "SELECT model_calls FROM business.child_invocations WHERE root_turn_id=?",
                                turn.id()))
                .isEqualTo(4);
        assertThat(
                        sql.queryForObject(
                                "SELECT status FROM business.child_invocations WHERE root_turn_id=?",
                                String.class,
                                turn.id()))
                .isEqualTo(answerAtLimit ? "completed" : "pending");
        var checkpoints =
                sql.queryForList(
                        """
                SELECT payload::text AS payload,version FROM agent_runtime.states
                WHERE NOT deleted AND namespace=agent_runtime.scope_hash(
                    ARRAY['financeclaw-state-v1',?,?,?,?])
                """,
                        OWNER.tenant(),
                        OWNER.subject(),
                        published.leaf().agentId(),
                        published.leaf().releaseHash());
        if (answerAtLimit) assertThat(checkpoints).hasSize(1);
        for (var checkpoint : checkpoints) {
            var state =
                    io.agentscope.core.util.JsonUtils.getJsonCodec()
                            .fromJson(
                                    (String) checkpoint.get("payload"),
                                    io.agentscope.core.state.AgentState.class);
            var outcome =
                    com.financeclaw.infrastructure.agentscope.AgentStateInspector.classify(
                            state, ((Number) checkpoint.get("version")).longValue());
            assertThat(outcome.status().equals("completed")).isEqualTo(answerAtLimit);
        }
    }

    @Test
    void forgedChildToolCannotBecomeSuccessfulParentAnswer() throws Exception {
        var published = publish(32, false);
        model.reset(
                request ->
                        request.model().equals(PrivateAgentLoopback.LEAF)
                                ? PrivateAgentLoopback.Reply.tool(
                                        "forged-leaf-tool", "market_snapshot", Map.of())
                                : request.round() == 1
                                        ? PrivateAgentLoopback.Reply.tool(
                                                CALL, published.tool(), Map.of("message", MESSAGE))
                                        : PrivateAgentLoopback.Reply.text(
                                                "FORGED_TOOL_FAILURE_WAS_SWALLOWED"));
        var turn = submit(published.parentId(), "A child may not invent tools.");
        var snapshot = awaitTerminal(turn);
        assertThat(snapshot.path("status").asText()).isEqualTo("blocked");
        assertThat(snapshot.path("reason").asText()).isEqualTo("child_tools_denied");
        assertThat(model.requests(PrivateAgentLoopback.PARENT)).hasSize(1);
        assertThat(model.requests(PrivateAgentLoopback.LEAF)).hasSize(1);
        assertCounters(turn, 2, 1, 1);
        assertUnfinishedChild(turn);
        assertThat(integer("SELECT count(*) FROM business.child_invocations")).isOne();
        assertThat(integer("SELECT count(*) FROM business.artifacts")).isZero();
        assertThat(integer("SELECT count(*) FROM business.reports")).isZero();
        assertNoAssistant(turn);
    }

    @Test
    void largeChildResultIsOffloadedBeforeParentRangeRead() throws Exception {
        var published = publish(32, false);
        String head = "LARGE_CHILD_VISIBLE_HEAD_SENTINEL";
        String tail = "LARGE_CHILD_UNREAD_TAIL_SENTINEL";
        String answer = head + "x".repeat(14000) + tail;
        var reference = new AtomicReference<JsonNode>();
        var chunk = new AtomicReference<JsonNode>();
        model.reset(
                request -> {
                    if (request.model().equals(PrivateAgentLoopback.LEAF))
                        return PrivateAgentLoopback.Reply.text(answer);
                    if (request.round() == 1)
                        return PrivateAgentLoopback.Reply.tool(
                                CALL, published.tool(), Map.of("message", MESSAGE));
                    if (request.round() == 2) {
                        // 此次推理发生在范围读取之前；任何原始头尾片段进入请求都会使替身失败。
                        assertThat(request.serialized()).doesNotContain(head, tail);
                        var ref = object(request.toolResult(CALL));
                        assertThat(ref.path("archived").asBoolean()).isTrue();
                        reference.set(ref);
                        return PrivateAgentLoopback.Reply.tool(
                                "range-call",
                                "read_artifact_range",
                                Map.of(
                                        "artifact_id",
                                        ref.path("artifact_id").asText(),
                                        "start",
                                        0,
                                        "max_bytes",
                                        512));
                    }
                    if (request.round() == 3) {
                        var range = object(request.toolResult("range-call"));
                        assertThat(range.path("start").asInt()).isZero();
                        assertThat(range.path("count").asInt()).isBetween(1, 512);
                        assertThat(range.path("text").asText()).contains(head).doesNotContain(tail);
                        assertThat(request.serialized()).contains(head).doesNotContain(tail);
                        chunk.set(range);
                        return PrivateAgentLoopback.Reply.text(FINAL);
                    }
                    throw new AssertionError("Unexpected parent request");
                });
        var turn =
                submit(
                        published.parentId(),
                        "Read only the first bounded part of the child result.");
        assertCompleted(turn, FINAL);
        assertThat(model.requests(PrivateAgentLoopback.PARENT)).hasSize(3);
        assertThat(model.requests(PrivateAgentLoopback.LEAF)).hasSize(1);
        var ref = reference.get();
        assertThat(ref).isNotNull();
        assertThat(chunk.get()).isNotNull();
        String id = ref.path("artifact_id").asText();
        var download = raw("GET", ref.path("download_url").asText(), null, Map.of(), 200);
        String archived = new String(download.body(), StandardCharsets.UTF_8);
        // 归档契约覆盖规范化完整 ToolResultBlock，而非裸 answer；摘要与字节数按实际下载文件核对。
        assertThat(archived).contains(head, tail, answer);
        assertThat(download.body()).hasSize(ref.path("bytes").asInt());
        assertThat(PayloadDigest.sha256(archived)).isEqualTo(ref.path("hash").asText());
        assertThat(download.headers().firstValue("X-Artifact-SHA256"))
                .contains(ref.path("hash").asText());
        assertThat(chunk.get().path("text").asText())
                .isEqualTo(new String(download.body(), 0, 512, StandardCharsets.UTF_8));
        assertThat(chunk.get().path("nextStart").asInt()).isEqualTo(512);
        assertThat(
                        sql.queryForObject(
                                "SELECT privacy_bound FROM business.artifacts WHERE id=?",
                                Boolean.class,
                                id))
                .isTrue();
        assertThat(
                        sql.queryForObject(
                                "SELECT required_scope FROM business.artifacts WHERE id=?",
                                String.class,
                                id))
                .isEqualTo("artifact:read");
        assertCounters(turn, 4, 2, 1);
        // 只检查父作用域与父会话投影；叶子自己的 SDK 状态/步骤回执合法保留完整答案。
        var parentStates =
                sql.queryForList(
                        """
                SELECT s.payload::text FROM agent_runtime.states s JOIN business.turns t ON t.id=?
                WHERE NOT s.deleted
                  AND s.namespace=agent_runtime.scope_hash(ARRAY['financeclaw-state-v1',t.tenant,t.subject,t.agent_id,t.release_hash])
                  AND s.user_id=agent_runtime.scope_hash(ARRAY[t.tenant,t.subject])
                """,
                        String.class,
                        turn.id());
        assertThat(parentStates).hasSize(1);
        parentStates.forEach(
                payload -> assertThat(payload).contains(head).doesNotContain(tail, answer));
        var work =
                sql.queryForList(
                        "SELECT payload::text,transcript::text FROM agent_runtime.work_contexts WHERE turn_id=?",
                        turn.id());
        assertThat(work).hasSize(1);
        for (String column : List.of("payload", "transcript")) {
            assertThat((String) work.getFirst().get(column))
                    .contains(head)
                    .doesNotContain(tail, answer);
        }
    }

    @Test
    void cancellationDuringChildHttpRejectsLateCompletion() throws Exception {
        var published = publish(32, false);
        var gate = heldChildScript(published);
        var turn =
                submit(
                        published.parentId(),
                        "Cancel while the independent child model is running.");
        assertThat(gate.awaitEntered(Duration.ofSeconds(10)))
                .as("child HTTP request reached SSE gate")
                .isTrue();
        var beforeCancellation = stateVersions();
        var childVersions = childStateVersions(published.leaf());
        send("POST", turn.path() + "/cancel", Map.of(), 202);
        // HTTP cancel 先进入 cancelling；SDK 可保存父中断检查点。先由正常 Worker 确认
        // cancelled，证明取消不必等待子网络答复，再以该终态为迟到写入的比较基线。
        var snapshot = awaitTerminal(turn);
        assertThat(snapshot.path("status").asText()).isEqualTo("cancelled");
        assertThat(snapshot.path("result").isNull()).isTrue();
        assertThat(childStateVersions(published.leaf()))
                .as("child checkpoint is fenced throughout cancelling")
                .isEqualTo(childVersions);
        assertUnfinishedChild(turn);
        assertNoAssistant(turn);
        var versions = stateVersions();
        String parentNamespace =
                sql.queryForObject(
                        """
                SELECT agent_runtime.scope_hash(ARRAY['financeclaw-state-v1',tenant,subject,agent_id,release_hash])
                FROM business.turns WHERE id=?
                """,
                        String.class,
                        turn.id());
        assertThat(versions.stream().filter(row -> !beforeCancellation.contains(row)).toList())
                .allSatisfy(
                        row ->
                                assertThat(row.get("namespace"))
                                        .as("only parent cancellation bookkeeping may change")
                                        .isEqualTo(parentNamespace));
        gate.releaseAfterInvalidation();
        model.awaitIdle(Duration.ofSeconds(5));
        assertThat(gate.responseAttempted())
                .as("late SSE reply attempted after confirmed cancellation")
                .isTrue();
        assertThat(get(turn.path()).path("status").asText()).isEqualTo("cancelled");
        assertThat(stateVersions()).isEqualTo(versions);
        assertLateReplyRejected(turn);
    }

    @Test
    void revocationDuringChildHttpRejectsLateCompletion() throws Exception {
        var published = publish(32, false);
        var gate = heldChildScript(published);
        var turn =
                submit(
                        published.parentId(),
                        "Revoke permission while the child HTTP request is running.");
        assertThat(gate.awaitEntered(Duration.ofSeconds(10)))
                .as("child HTTP request reached SSE gate")
                .isTrue();
        String grantPath = "/v1/conversations/" + turn.conversation() + "/grant";
        long revision = get(grantPath).path("revision").asLong();
        var changed =
                send("PUT", grantPath, Map.of("revision", revision, "scopes", List.of()), 200);
        assertThat(changed.path("revision").asLong()).isEqualTo(revision + 1);
        var versions = stateVersions();
        gate.releaseAfterInvalidation();
        model.awaitIdle(Duration.ofSeconds(5));
        assertThat(gate.responseAttempted())
                .as("late SSE reply attempted after committed invalidation")
                .isTrue();
        var snapshot = awaitTerminal(turn);
        assertThat(snapshot.path("status").asText()).isEqualTo("blocked");
        assertThat(snapshot.path("reason").asText()).isEqualTo("authorization_changed");
        assertThat(snapshot.path("result").isNull()).isTrue();
        assertThat(stateVersions()).isEqualTo(versions);
        assertLateReplyRejected(turn);
    }

    @Test
    void completedChildReceiptAndFrozenTurnMemorySurviveSpringRestart() throws Exception {
        var published = publish(32, List.of("function:request_city"));
        String profileA = "PROFILE_A_FROZEN_FOR_WAITING_TURN";
        String profileB = "PROFILE_B_ONLY_FOR_NEXT_TURN";
        String changedLeafPrompt = "NEW_NON_LEAF_PROMPT_MUST_NOT_REPLACE_FROZEN_TARGET";
        String nextAnswer = "NEXT_TURN_USES_CURRENT_PROFILE";
        var canonicalReceipt = new AtomicReference<String>();
        updateProfile(profileA);
        waitingScript(
                published,
                request -> {
                    if (request.round() == 3) {
                        // 恢复输入必须带上原始已完成子回执与本次 HTTP 城市答复；画像仍是受理时的 A。
                        assertThat(canonicalReceipt.get()).isNotBlank();
                        assertThat(request.toolResults(CALL)).hasSize(1);
                        assertThat(request.lastToolResult(CALL)).isEqualTo(canonicalReceipt.get());
                        assertThat(object(request.lastToolResult(CALL)).path("answer").asText())
                                .isEqualTo(ANSWER);
                        assertThat(object(request.lastToolResult(CITY_CALL)).path("city").asText())
                                .isEqualTo(CITY);
                        assertThat(request.serialized())
                                .contains(profileA)
                                .doesNotContain(profileB, changedLeafPrompt);
                        assertThat(request.toolNames())
                                .containsExactlyInAnyOrder(
                                        published.tool(), "request_city", "read_artifact_range");
                        // 显式重复同 CALL 观察 SDK 对已完成 ID 的去重；旧结果来自恢复的检查点，
                        // 不再次进入 GovernedTools/子业务回执边界，也不再次扣根工具预算。
                        return PrivateAgentLoopback.Reply.tool(
                                CALL, published.tool(), Map.of("message", MESSAGE));
                    }
                    if (request.round() == 4) {
                        assertThat(canonicalReceipt.get()).isNotBlank();
                        assertThat(request.toolResults(CALL)).hasSize(1);
                        assertThat(request.lastToolResult(CALL)).isEqualTo(canonicalReceipt.get());
                        assertThat(object(request.lastToolResult(CALL)).path("answer").asText())
                                .isEqualTo(ANSWER);
                        assertThat(request.serialized())
                                .contains(profileA)
                                .doesNotContain(profileB, changedLeafPrompt);
                        return PrivateAgentLoopback.Reply.text(FINAL);
                    }
                    if (request.round() == 5) {
                        // 同一 conversation 的新 Turn 重新冻结画像；A 是系统记忆，不应混入继承工作投影。
                        assertThat(request.serialized())
                                .contains(profileB)
                                .doesNotContain(profileA, changedLeafPrompt);
                        return PrivateAgentLoopback.Reply.text(nextAnswer);
                    }
                    throw new AssertionError("Unexpected parent request after resume");
                });
        var paused =
                awaitWaiting(
                        published,
                        submit(published.parentId(), "Delegate, then ask me for a city."));
        // 以已封存 SQL 回执为独立基准，不能把同一 wire 列表的 first/last 相等当成恢复证据。
        canonicalReceipt.set(paused.childReceipt());
        assertThat((String) paused.memorySnapshot().get("context"))
                .contains(profileA)
                .doesNotContain(profileB);
        updateProfile(profileB);
        // current 改为非空工具 Agent，但本 Turn 的冻结 target/旧 SDK 结果保持不变。
        // 该重复 CALL 被 SDK 去重；这里只验旧结果与持久绑定，不声称重新运行目标或业务缓存重入。
        agents.update(
                OWNER,
                published.leaf().agentId(),
                1,
                "Changed non-leaf",
                changedLeafPrompt,
                new AgentComposition("primary", List.of("function:search_catalog")));
        var newer = agents.publish(OWNER, published.leaf().agentId(), 2, 1);
        assertThat(newer.releaseHash()).isNotEqualTo(published.leaf().releaseHash());
        assertThat(memorySnapshot(paused.turn())).isEqualTo(paused.memorySnapshot());

        restartSpring();
        assertWaitingWasNotReplayed(paused);
        var accepted = answer(paused, 202);
        assertThat(accepted.path("idempotent_replay").asBoolean()).isFalse();
        assertCompleted(paused.turn(), FINAL);
        assertThat(model.requests(PrivateAgentLoopback.PARENT)).hasSize(4);
        assertThat(model.requests(PrivateAgentLoopback.LEAF)).hasSize(1);
        assertCounters(paused.turn(), 5, 2, 1);
        assertResumedAttempt(paused);
        assertCompletedChildUnchanged(paused);
        assertThat(stateVersions())
                .containsAll(paused.sdkRows())
                .hasSize(paused.sdkRows().size() + 1);
        assertThat(childStateVersions(published.leaf()))
                .isEqualTo(
                        paused.sdkRows().stream()
                                .filter(
                                        row ->
                                                childNamespace(published.leaf())
                                                        .equals(row.get("namespace")))
                                .toList());
        assertThat(memorySnapshot(paused.turn())).isEqualTo(paused.memorySnapshot());
        assertThat(integer("SELECT count(*) FROM memory.sources WHERE id=?", paused.turn().id()))
                .isOne();
        assertThat(
                        integer(
                                "SELECT count(*) FROM business.journal WHERE turn_id=? AND role='assistant'",
                                paused.turn().id()))
                .isOne();
        for (var request : model.requests(PrivateAgentLoopback.PARENT)) {
            assertThat(request.serialized()).contains(profileA).doesNotContain(profileB);
        }
        assertThat(model.requests(PrivateAgentLoopback.LEAF).getFirst().serialized())
                .contains(LEAF_PROMPT, MESSAGE)
                .doesNotContain(profileA, profileB, changedLeafPrompt);

        // 回答幂等回放只返回原回执，不能让已完成的 Turn 再领取第三次 attempt 或重复调用模型。
        var replay = answer(paused, 202);
        assertThat(replay.path("idempotent_replay").asBoolean()).isTrue();
        assertThat(replay.path("interaction_id").asText())
                .isEqualTo(paused.question().path("id").asText());
        assertResumedAttempt(paused);
        assertThat(model.requests()).hasSize(5);
        assertCounters(paused.turn(), 5, 2, 1);

        var next =
                submitConversation(
                        paused.turn().conversation(),
                        "Use the newly maintained profile in this new Turn.");
        assertCompleted(next, nextAnswer);
        assertThat(model.requests(PrivateAgentLoopback.PARENT)).hasSize(5);
        assertThat(model.requests(PrivateAgentLoopback.LEAF)).hasSize(1);
        assertThat(
                        integer(
                                "SELECT model_calls FROM business.turn_policies WHERE turn_id=?",
                                next.id()))
                .isOne();
        assertThat(
                        integer(
                                "SELECT tool_calls FROM business.turn_policies WHERE turn_id=?",
                                next.id()))
                .isZero();
        assertThat(
                        integer(
                                "SELECT count(*) FROM business.child_invocations WHERE root_turn_id=?",
                                next.id()))
                .isZero();
        assertThat((String) memorySnapshot(next).get("context"))
                .contains(profileB)
                .doesNotContain(profileA);
        assertThat(memorySnapshot(paused.turn())).isEqualTo(paused.memorySnapshot());
    }

    @Test
    void resumedTurnCannotResetTheSharedRootModelBudget() throws Exception {
        // 父两次模型 + 叶子一次模型恰好耗尽 3 次根预算；暂停与新 attempt 不是新额度。
        var paused = pauseAfterCompletedChild(3);
        restartSpring();
        assertWaitingWasNotReplayed(paused);
        answer(paused, 202);
        var snapshot = awaitTerminal(paused.turn());
        assertThat(snapshot.path("status").asText()).isEqualTo("blocked");
        assertThat(snapshot.path("reason").asText()).isEqualTo("execution_budget_exhausted");
        assertThat(snapshot.path("result").isNull()).isTrue();
        assertThat(model.requests(PrivateAgentLoopback.PARENT)).hasSize(2);
        assertThat(model.requests(PrivateAgentLoopback.LEAF)).hasSize(1);
        assertCounters(paused.turn(), 3, 2, 1);
        assertResumedAttempt(paused);
        assertCompletedChildUnchanged(paused);
        assertThat(stateVersions()).containsAll(paused.sdkRows());
        assertThat(childStateVersions(paused.published().leaf()))
                .isEqualTo(
                        paused.sdkRows().stream()
                                .filter(
                                        row ->
                                                childNamespace(paused.published().leaf())
                                                        .equals(row.get("namespace")))
                                .toList());
        assertNoAssistant(paused.turn());
    }

    @Test
    void waitingCancellationSurvivesRestartAndRejectsOldAnswer() throws Exception {
        var paused = pauseAfterCompletedChild(32);
        var cancelled = send("POST", paused.turn().path() + "/cancel", Map.of(), 202);
        assertThat(cancelled.path("status").asText()).isEqualTo("cancelled");
        restartSpring();
        assertThat(get(paused.turn().path()).path("status").asText()).isEqualTo("cancelled");
        assertThat(answer(paused, 409).path("error").asText()).isEqualTo("stale_interaction");
        assertThat(get(paused.turn().path() + "/interactions").get(0).path("status").asText())
                .isEqualTo("invalid");
        assertNoResumedAttemptOrNetwork(paused);
        assertCompletedChildUnchanged(paused);
        assertThat(stateVersions()).isEqualTo(paused.sdkRows());
        assertNoAssistant(paused.turn());
    }

    @Test
    void waitingRevocationSurvivesRestartAndRejectsOldAnswer() throws Exception {
        var paused = pauseAfterCompletedChild(32);
        String path = "/v1/conversations/" + paused.turn().conversation() + "/grant";
        long revision = get(path).path("revision").asLong();
        var revoked = send("PUT", path, Map.of("revision", revision, "scopes", List.of()), 200);
        assertThat(revoked.path("revision").asLong()).isEqualTo(revision + 1);
        restartSpring();
        assertThat(get(path).path("revision").asLong()).isEqualTo(revision + 1);
        assertThat(answer(paused, 409).path("error").asText()).isEqualTo("authorization_changed");
        // 拒绝回答的事务没有迁移 Turn，也没有回答/重新入队；等待状态仍可由正常取消入口结束。
        assertThat(get(paused.turn().path()).path("status").asText()).isEqualTo("waiting");
        assertThat(get(paused.turn().path() + "/interactions").get(0).path("status").asText())
                .isEqualTo("pending");
        assertNoResumedAttemptOrNetwork(paused);
        assertCompletedChildUnchanged(paused);
        assertThat(stateVersions()).isEqualTo(paused.sdkRows());
        assertNoAssistant(paused.turn());
        send("POST", paused.turn().path() + "/cancel", Map.of(), 202);
    }

    @Test
    void waitingForgetErasesPrivateExecutionAndRejectsOldAnswerAfterRestart() throws Exception {
        updateProfile("WAITING_PROFILE_MUST_BE_FORGOTTEN");
        var paused = pauseAfterCompletedChild(32);
        long epoch = get("/v1/memory/privacy").path("epoch").asLong();
        var forgotten = send("POST", "/v1/memory/forget", Map.of("epoch", epoch), 200);
        assertThat(forgotten.path("epoch").asLong()).isEqualTo(epoch + 1);
        // 本 fixture 仅启用画像；本地擦除同步结束，不依赖证据 Worker 才能解除 purge 屏障。
        var privacy = get("/v1/memory/privacy");
        assertThat(privacy.path("epoch").asLong()).isEqualTo(epoch + 1);
        assertThat(privacy.path("purge_pending").asBoolean()).isFalse();
        assertForgottenExecution(paused);
        var tombstones = stateVersions();
        restartSpring();
        assertThat(answer(paused, 409).path("error").asText()).isEqualTo("memory_privacy_changed");
        assertThat(get(paused.turn().path()).path("status").asText()).isEqualTo("waiting");
        assertNoResumedAttemptOrNetwork(paused);
        assertForgottenExecution(paused);
        assertThat(stateVersions()).isEqualTo(tombstones);
        assertNoAssistant(paused.turn());
        send("POST", paused.turn().path() + "/cancel", Map.of(), 202);
    }

    /** 普通控制面发布三份不同证据，真正的子工具请求必须命中父计划中已冻结的 A。 */
    @Test
    void marketLeafReadsItsFrozenTargetInsteadOfParentOrGlobalCurrentMarket() throws Exception {
        var published = publishMarket(List.of());
        String changedPrompt = "CURRENT_MARKET_LEAF_PROMPT_MUST_NOT_REPLACE_FROZEN_A";
        // 普通草稿没有 rebase 能力：同一叶子 current 只改变提示词/hash，行情仍来自原 base A。
        // 独立发布全局 B 并创建 B 叶子，明确区分 target A、parent P 和当前全局 B 三份数据。
        publishMarketBase(PrivateAgentLoopback.LEAF, 32, MARKET_B);
        String newerLeaf = UUID.randomUUID().toString();
        agents.create(
                OWNER,
                newerLeaf,
                "Independent B leaf",
                "GLOBAL_B_LEAF_PROMPT",
                new AgentComposition("primary", List.of("function:market_snapshot")));
        String newerHash = agents.publish(OWNER, newerLeaf, 1, 0).releaseHash();
        assertThat(releases.resolve(newerHash).definition().business().marketJson())
                .isEqualTo(PayloadJson.canonical(Map.of(SYMBOL, MARKET_B)));
        agents.update(
                OWNER,
                published.leaf().agentId(),
                1,
                "Changed market leaf",
                changedPrompt,
                new AgentComposition("primary", List.of("function:market_snapshot")));
        String changedHash = agents.publish(OWNER, published.leaf().agentId(), 2, 1).releaseHash();
        assertThat(changedHash).isNotEqualTo(published.leaf().releaseHash());
        assertThat(releases.resolve(changedHash).definition().business().marketJson())
                .isEqualTo(PayloadJson.canonical(Map.of(SYMBOL, MARKET_A)));
        marketScript(
                published,
                MARKET_ANSWER,
                request -> {
                    assertThat(request.round()).isEqualTo(2);
                    assertThat(object(request.lastToolResult(CALL)).path("answer").asText())
                            .isEqualTo(MARKET_ANSWER);
                    assertThat(request.serialized())
                            .contains((String) MARKET_A.get("provider"))
                            .doesNotContain(
                                    (String) MARKET_P.get("provider"),
                                    (String) MARKET_B.get("provider"));
                    return PrivateAgentLoopback.Reply.text(FINAL);
                });
        var turn = submit(published.parentId(), "Use only the frozen target market evidence.");
        assertCompleted(turn, FINAL);
        assertThat(model.requests(PrivateAgentLoopback.PARENT)).hasSize(2);
        assertThat(model.requests(PrivateAgentLoopback.PARENT).getFirst().toolNames())
                .containsExactlyInAnyOrder(published.tool(), "read_artifact_range");
        assertThat(model.requests(PrivateAgentLoopback.LEAF)).hasSize(2);
        for (var request : model.requests(PrivateAgentLoopback.LEAF)) {
            assertThat(request.serialized())
                    .contains(LEAF_PROMPT, MESSAGE)
                    .doesNotContain(PARENT_PROMPT, changedPrompt, "GLOBAL_B_LEAF_PROMPT");
        }
        assertCounters(turn, 4, 2, 2, 1);
        assertMarketSnapshotReceipt(turn);
        assertThat(
                        sql.queryForObject(
                                "SELECT target_release_hash FROM business.child_invocations WHERE root_turn_id=?",
                                String.class,
                                turn.id()))
                .isEqualTo(published.leaf().releaseHash());
        assertThat(integer("SELECT count(*) FROM agent_runtime.unsafe_execution_data()"))
                .as("valid market binding and its SDK scope are safe recovery data")
                .isZero();
    }

    @ParameterizedTest
    @ValueSource(strings = {"read_artifact_range", "delegate"})
    void marketLeafCannotTurnCompileDependenciesOrNestedDelegationIntoTools(String forbidden)
            throws Exception {
        var published = publishMarket(List.of());
        String name = forbidden.equals("delegate") ? published.tool() : forbidden;
        model.reset(
                request -> {
                    if (request.model().equals(PrivateAgentLoopback.LEAF)) {
                        assertMarketSchema(request);
                        return PrivateAgentLoopback.Reply.tool(
                                "forged-market-tool",
                                name,
                                forbidden.equals("delegate")
                                        ? Map.of("message", "Nested delegation")
                                        : Map.of(
                                                "artifact_id",
                                                "guessed-artifact",
                                                "start",
                                                0,
                                                "max_bytes",
                                                512));
                    }
                    return request.round() == 1
                            ? PrivateAgentLoopback.Reply.tool(
                                    CALL, published.tool(), Map.of("message", MESSAGE))
                            : PrivateAgentLoopback.Reply.text("MARKET_TOOL_FAILURE_WAS_SWALLOWED");
                });
        var turn =
                submit(published.parentId(), "The market child cannot invent another capability.");
        var snapshot = awaitTerminal(turn);
        assertThat(snapshot.path("status").asText()).isEqualTo("blocked");
        assertThat(snapshot.path("reason").asText()).isEqualTo("child_tools_denied");
        assertThat(model.requests(PrivateAgentLoopback.PARENT)).hasSize(1);
        assertThat(model.requests(PrivateAgentLoopback.LEAF)).hasSize(1);
        assertCounters(turn, 2, 1, 1, 0);
        assertUnfinishedChild(turn);
        assertNoAssistant(turn);
        assertThat(
                        integer(
                                "SELECT count(*) FROM business.capability_steps WHERE turn_id=?",
                                turn.id()))
                .isZero();
        assertThat(integer("SELECT count(*) FROM business.artifacts")).isZero();
        assertThat(integer("SELECT count(*) FROM business.reports")).isZero();
    }

    @Test
    void cancellationAfterMarketSnapshotRejectsLateChildFinalWithoutErasingCommittedEvidence()
            throws Exception {
        var published = publishMarket(List.of());
        var finalGate = new AtomicReference<PrivateAgentLoopback.Gate>();
        String lateAnswer = "LATE_MARKET_CHILD_FINAL_MUST_NOT_COMMIT";
        String lateParent = "LATE_MARKET_PARENT_FINAL_MUST_NOT_COMMIT";
        marketScript(
                published,
                lateAnswer,
                request -> PrivateAgentLoopback.Reply.text(lateParent),
                finalGate);
        // reset 会释放并清理旧 gate，因此本次 gate 必须在脚本安装完成后创建。
        var gate = model.gate();
        finalGate.set(gate);
        var turn = submit(published.parentId(), "Cancel after the market evidence has been read.");
        assertThat(gate.awaitEntered(Duration.ofSeconds(10))).isTrue();
        // 第二次子模型请求已携带成功快照；取消不能撤销已经提交的读回执，也不能允许迟到最终答复收尾。
        assertMarketSnapshotReceipt(turn);
        assertCounters(turn, 3, 2, 2, 1);
        var childVersions = childStateVersions(published.leaf());
        send("POST", turn.path() + "/cancel", Map.of(), 202);
        var cancelled = awaitTerminal(turn);
        assertThat(cancelled.path("status").asText()).isEqualTo("cancelled");
        assertThat(cancelled.path("result").isNull()).isTrue();
        assertThat(childStateVersions(published.leaf())).isEqualTo(childVersions);
        var terminalVersions = stateVersions();
        gate.releaseAfterInvalidation();
        model.awaitIdle(Duration.ofSeconds(5));
        assertThat(gate.responseAttempted()).isTrue();
        assertThat(stateVersions()).isEqualTo(terminalVersions);
        assertThat(get(turn.path()).path("status").asText()).isEqualTo("cancelled");
        assertCounters(turn, 3, 2, 2, 1);
        assertMarketSnapshotReceipt(turn);
        assertUnfinishedChild(turn);
        assertNoAssistant(turn);
        assertThat(model.requests(PrivateAgentLoopback.PARENT)).hasSize(1);
        assertThat(model.requests(PrivateAgentLoopback.LEAF)).hasSize(2);
        for (String forbidden : List.of(lateAnswer, lateParent)) {
            assertThat(
                            integer(
                                    "SELECT count(*) FROM agent_runtime.states WHERE payload::text LIKE ?",
                                    "%" + forbidden + "%"))
                    .isZero();
            assertThat(
                            integer(
                                    "SELECT count(*) FROM agent_runtime.work_contexts WHERE (payload::text || transcript::text) LIKE ?",
                                    "%" + forbidden + "%"))
                    .isZero();
        }
    }

    @Test
    void completedMarketEvidenceSurvivesWaitingRestartAndNativeResolvedCallReplay()
            throws Exception {
        var published = publishMarket(List.of("function:request_city"));
        var canonical = new AtomicReference<String>();
        marketScript(
                published,
                MARKET_ANSWER,
                request -> {
                    if (request.round() == 2) {
                        assertThat(object(request.lastToolResult(CALL)).path("answer").asText())
                                .isEqualTo(MARKET_ANSWER);
                        return PrivateAgentLoopback.Reply.tool(CITY_CALL, "request_city", Map.of());
                    }
                    assertThat(canonical.get()).isNotBlank();
                    assertThat(request.toolResults(CALL)).hasSize(1);
                    assertThat(request.lastToolResult(CALL)).isEqualTo(canonical.get());
                    assertThat(object(request.lastToolResult(CITY_CALL)).path("city").asText())
                            .isEqualTo(CITY);
                    if (request.round() == 3) {
                        // SDK 已完成 ID 去重，不重入子业务调用；其余恢复事实仍由数据库独立核验。
                        return PrivateAgentLoopback.Reply.tool(
                                CALL, published.tool(), Map.of("message", MESSAGE));
                    }
                    assertThat(request.round()).isEqualTo(4);
                    return PrivateAgentLoopback.Reply.text(FINAL);
                });
        var paused =
                awaitWaiting(
                        published,
                        submit(
                                published.parentId(),
                                "Read market evidence, then ask me for a city."),
                        2,
                        1);
        canonical.set(paused.childReceipt());
        assertMarketSnapshotReceipt(paused.turn());
        String snapshotReceipt = marketSnapshotReceipt(paused.turn());
        restartSpring();
        assertWaitingWasNotReplayed(paused);
        assertThat(answer(paused, 202).path("idempotent_replay").asBoolean()).isFalse();
        assertCompleted(paused.turn(), FINAL);
        assertResumedAttempt(paused);
        assertCompletedChildUnchanged(paused);
        assertThat(marketSnapshotReceipt(paused.turn())).isEqualTo(snapshotReceipt);
        assertCounters(paused.turn(), 6, 3, 2, 1);
        assertThat(model.requests(PrivateAgentLoopback.PARENT)).hasSize(4);
        assertThat(model.requests(PrivateAgentLoopback.LEAF)).hasSize(2);
        assertThat(childStateVersions(published.leaf()))
                .isEqualTo(
                        paused.sdkRows().stream()
                                .filter(
                                        row ->
                                                childNamespace(published.leaf())
                                                        .equals(row.get("namespace")))
                                .toList());
        assertThat(stateVersions())
                .containsAll(paused.sdkRows())
                .hasSize(paused.sdkRows().size() + 1);
        assertThat(answer(paused, 202).path("idempotent_replay").asBoolean()).isTrue();
        assertResumedAttempt(paused);
        assertCounters(paused.turn(), 6, 3, 2, 1);
        assertThat(model.requests()).hasSize(6);
    }

    @Test
    void marketArchiveKeepsMarketScopeAfterOnlyMarketPermissionIsRevoked() throws Exception {
        var published = publishMarket(List.of());
        String head = "MARKET_ARCHIVE_VISIBLE_HEAD";
        String tail = "MARKET_ARCHIVE_UNREAD_TAIL";
        String answer = head + "x".repeat(14000) + tail;
        String deniedFinal = "CURRENT_READER_OBSERVED_MARKET_SCOPE_DENIAL";
        var reference = new AtomicReference<JsonNode>();
        var chunk = new AtomicReference<JsonNode>();
        marketScript(
                published,
                answer,
                request -> {
                    if (request.round() == 2) {
                        assertThat(request.serialized()).doesNotContain(head, tail, answer);
                        var ref = object(request.lastToolResult(CALL));
                        assertThat(ref.path("archived").asBoolean()).isTrue();
                        reference.set(ref);
                        return rangeRequest("market-range", ref.path("artifact_id").asText());
                    }
                    if (request.round() == 3) {
                        var read = object(request.lastToolResult("market-range"));
                        assertThat(read.path("start").asInt()).isZero();
                        assertThat(read.path("count").asInt()).isBetween(1, 512);
                        assertThat(read.path("text").asText()).contains(head).doesNotContain(tail);
                        assertThat(request.serialized())
                                .contains(head)
                                .doesNotContain(tail, answer);
                        chunk.set(read);
                        return PrivateAgentLoopback.Reply.text(FINAL);
                    }
                    if (request.round() == 4) {
                        // 这是使用最新 grant revision 受理的新 Turn，不能靠旧 Claim 的通用 revision 栅栏制造假阳性。
                        assertThat(request.toolNames()).containsExactly("read_artifact_range");
                        assertThat(request.serialized()).doesNotContain(tail, answer);
                        return rangeRequest(
                                "revoked-market-range",
                                reference.get().path("artifact_id").asText());
                    }
                    assertThat(request.round()).isEqualTo(5);
                    assertThat(request.lastToolResult("revoked-market-range"))
                            .contains("artifact_scope_denied")
                            .doesNotContain(head, tail, answer);
                    return PrivateAgentLoopback.Reply.text(deniedFinal);
                });
        var turn = submit(published.parentId(), "Read only a bounded part of the market answer.");
        assertCompleted(turn, FINAL);
        assertCounters(turn, 5, 3, 2, 1);
        assertMarketSnapshotReceipt(turn);
        var ref = reference.get();
        assertThat(ref).isNotNull();
        assertThat(chunk.get()).isNotNull();
        String id = ref.path("artifact_id").asText();
        var download = raw("GET", ref.path("download_url").asText(), null, Map.of(), 200);
        String archived = new String(download.body(), StandardCharsets.UTF_8);
        assertThat(archived).contains(head, tail, answer);
        assertThat(download.body()).hasSize(ref.path("bytes").asInt());
        assertThat(PayloadDigest.sha256(archived)).isEqualTo(ref.path("hash").asText());
        assertThat(download.headers().firstValue("X-Artifact-SHA256"))
                .contains(ref.path("hash").asText());
        assertThat(chunk.get().path("text").asText())
                .isEqualTo(new String(download.body(), 0, 512, StandardCharsets.UTF_8));
        assertThat(chunk.get().path("nextStart").asInt()).isEqualTo(512);
        assertThat(
                        sql.queryForMap(
                                "SELECT required_scope,privacy_bound FROM business.artifacts WHERE id=?",
                                id))
                .containsEntry("required_scope", "market:read")
                .containsEntry("privacy_bound", true);
        assertParentContextHasOnlyReadChunk(turn, head, tail, answer);

        String grantPath = "/v1/conversations/" + turn.conversation() + "/grant";
        long revision = get(grantPath).path("revision").asLong();
        var changed =
                send(
                        "PUT",
                        grantPath,
                        Map.of("revision", revision, "scopes", List.of("artifact:read")),
                        200);
        assertThat(changed.path("revision").asLong()).isEqualTo(revision + 1);
        assertThat(
                        new String(
                                raw("GET", ref.path("download_url").asText(), null, Map.of(), 403)
                                        .body(),
                                StandardCharsets.UTF_8))
                .contains("artifact_scope_denied");
        var reader =
                submitConversation(
                        turn.conversation(),
                        "Read that archived market result with my current grant.");
        // SDK 可继续一次推理解释拒权，但 ERROR 工具回执仍使最终检查点判为 failed，
        // 不能把模型的拒权说明作为业务成功答案或 assistant journal 提交。
        var denied = awaitTerminal(reader);
        assertThat(denied.path("status").asText()).isEqualTo("failed");
        assertThat(denied.path("result").isNull()).isTrue();
        assertNoAssistant(reader);
        assertThat(
                        sql.queryForObject(
                                "SELECT grant_revision FROM business.turn_policies WHERE turn_id=?",
                                Long.class,
                                reader.id()))
                .isEqualTo(revision + 1);
        assertThat(
                        integer(
                                "SELECT model_calls FROM business.turn_policies WHERE turn_id=?",
                                reader.id()))
                .isEqualTo(2);
        assertThat(
                        integer(
                                "SELECT tool_calls FROM business.turn_policies WHERE turn_id=?",
                                reader.id()))
                .isOne();
        assertThat(
                        integer(
                                "SELECT count(*) FROM business.child_invocations WHERE root_turn_id=?",
                                reader.id()))
                .isZero();
        assertThat(model.requests(PrivateAgentLoopback.PARENT)).hasSize(5);
        assertThat(model.requests(PrivateAgentLoopback.LEAF)).hasSize(2);
        assertThat(
                        sql.queryForObject(
                                "SELECT required_scope FROM business.artifacts WHERE id=?",
                                String.class,
                                id))
                .isEqualTo("market:read");
    }

    /** 正常模型先完成一次子调用，再主动选择无参数 request_city 形成持久暂停。 */
    @Test
    void controlledChildClarificationAndExactReportApprovalResumeAcrossTwoRestarts()
            throws Exception {
        var published =
                publishControlled(
                        List.of("function:request_city", "function:create_report"), List.of());
        model.reset(
                request -> {
                    if (request.model().equals(PrivateAgentLoopback.PARENT)) {
                        if (request.round() == 1)
                            return PrivateAgentLoopback.Reply.tool(
                                    CALL, published.tool(), Map.of("message", MESSAGE));
                        assertThat(request.lastToolResult(CALL)).contains(ANSWER);
                        return PrivateAgentLoopback.Reply.text(FINAL);
                    }
                    assertThat(request.toolNames())
                            .containsExactlyInAnyOrder(
                                    "request_city", "create_report", "read_artifact_range");
                    if (request.round() == 1)
                        return PrivateAgentLoopback.Reply.tool(
                                "child-city", "request_city", Map.of());
                    if (request.round() == 2) {
                        assertThat(request.lastToolResult("child-city")).contains(CITY);
                        return PrivateAgentLoopback.Reply.tool(
                                "child-report",
                                "create_report",
                                Map.of("title", "CHILD_APPROVED_TITLE"));
                    }
                    assertThat(request.lastToolResult("child-report"))
                            .contains("report_id", "created");
                    return PrivateAgentLoopback.Reply.text(ANSWER);
                });
        var turn = submit(published.parentId(), "controlled child");
        var first = awaitChildQuestions(turn, 1).getFirst();
        assertThat(first.path("kind").asText()).isEqualTo("input");
        assertThat(
                        sql.queryForObject(
                                "SELECT call_id FROM business.interactions WHERE id=?",
                                String.class,
                                first.path("id").asText()))
                .startsWith("ci-");
        assertThat(integer("SELECT count(*) FROM business.reports WHERE turn_id=?", turn.id()))
                .isZero();
        restartSpring();
        answerChild(turn, first, "approve", 202);
        var second = awaitChildQuestions(turn, 1).getFirst();
        assertThat(second.path("kind").asText()).isEqualTo("approval");
        assertThat(second.path("action_hash").asText()).hasSize(64);
        assertThat(second.path("id").asText()).isNotEqualTo(first.path("id").asText());
        assertThat(
                        sql.queryForList(
                                "SELECT result::jsonb ->> 'childVersion' AS version FROM business.capability_steps WHERE turn_id=? AND step LIKE 'interaction:%'",
                                String.class, turn.id()))
                .hasSize(2);
        // Replaying an already accepted old answer cannot answer the new child checkpoint.
        answerChild(turn, first, "approve", 202);
        assertThat(get(turn.path()).path("status").asText()).isEqualTo("waiting");
        assertThat(integer("SELECT count(*) FROM business.reports WHERE turn_id=?", turn.id()))
                .isZero();
        restartSpring();
        answerChild(turn, second, "approve", 202);
        assertCompleted(turn, FINAL);
        assertThat(integer("SELECT count(*) FROM business.reports WHERE turn_id=?", turn.id()))
                .isOne();
        assertThat(
                        sql.queryForObject(
                                "SELECT call_id FROM business.reports WHERE turn_id=?",
                                String.class,
                                turn.id()))
                .startsWith("child-");
        assertThat(
                        integer(
                                "SELECT model_calls FROM business.child_invocations WHERE root_turn_id=?",
                                turn.id()))
                .isEqualTo(3);
        assertThat(
                        integer(
                                "SELECT count(*) FROM business.attempts a JOIN business.commands c ON c.id=a.command_id WHERE c.turn_id=?",
                                turn.id()))
                .isEqualTo(3);
        assertThat(
                        integer(
                                "SELECT count(*) FROM agent_runtime.child_invocation_bindings() WHERE root_turn_id=? AND NOT binding_valid",
                                turn.id()))
                .isZero();
        answerChild(turn, second, "approve", 202);
        assertThat(integer("SELECT count(*) FROM business.reports WHERE turn_id=?", turn.id()))
                .isOne();
    }

    @Test
    void controlledChildBatchesTwoApprovalsAndClarificationWithoutLosingAnyAnswer()
            throws Exception {
        var published =
                publishControlled(
                        List.of("function:request_city", "function:create_report"),
                        List.of("function:request_city"));
        model.reset(
                request -> {
                    if (request.model().equals(PrivateAgentLoopback.PARENT)) {
                        if (request.round() == 1)
                            return PrivateAgentLoopback.Reply.tools(
                                    PrivateAgentLoopback.Reply.tool(
                                            CALL, published.tool(), Map.of("message", MESSAGE)),
                                    PrivateAgentLoopback.Reply.tool(
                                            "root-city", "request_city", Map.of()));
                        assertThat(request.lastToolResult("root-city")).contains(CITY);
                        assertThat(request.lastToolResult(CALL)).contains(ANSWER);
                        return PrivateAgentLoopback.Reply.text(FINAL);
                    }
                    if (request.round() == 1)
                        return PrivateAgentLoopback.Reply.tools(
                                PrivateAgentLoopback.Reply.tool(
                                        "child-report-a", "create_report", Map.of("title", "A")),
                                PrivateAgentLoopback.Reply.tool(
                                        "child-report-b", "create_report", Map.of("title", "B")),
                                PrivateAgentLoopback.Reply.tool(
                                        "child-city", "request_city", Map.of()));
                    assertThat(request.lastToolResult("child-city")).contains(CITY);
                    assertThat(request.lastToolResult("child-report-a")).contains("created");
                    assertThat(request.lastToolResult("child-report-b")).contains("created");
                    return PrivateAgentLoopback.Reply.text(ANSWER);
                });
        var turn = submit(published.parentId(), "multiple child actions");
        var questions = awaitChildQuestions(turn, 4);
        assertThat(
                        integer(
                                "SELECT count(DISTINCT call_id) FROM business.interactions WHERE turn_id=?",
                                turn.id()))
                .isEqualTo(4);
        restartSpring();
        for (int i = 0; i < questions.size() - 1; i++) {
            answerChild(turn, questions.get(i), "approve", 202);
            assertThat(get(turn.path()).path("status").asText()).isEqualTo("waiting");
            assertThat(integer("SELECT count(*) FROM business.reports WHERE turn_id=?", turn.id()))
                    .isZero();
        }
        answerChild(turn, questions.getLast(), "approve", 202);
        assertCompleted(turn, FINAL);
        assertThat(integer("SELECT count(*) FROM business.reports WHERE turn_id=?", turn.id()))
                .isEqualTo(2);
        assertThat(model.requests(PrivateAgentLoopback.LEAF)).hasSize(2);
        assertThat(integer("SELECT count(*) FROM agent_runtime.unsafe_execution_data()")).isZero();
    }

    @Test
    void childApprovalRejectAndTamperedActionCannotCreateReport() throws Exception {
        var published = publishControlled(List.of("function:create_report"), List.of());
        model.reset(
                request ->
                        request.model().equals(PrivateAgentLoopback.LEAF)
                                ? PrivateAgentLoopback.Reply.tool(
                                        "effect", "create_report", Map.of("title", "UNAPPROVED"))
                                : PrivateAgentLoopback.Reply.tool(
                                        CALL, published.tool(), Map.of("message", MESSAGE)));
        var turn = submit(published.parentId(), "reject this effect");
        var question = awaitChildQuestions(turn, 1).getFirst();
        send(
                "POST",
                turn.path() + "/interactions/" + question.path("id").asText() + "/answer",
                Map.of(
                        "kind",
                        "approval",
                        "revision",
                        question.path("revision").asLong(),
                        "decision",
                        "approve",
                        "action_hash",
                        "0".repeat(64)),
                409);
        assertThat(integer("SELECT count(*) FROM business.reports WHERE turn_id=?", turn.id()))
                .isZero();
        answerChild(turn, question, "reject", 202);
        assertThat(awaitTerminal(turn).path("status").asText()).isNotEqualTo("completed");
        assertThat(integer("SELECT count(*) FROM business.reports WHERE turn_id=?", turn.id()))
                .isZero();
    }

    @Test
    void forgettingControlledWaitingChildErasesBridgesAndAllAttemptStates() throws Exception {
        var published =
                publishControlled(
                        List.of("function:request_city", "function:create_report"), List.of());
        model.reset(
                request -> {
                    if (request.model().equals(PrivateAgentLoopback.PARENT))
                        return PrivateAgentLoopback.Reply.tool(
                                CALL, published.tool(), Map.of("message", MESSAGE));
                    return request.round() == 1
                            ? PrivateAgentLoopback.Reply.tool("city", "request_city", Map.of())
                            : PrivateAgentLoopback.Reply.tool(
                                    "effect", "create_report", Map.of("title", "FORGOTTEN_ACTION"));
                });
        var turn = submit(published.parentId(), "private controlled input");
        answerChild(turn, awaitChildQuestions(turn, 1).getFirst(), "approve", 202);
        var approval = awaitChildQuestions(turn, 1).getFirst();
        assertThat(childStateVersions(published.leaf())).hasSize(2);
        assertThat(integer("SELECT count(*) FROM agent_runtime.unsafe_execution_data()")).isZero();
        long epoch = get("/v1/memory/privacy").path("epoch").asLong();
        send("POST", "/v1/memory/forget", Map.of("epoch", epoch), 200);
        assertThat(
                        integer(
                                "SELECT count(*) FROM business.child_invocations WHERE root_turn_id=?",
                                turn.id()))
                .isZero();
        assertThat(
                        integer(
                                "SELECT count(*) FROM business.capability_steps WHERE turn_id=? AND namespace LIKE 'child:%'",
                                turn.id()))
                .isZero();
        assertThat(integer("SELECT count(*) FROM agent_runtime.states WHERE payload IS NOT NULL"))
                .isZero();
        assertThat(integer("SELECT count(*) FROM agent_runtime.unsafe_execution_data()")).isZero();
        restartSpring();
        answerChild(turn, approval, "approve", 409);
        assertThat(integer("SELECT count(*) FROM business.reports WHERE turn_id=?", turn.id()))
                .isZero();
        send("POST", turn.path() + "/cancel", Map.of(), 202);
    }

    @Test
    void genericFunctionAndNativeSkillLeafArchivesAllSourceScopes() throws Exception {
        var published =
                publishControlled(
                        List.of("function:search_catalog", "skill:cocktail-from-what-i-have"),
                        List.of());
        assertThat(published.leaf().profile()).isEqualTo(PrivateAgentTarget.Profile.READ_ONLY);
        String skillId;
        try (var selected =
                PackagedSkills.selected(
                        releases.resolve(published.leaf().releaseHash()).plan().capabilities())) {
            skillId = selected.getSkill("cocktail-from-what-i-have").getSkillId();
        }
        String large = "MIXED_SOURCE_HEAD" + "x".repeat(14000) + "MIXED_SOURCE_TAIL";
        var artifact = new AtomicReference<JsonNode>();
        model.reset(
                request -> {
                    if (request.model().equals(PrivateAgentLoopback.PARENT)) {
                        if (request.round() == 1)
                            return PrivateAgentLoopback.Reply.tool(
                                    CALL, published.tool(), Map.of("message", MESSAGE));
                        var ref = object(request.lastToolResult(CALL));
                        assertThat(ref.path("archived").asBoolean()).isTrue();
                        assertThat(request.serialized()).doesNotContain("MIXED_SOURCE_TAIL");
                        artifact.set(ref);
                        return PrivateAgentLoopback.Reply.text(FINAL);
                    }
                    assertThat(request.toolNames())
                            .containsExactlyInAnyOrder(
                                    "search_catalog",
                                    "read_artifact_range",
                                    "load_skill_through_path");
                    if (request.round() == 1)
                        return PrivateAgentLoopback.Reply.tools(
                                PrivateAgentLoopback.Reply.tool(
                                        "catalog", "search_catalog", Map.of("city", CITY)),
                                PrivateAgentLoopback.Reply.tool(
                                        "skill",
                                        "load_skill_through_path",
                                        Map.of("skillId", skillId, "path", "SKILL.md")));
                    assertThat(request.lastToolResult("catalog")).contains("artifact_id");
                    assertThat(request.lastToolResult("skill"))
                            .contains("Cocktail From What I Have");
                    return PrivateAgentLoopback.Reply.text(large);
                });
        var turn = submit(published.parentId(), "use the selected function and skill");
        assertCompleted(turn, FINAL);
        assertThat(
                        integer(
                                "SELECT tool_calls FROM business.child_invocations WHERE root_turn_id=?",
                                turn.id()))
                .isEqualTo(2);
        assertThat(
                        sql.queryForList(
                                "SELECT required_scope FROM business.artifacts WHERE turn_id=?",
                                String.class,
                                turn.id()))
                .contains(
                        com.financeclaw.support.ScopeRequirements.encode(
                                List.of("artifact:read", "catalog:read")));
        String id = artifact.get().path("artifact_id").asText();
        assertThat(
                        sql.queryForObject(
                                "SELECT required_scope FROM business.artifacts WHERE id=?",
                                String.class,
                                id))
                .isEqualTo(published.leaf().resultScope());
        raw("GET", artifact.get().path("download_url").asText(), null, Map.of(), 200);
        String grantPath = "/v1/conversations/" + turn.conversation() + "/grant";
        var grant = get(grantPath);
        var narrowed = new ArrayList<String>();
        for (var scope : grant.path("scopes"))
            if (!scope.asText().equals("skill:use")) narrowed.add(scope.asText());
        assertThat(narrowed).contains("catalog:read", "artifact:read");
        send(
                "PUT",
                grantPath,
                Map.of("revision", grant.path("revision").asLong(), "scopes", narrowed),
                200);
        assertThat(
                        new String(
                                raw(
                                                "GET",
                                                artifact.get().path("download_url").asText(),
                                                null,
                                                Map.of(),
                                                403)
                                        .body(),
                                StandardCharsets.UTF_8))
                .contains("artifact_scope_denied");
        assertThat(integer("SELECT count(*) FROM agent_runtime.unsafe_execution_data()")).isZero();
    }

    @ParameterizedTest
    @ValueSource(booleans = {false, true})
    void pendingControlledChildCannotApproveAfterCancelOrParentRevocation(boolean revoke)
            throws Exception {
        var published = publishControlled(List.of("function:create_report"), List.of());
        model.reset(
                request ->
                        request.model().equals(PrivateAgentLoopback.LEAF)
                                ? PrivateAgentLoopback.Reply.tool(
                                        "effect",
                                        "create_report",
                                        Map.of("title", "FORBIDDEN_AFTER_STOP"))
                                : PrivateAgentLoopback.Reply.tool(
                                        CALL, published.tool(), Map.of("message", MESSAGE)));
        var turn = submit(published.parentId(), "wait for approval");
        var question = awaitChildQuestions(turn, 1).getFirst();
        if (revoke) {
            String path = "/v1/conversations/" + turn.conversation() + "/grant";
            send(
                    "PUT",
                    path,
                    Map.of("revision", get(path).path("revision").asLong(), "scopes", List.of()),
                    200);
        } else send("POST", turn.path() + "/cancel", Map.of(), 202);
        restartSpring();
        answerChild(turn, question, "approve", 409);
        assertThat(model.requests(PrivateAgentLoopback.LEAF)).hasSize(1);
        assertThat(integer("SELECT count(*) FROM business.reports WHERE turn_id=?", turn.id()))
                .isZero();
        if (revoke) send("POST", turn.path() + "/cancel", Map.of(), 202);
    }

    @Test
    void sharedControlledLeafRevocationRejectsPendingAnswerAndNewDispatch() throws Exception {
        var publisher = new Owner(OWNER.tenant(), "publisher-" + UUID.randomUUID());
        var access =
                application.getBean(com.financeclaw.app.agent.port.ResourceAdministration.class);
        access.change(OWNER, "credential", "fixture-model", publisher, null, 0, true);
        publishBase(PrivateAgentLoopback.LEAF, 32);
        String leaf = UUID.randomUUID().toString();
        agents.create(
                publisher,
                leaf,
                "Shared controlled leaf",
                LEAF_PROMPT,
                new AgentComposition("primary", List.of("function:create_report")));
        String hash = agents.publish(publisher, leaf, 1, 0).releaseHash();
        access.change(publisher, "agent", leaf, OWNER, hash, 0, true);
        publishBase(PrivateAgentLoopback.PARENT, 32);
        String parent = UUID.randomUUID().toString();
        agents.create(
                OWNER,
                parent,
                "Shared controlled parent",
                PARENT_PROMPT,
                new AgentComposition("primary", List.of("agent:" + leaf)));
        agents.publish(OWNER, parent, 1, 0);
        var published =
                new Published(
                        parent,
                        PrivateAgentTarget.fromRelease(publisher, hash, releases.resolve(hash)));
        var archived = new AtomicReference<JsonNode>();
        model.reset(
                request -> {
                    if (request.model().equals(PrivateAgentLoopback.LEAF))
                        return PrivateAgentLoopback.Reply.text(
                                "SHARED_ARCHIVE_" + "x".repeat(14000));
                    if (request.round() == 1)
                        return PrivateAgentLoopback.Reply.tool(
                                CALL, published.tool(), Map.of("message", MESSAGE));
                    archived.set(object(request.lastToolResult(CALL)));
                    return PrivateAgentLoopback.Reply.text(FINAL);
                });
        var completed = submit(parent, "shared source result");
        assertCompleted(completed, FINAL);
        assertThat(integer("SELECT count(*) FROM agent_runtime.unsafe_execution_data()")).isZero();
        raw("GET", archived.get().path("download_url").asText(), null, Map.of(), 200);
        model.reset(
                request ->
                        request.model().equals(PrivateAgentLoopback.LEAF)
                                ? PrivateAgentLoopback.Reply.tool(
                                        "effect",
                                        "create_report",
                                        Map.of("title", "REVOKED_SHARED_EFFECT"))
                                : PrivateAgentLoopback.Reply.tool(
                                        CALL, published.tool(), Map.of("message", MESSAGE)));
        var turn = submit(parent, "shared leaf approval");
        var question = awaitChildQuestions(turn, 1).getFirst();
        access.change(publisher, "agent", leaf, OWNER, hash, 1, false);
        assertThat(
                        new String(
                                raw(
                                                "GET",
                                                archived.get().path("download_url").asText(),
                                                null,
                                                Map.of(),
                                                409)
                                        .body(),
                                StandardCharsets.UTF_8))
                .contains("agent_access_denied");
        raw("GET", completed.path(), null, Map.of(), 403);
        raw("GET", turn.path() + "/interactions", null, Map.of(), 403);
        raw("GET", "/v1/interactions/" + question.path("id").asText(), null, Map.of(), 403);
        raw(
                "GET",
                "/v1/conversations/" + completed.conversation() + "/messages",
                null,
                Map.of(),
                403);
        var queries = application.getBean(com.financeclaw.app.task.TaskQueryService.class);
        var gateway = application.getBean(com.financeclaw.app.task.port.TaskQueryGateway.class);
        var committed = gateway.ownedTurn(OWNER, completed.conversation(), completed.id());
        assertThatThrownBy(() -> queries.replay(committed, 0)).hasMessage("agent_access_denied");
        assertThatThrownBy(
                        () ->
                                application
                                        .getBean(
                                                com.financeclaw.app.task.port.TaskEventAccess.class)
                                        .check(committed))
                .hasMessage("agent_access_denied");
        restartSpring();
        answerChild(turn, question, "approve", 409);
        int commands = integer("SELECT count(*) FROM business.commands");
        String deniedConversation = conversation(parent);
        assertThat(
                        send(
                                        "POST",
                                        "/v1/conversations/" + deniedConversation + "/turns",
                                        Map.of("message", "revoked shared target"),
                                        403)
                                .path("error")
                                .asText())
                .isEqualTo("agent_access_denied");
        assertThat(
                        integer(
                                "SELECT count(*) FROM business.turns WHERE conversation_id=?",
                                deniedConversation))
                .isZero();
        assertThat(integer("SELECT count(*) FROM business.commands")).isEqualTo(commands);
        assertThat(model.requests(PrivateAgentLoopback.LEAF)).hasSize(1);
        assertThat(integer("SELECT count(*) FROM business.reports WHERE turn_id=?", turn.id()))
                .isZero();
        // Revocation denies reads/execution but does not make known historical provenance unsafe.
        assertThat(integer("SELECT count(*) FROM agent_runtime.unsafe_execution_data()")).isZero();
        send("POST", turn.path() + "/cancel", Map.of(), 202);
    }

    private Published publishControlled(
            List<String> leafCapabilities, List<String> parentCapabilities) {
        publishBase(PrivateAgentLoopback.LEAF, 32);
        String leaf = UUID.randomUUID().toString();
        agents.create(
                OWNER,
                leaf,
                "Controlled leaf",
                LEAF_PROMPT,
                new AgentComposition("primary", leafCapabilities));
        String hash = agents.publish(OWNER, leaf, 1, 0).releaseHash();
        var target = PrivateAgentTarget.fromRelease(OWNER, hash, releases.resolve(hash));
        publishBase(PrivateAgentLoopback.PARENT, 32);
        String parent = UUID.randomUUID().toString();
        var requested = new ArrayList<String>(parentCapabilities);
        requested.add("agent:" + leaf);
        agents.create(
                OWNER,
                parent,
                "Controlled parent",
                PARENT_PROMPT,
                new AgentComposition("primary", requested));
        agents.publish(OWNER, parent, 1, 0);
        return new Published(parent, target);
    }

    private List<JsonNode> awaitChildQuestions(Turn turn, int count) throws Exception {
        awaitStatus(turn, "waiting");
        var result = new ArrayList<JsonNode>();
        for (var question : get(turn.path() + "/interactions"))
            if (question.path("status").asText().equals("pending")) result.add(question);
        assertThat(result).hasSize(count);
        return result;
    }

    private JsonNode answerChild(Turn turn, JsonNode question, String decision, int status)
            throws Exception {
        var body = new LinkedHashMap<String, Object>();
        body.put("kind", question.path("kind").asText());
        body.put("revision", question.path("revision").asLong());
        if (question.path("kind").asText().equals("approval")) {
            body.put("decision", decision);
            body.put("action_hash", question.path("action_hash").asText());
        } else body.put("answer", Map.of("city", CITY));
        return send(
                "POST",
                turn.path() + "/interactions/" + question.path("id").asText() + "/answer",
                body,
                status);
    }

    private void waitingScript(
            Published published,
            Function<PrivateAgentLoopback.Request, PrivateAgentLoopback.Reply> continuation) {
        model.reset(
                request -> {
                    if (request.model().equals(PrivateAgentLoopback.LEAF)) {
                        assertThat(request.round())
                                .as("completed child must not make another HTTP request")
                                .isOne();
                        return PrivateAgentLoopback.Reply.text(ANSWER);
                    }
                    if (request.round() == 1) {
                        return PrivateAgentLoopback.Reply.tool(
                                CALL, published.tool(), Map.of("message", MESSAGE));
                    }
                    if (request.round() == 2) {
                        assertThat(object(request.lastToolResult(CALL)).path("answer").asText())
                                .isEqualTo(ANSWER);
                        return PrivateAgentLoopback.Reply.tool(CITY_CALL, "request_city", Map.of());
                    }
                    return continuation.apply(request);
                });
    }

    private Waiting pauseAfterCompletedChild(int rootModels) throws Exception {
        var published = publish(rootModels, List.of("function:request_city"));
        waitingScript(
                published,
                request -> {
                    throw new AssertionError(
                            "No additional model HTTP request is authorized in this scenario");
                });
        return awaitWaiting(
                published,
                submit(published.parentId(), "Delegate once, then wait for my city answer."));
    }

    /** 先观察 HTTP waiting 与已封存问题，再读数据库核对，不用 SQL 制造暂停或后续 attempt。 */
    private Waiting awaitWaiting(Published published, Turn turn) throws Exception {
        return awaitWaiting(published, turn, 1, 0);
    }

    private Waiting awaitWaiting(Published published, Turn turn, int childModels, int childTools)
            throws Exception {
        var snapshot = awaitStatus(turn, "waiting");
        assertThat(snapshot.path("result").isNull()).isTrue();
        model.awaitIdle(Duration.ofSeconds(5));
        var questions = get(turn.path() + "/interactions");
        assertThat(questions.isArray()).isTrue();
        assertThat(questions.size()).isOne();
        var question = questions.get(0);
        assertThat(question.path("id").asText()).isNotBlank();
        assertThat(question.path("tool").asText()).isEqualTo("request_city");
        assertThat(question.path("kind").asText()).isEqualTo("input");
        assertThat(question.path("status").asText()).isEqualTo("pending");
        assertThat(question.path("live").asBoolean()).isTrue();
        String command =
                sql.queryForObject(
                        "SELECT id FROM business.commands WHERE turn_id=?",
                        String.class,
                        turn.id());
        String attempt =
                sql.queryForObject(
                        "SELECT attempt_id FROM business.commands WHERE id=?",
                        String.class,
                        command);
        String release =
                sql.queryForObject(
                        "SELECT release_hash FROM business.turns WHERE id=?",
                        String.class,
                        turn.id());
        var binding =
                sql.queryForMap(
                        "SELECT command_id,attempt_id,state_version,release_hash FROM business.interactions WHERE id=?",
                        question.path("id").asText());
        assertThat(binding)
                .containsEntry("command_id", command)
                .containsEntry("attempt_id", attempt)
                .containsEntry("release_hash", release);
        String rootNamespace =
                ExecutionScope.hash(
                        List.of(
                                "financeclaw-state-v1",
                                OWNER.tenant(),
                                OWNER.subject(),
                                published.parentId(),
                                release));
        assertThat(
                        sql.queryForObject(
                                "SELECT version FROM agent_runtime.states WHERE namespace=? AND NOT deleted",
                                Long.class,
                                rootNamespace))
                .isEqualTo(((Number) binding.get("state_version")).longValue());
        String receipt =
                sql.queryForObject(
                        "SELECT result FROM business.capability_steps WHERE turn_id=? AND namespace=? AND step='result'",
                        String.class,
                        turn.id(),
                        "child:" + CALL);
        assertThat(object(receipt).path("answer").asText())
                .isEqualTo(childTools == 0 ? ANSWER : MARKET_ANSWER);
        var paused =
                new Waiting(
                        published,
                        turn,
                        question,
                        command,
                        attempt,
                        release,
                        receipt,
                        stateVersions(),
                        memorySnapshot(turn),
                        childModels,
                        childTools);
        assertThat(paused.sdkRows()).hasSize(2);
        assertNoResumedAttemptOrNetwork(paused);
        assertCompletedChildUnchanged(paused);
        assertNoAssistant(turn);
        return paused;
    }

    /** waiting 不是失败终态，单独等待它；有界时间与本类原终态等待保持相同。 */
    private JsonNode awaitStatus(Turn turn, String expected) throws Exception {
        long deadline = System.nanoTime() + Duration.ofSeconds(25).toNanos();
        JsonNode last;
        do {
            last = get(turn.path());
            if (last.path("status").asText().equals(expected)) {
                waitCommandsFinished();
                return last;
            }
            if (List.of("completed", "failed", "cancelled", "blocked")
                    .contains(last.path("status").asText())) {
                throw new AssertionError(
                        "Expected "
                                + expected
                                + ", observed "
                                + last
                                + "; protocol failures="
                                + model.failures());
            }
            Thread.sleep(100);
        } while (System.nanoTime() < deadline);
        throw new AssertionError(
                "Turn did not reach "
                        + expected
                        + ": "
                        + last
                        + "; protocol failures="
                        + model.failures());
    }

    private void assertWaitingWasNotReplayed(Waiting paused) throws Exception {
        assertThat(get(paused.turn().path()).path("status").asText()).isEqualTo("waiting");
        assertThat(get(paused.turn().path() + "/interactions").get(0)).isEqualTo(paused.question());
        assertNoResumedAttemptOrNetwork(paused);
        assertCompletedChildUnchanged(paused);
        assertThat(stateVersions()).isEqualTo(paused.sdkRows());
        assertThat(memorySnapshot(paused.turn())).isEqualTo(paused.memorySnapshot());
    }

    private JsonNode answer(Waiting paused, int expectedStatus) throws Exception {
        return send(
                "POST",
                paused.answerPath(),
                Map.of(
                        "kind",
                        "input",
                        "revision",
                        paused.question().path("revision").asLong(),
                        "answer",
                        Map.of("city", CITY)),
                expectedStatus);
    }

    private Map<String, Object> memorySnapshot(Turn turn) {
        return sql.queryForMap(
                "SELECT epoch,profile_revision,context FROM memory.turn_snapshots WHERE turn_id=?",
                turn.id());
    }

    /** 回答后仍是原 Command；新 attempt 和 resume_id 必须由回答事务与普通 Worker 产生。 */
    private void assertResumedAttempt(Waiting paused) {
        var command =
                sql.queryForMap(
                        "SELECT id,attempt_id,status,resume_id FROM business.commands WHERE turn_id=?",
                        paused.turn().id());
        assertThat(command)
                .containsEntry("id", paused.commandId())
                .containsEntry("status", "done")
                .containsEntry("resume_id", paused.question().path("id").asText());
        assertThat((String) command.get("attempt_id"))
                .isNotBlank()
                .isNotEqualTo(paused.attemptId());
        assertThat(
                        integer(
                                "SELECT count(*) FROM business.commands WHERE turn_id=?",
                                paused.turn().id()))
                .isOne();
        assertThat(
                        integer(
                                "SELECT count(*) FROM business.attempts WHERE command_id=?",
                                paused.commandId()))
                .isEqualTo(2);
        assertThat(
                        sql.queryForObject(
                                "SELECT outcome FROM business.attempts WHERE id=?",
                                String.class,
                                paused.attemptId()))
                .isEqualTo("waiting");
        assertThat(
                        sql.queryForObject(
                                "SELECT status FROM business.interactions WHERE id=?",
                                String.class,
                                paused.question().path("id").asText()))
                .isEqualTo("answered");
        assertThat(
                        sql.queryForObject(
                                "SELECT attempt_id FROM business.interactions WHERE id=?",
                                String.class,
                                paused.question().path("id").asText()))
                .isEqualTo(paused.attemptId());
        assertThat(
                        sql.queryForObject(
                                "SELECT release_hash FROM business.turns WHERE id=?",
                                String.class,
                                paused.turn().id()))
                .isEqualTo(paused.releaseHash());
    }

    /** 拒答必须没有重新排队/新 attempt/模型网络访问；遗忘后 child 表会被擦除，所以此处只验根事实。 */
    private void assertNoResumedAttemptOrNetwork(Waiting paused) {
        var command =
                sql.queryForMap(
                        "SELECT id,attempt_id,status,resume_id FROM business.commands WHERE turn_id=?",
                        paused.turn().id());
        assertThat(command)
                .containsEntry("id", paused.commandId())
                .containsEntry("attempt_id", paused.attemptId())
                .containsEntry("status", "done")
                .containsEntry("resume_id", null);
        assertThat(
                        integer(
                                "SELECT count(*) FROM business.commands WHERE turn_id=?",
                                paused.turn().id()))
                .isOne();
        assertThat(
                        integer(
                                "SELECT count(*) FROM business.attempts WHERE command_id=?",
                                paused.commandId()))
                .isOne();
        assertThat(
                        integer(
                                "SELECT count(*) FROM business.interactions WHERE id=? AND response_json IS NOT NULL",
                                paused.question().path("id").asText()))
                .isZero();
        assertThat(
                        integer(
                                "SELECT model_calls FROM business.turn_policies WHERE turn_id=?",
                                paused.turn().id()))
                .isEqualTo(2 + paused.childModels());
        assertThat(
                        integer(
                                "SELECT tool_calls FROM business.turn_policies WHERE turn_id=?",
                                paused.turn().id()))
                .isEqualTo(2 + paused.childTools());
        assertThat(
                        sql.queryForObject(
                                "SELECT release_hash FROM business.turns WHERE id=?",
                                String.class,
                                paused.turn().id()))
                .isEqualTo(paused.releaseHash());
        assertThat(model.requests(PrivateAgentLoopback.PARENT)).hasSize(2);
        assertThat(model.requests(PrivateAgentLoopback.LEAF)).hasSize(paused.childModels());
    }

    private void assertCompletedChildUnchanged(Waiting paused) {
        assertThat(
                        integer(
                                "SELECT count(*) FROM business.child_invocations WHERE root_turn_id=?",
                                paused.turn().id()))
                .isOne();
        var row =
                sql.queryForMap(
                        "SELECT call_id,target,target_agent_id,target_release_hash,status,model_calls,tool_calls FROM business.child_invocations WHERE root_turn_id=?",
                        paused.turn().id());
        assertThat(row)
                .containsEntry("call_id", CALL)
                .containsEntry("target", "private_agent")
                .containsEntry("target_agent_id", paused.published().leaf().agentId())
                .containsEntry("target_release_hash", paused.published().leaf().releaseHash())
                .containsEntry("status", "completed")
                .containsEntry("model_calls", paused.childModels())
                .containsEntry("tool_calls", paused.childTools());
        assertThat(
                        integer(
                                "SELECT count(*) FROM business.capability_steps WHERE turn_id=? AND namespace=?",
                                paused.turn().id(),
                                "child:" + CALL))
                .isEqualTo(1 + paused.childTools());
        assertThat(
                        sql.queryForObject(
                                "SELECT result FROM business.capability_steps WHERE turn_id=? AND namespace=? AND step='result'",
                                String.class,
                                paused.turn().id(),
                                "child:" + CALL))
                .isEqualTo(paused.childReceipt());
    }

    private void assertForgottenExecution(Waiting paused) {
        assertThat(
                        integer(
                                "SELECT count(*) FROM business.child_invocations WHERE root_turn_id=?",
                                paused.turn().id()))
                .isZero();
        assertThat(
                        integer(
                                "SELECT count(*) FROM business.capability_steps WHERE turn_id=? AND namespace LIKE 'child:%'",
                                paused.turn().id()))
                .isZero();
        assertThat(
                        integer(
                                "SELECT count(*) FROM memory.turn_snapshots WHERE turn_id=?",
                                paused.turn().id()))
                .isZero();
        assertThat(
                        integer(
                                "SELECT count(*) FROM agent_runtime.work_contexts WHERE turn_id=?",
                                paused.turn().id()))
                .isZero();
        assertThat(
                        sql.queryForObject(
                                "SELECT status FROM memory.sources WHERE id=?",
                                String.class,
                                paused.turn().id()))
                .isEqualTo("forgotten");
        var erased = stateVersions();
        assertThat(erased).hasSize(paused.sdkRows().size());
        for (var before : paused.sdkRows()) {
            var after =
                    erased.stream()
                            .filter(row -> stateKey(row).equals(stateKey(before)))
                            .findFirst()
                            .orElseThrow();
            assertThat(after).containsEntry("deleted", true);
            assertThat(((Number) after.get("version")).longValue())
                    .isEqualTo(((Number) before.get("version")).longValue() + 1);
        }
        assertThat(integer("SELECT count(*) FROM agent_runtime.states WHERE payload IS NOT NULL"))
                .isZero();
        assertThat(integer("SELECT count(*) FROM agent_runtime.unsafe_execution_data()")).isZero();
    }

    private Map<String, Object> stateKey(Map<String, Object> row) {
        return Map.of(
                "namespace",
                row.get("namespace"),
                "user_id",
                row.get("user_id"),
                "session_id",
                row.get("session_id"),
                "state_key",
                row.get("state_key"));
    }

    /** 三份行情使用同一证券、不同价格/来源/采样时间，错误读取父或 global 发布时一定无法通过。 */
    private static Map<String, Object> market(String price, String provider, String asOf) {
        return Map.of("price", price, "currency", "USD", "provider", provider, "as_of", asOf);
    }

    private Published publishMarket(List<String> extraCapabilities) {
        publishMarketBase(PrivateAgentLoopback.LEAF, 32, MARKET_A);
        String leaf = UUID.randomUUID().toString();
        agents.create(
                OWNER,
                leaf,
                "Read-only market leaf",
                LEAF_PROMPT,
                new AgentComposition("primary", List.of("function:market_snapshot")));
        var target =
                new PrivateAgentTarget(
                        leaf,
                        agents.publish(OWNER, leaf, 1, 0).releaseHash(),
                        PrivateAgentTarget.Profile.MARKET_SNAPSHOT);
        publishMarketBase(PrivateAgentLoopback.PARENT, 32, MARKET_P);
        String parent = UUID.randomUUID().toString();
        var requested = new ArrayList<String>();
        requested.add("agent:" + leaf);
        requested.addAll(extraCapabilities);
        agents.create(
                OWNER,
                parent,
                "Market-delegating parent",
                PARENT_PROMPT,
                new AgentComposition("primary", requested));
        String parentHash = agents.publish(OWNER, parent, 1, 0).releaseHash();
        assertThat(releases.resolve(parentHash).definition().business().marketJson())
                .isEqualTo(PayloadJson.canonical(Map.of(SYMBOL, MARKET_P)));
        return new Published(parent, target);
    }

    private void publishMarketBase(String name, int maxModels, Map<String, Object> snapshot) {
        releases.publish(
                new ReleaseDefinition(
                        "private-market-runtime-e2e",
                        "finance_agent",
                        ReleaseDefinition.SCOPES,
                        new ModelProfile(name, model.baseUrl(), 65536, 131072, 8192, 512, false),
                        null,
                        maxModels,
                        64,
                        new BusinessFeatures(
                                List.of(),
                                PackagedSkills.hash(),
                                PayloadJson.canonical(Map.of(SYMBOL, snapshot)),
                                null)));
    }

    private void marketScript(
            Published published,
            String leafAnswer,
            Function<PrivateAgentLoopback.Request, PrivateAgentLoopback.Reply> parentContinuation) {
        marketScript(published, leafAnswer, parentContinuation, null);
    }

    /** 仅 SSE 协议由测试驱动；快照工具真实经 SDK、子调用服务和数据库预算边界执行。 */
    private void marketScript(
            Published published,
            String leafAnswer,
            Function<PrivateAgentLoopback.Request, PrivateAgentLoopback.Reply> parentContinuation,
            AtomicReference<PrivateAgentLoopback.Gate> finalGate) {
        model.reset(
                request -> {
                    if (request.model().equals(PrivateAgentLoopback.LEAF)) {
                        assertMarketSchema(request);
                        if (request.round() == 1) {
                            // 首片有名字/id，后片仅续传 arguments；验证实际 SDK 参数流合并与工具白名单相容。
                            return PrivateAgentLoopback.Reply.fragmentedTool(
                                    SNAPSHOT_CALL, "market_snapshot", Map.of("symbol", SYMBOL));
                        }
                        assertThat(request.round())
                                .as("completed market child cannot call its model again")
                                .isEqualTo(2);
                        assertFrozenSnapshot(
                                request.lastToolResult(SNAPSHOT_CALL),
                                published.leaf().releaseHash());
                        var reply = PrivateAgentLoopback.Reply.text(leafAnswer);
                        return finalGate == null ? reply : reply.held(finalGate.get());
                    }
                    if (request.round() == 1) {
                        return PrivateAgentLoopback.Reply.tool(
                                CALL, published.tool(), Map.of("message", MESSAGE));
                    }
                    return parentContinuation.apply(request);
                });
    }

    private void assertMarketSchema(PrivateAgentLoopback.Request request) {
        assertThat(request.toolNames()).containsExactly("market_snapshot");
        assertThat(request.toolSchema("market_snapshot"))
                .isEqualTo(json.valueToTree(MarketToolSchemas.SNAPSHOT));
    }

    private void assertFrozenSnapshot(String value, String frozenHash) {
        var snapshot = object(value);
        assertThat(snapshot.path("symbol").asText()).isEqualTo(SYMBOL);
        for (String key : List.of("price", "currency", "provider", "as_of")) {
            assertThat(snapshot.path(key).asText())
                    .as("frozen target snapshot field %s", key)
                    .isEqualTo(MARKET_A.get(key));
        }
        assertThat(snapshot.path("source_release_hash").asText()).isEqualTo(frozenHash);
        assertThat(snapshot.path("input_hash").asText())
                .isEqualTo(
                        PayloadDigest.sha256(
                                PayloadJson.canonical(
                                        Map.of(
                                                "symbol",
                                                SYMBOL,
                                                "source_release_hash",
                                                frozenHash))));
        assertThat(snapshot.path("tool_version").asText()).isEqualTo("1.0.0");
        assertThat(value)
                .doesNotContain(
                        (String) MARKET_P.get("provider"), (String) MARKET_B.get("provider"));
    }

    private String marketSnapshotReceipt(Turn turn) {
        return sql.queryForObject(
                "SELECT result FROM business.capability_steps WHERE turn_id=? AND namespace=? AND step=?",
                String.class,
                turn.id(),
                "child:" + CALL,
                "tool:" + SNAPSHOT_CALL);
    }

    private void assertMarketSnapshotReceipt(Turn turn) {
        String frozenHash =
                sql.queryForObject(
                        "SELECT target_release_hash FROM business.child_invocations WHERE root_turn_id=?",
                        String.class,
                        turn.id());
        assertFrozenSnapshot(marketSnapshotReceipt(turn), frozenHash);
        assertThat(
                        integer(
                                "SELECT count(*) FROM business.capability_steps WHERE turn_id=? AND namespace=? AND step LIKE 'tool:%'",
                                turn.id(), "child:" + CALL))
                .isOne();
    }

    private PrivateAgentLoopback.Reply rangeRequest(String callId, String artifact) {
        return PrivateAgentLoopback.Reply.tool(
                callId,
                "read_artifact_range",
                Map.of("artifact_id", artifact, "start", 0, "max_bytes", 512));
    }

    /** 完整正文只能存在子侧与归档文件；父 SDK/work context 仅允许显式读取过的有界头部。 */
    private void assertParentContextHasOnlyReadChunk(
            Turn turn, String head, String tail, String answer) {
        var parentStates =
                sql.queryForList(
                        """
                SELECT s.payload::text FROM agent_runtime.states s JOIN business.turns t ON t.id=?
                WHERE NOT s.deleted
                  AND s.namespace=agent_runtime.scope_hash(ARRAY['financeclaw-state-v1',t.tenant,t.subject,t.agent_id,t.release_hash])
                  AND s.user_id=agent_runtime.scope_hash(ARRAY[t.tenant,t.subject])
                """,
                        String.class,
                        turn.id());
        assertThat(parentStates).hasSize(1);
        parentStates.forEach(
                payload -> assertThat(payload).contains(head).doesNotContain(tail, answer));
        var work =
                sql.queryForList(
                        "SELECT payload::text,transcript::text FROM agent_runtime.work_contexts WHERE turn_id=?",
                        turn.id());
        assertThat(work).hasSize(1);
        for (String column : List.of("payload", "transcript")) {
            assertThat((String) work.getFirst().get(column))
                    .contains(head)
                    .doesNotContain(tail, answer);
        }
    }

    private Published publish(int rootModels, boolean catalog) {
        return publish(rootModels, catalog ? List.of("function:search_catalog") : List.of());
    }

    private Published publish(int rootModels, List<String> extraCapabilities) {
        // Leaf 与 parent 分别捕获自己的无 fallback 模型；发布顺序不让模型选择互相混淆。
        publishBase(PrivateAgentLoopback.LEAF, 32);
        String leaf = UUID.randomUUID().toString();
        agents.create(
                OWNER, leaf, "Empty leaf", LEAF_PROMPT, new AgentComposition("primary", List.of()));
        var target = new PrivateAgentTarget(leaf, agents.publish(OWNER, leaf, 1, 0).releaseHash());
        publishBase(PrivateAgentLoopback.PARENT, rootModels);
        String parent = UUID.randomUUID().toString();
        var requested = new ArrayList<String>();
        requested.add("agent:" + leaf);
        requested.addAll(extraCapabilities);
        agents.create(
                OWNER,
                parent,
                "Native parent",
                PARENT_PROMPT,
                new AgentComposition("primary", requested));
        agents.publish(OWNER, parent, 1, 0);
        return new Published(parent, target);
    }

    private void publishBase(String name, int maxModels) {
        releases.publish(
                new ReleaseDefinition(
                        "private-runtime-e2e",
                        "finance_agent",
                        ReleaseDefinition.SCOPES,
                        new ModelProfile(name, model.baseUrl(), 65536, 131072, 8192, 512, false),
                        null,
                        maxModels,
                        64,
                        new BusinessFeatures(List.of(), PackagedSkills.hash(), "{}", null)));
    }

    private void happyScript(Published published) {
        model.reset(
                request -> {
                    if (request.model().equals(PrivateAgentLoopback.LEAF))
                        return PrivateAgentLoopback.Reply.text(ANSWER);
                    if (request.round() == 1)
                        return PrivateAgentLoopback.Reply.tool(
                                CALL, published.tool(), Map.of("message", MESSAGE));
                    assertThat(request.toolResult(CALL)).contains(ANSWER);
                    return PrivateAgentLoopback.Reply.text(FINAL);
                });
    }

    private PrivateAgentLoopback.Gate heldChildScript(Published published) {
        // reset 必须先于 gate 登记，防止 reset 把本 case 的 gate 提前解除。
        var holder = new AtomicReference<PrivateAgentLoopback.Gate>();
        model.reset(
                request ->
                        request.model().equals(PrivateAgentLoopback.LEAF)
                                ? PrivateAgentLoopback.Reply.text(
                                                "LATE_CHILD_ANSWER_MUST_NOT_COMMIT")
                                        .held(holder.get())
                                : request.round() == 1
                                        ? PrivateAgentLoopback.Reply.tool(
                                                CALL, published.tool(), Map.of("message", MESSAGE))
                                        : PrivateAgentLoopback.Reply.text(
                                                "LATE_PARENT_SUCCESS_MUST_NOT_COMMIT"));
        var gate = model.gate();
        holder.set(gate);
        return gate;
    }

    private String conversation(String parent) throws Exception {
        return send("POST", "/v1/conversations", Map.of("agent_id", parent), 201)
                .path("conversation_id")
                .asText();
    }

    private Turn submit(String parent, String message) throws Exception {
        return submitConversation(conversation(parent), message);
    }

    private Turn submitConversation(String conversation, String message) throws Exception {
        var accepted =
                send(
                        "POST",
                        "/v1/conversations/" + conversation + "/turns",
                        Map.of("message", message),
                        202);
        var turn = new Turn(conversation, accepted.path("turn_id").asText());
        assertThat(turn.id()).isNotBlank();
        admitted.add(turn);
        return turn;
    }

    private void updateProfile(String markdown) throws Exception {
        var before = raw("GET", "/v1/memory/profile.md", null, Map.of(), 200);
        raw(
                "PUT",
                "/v1/memory/profile.md",
                markdown.getBytes(StandardCharsets.UTF_8),
                Map.of(
                        "Content-Type",
                        "text/markdown; charset=utf-8",
                        "If-Match",
                        before.headers().firstValue("ETag").orElseThrow()),
                200);
    }

    private JsonNode get(String path) throws Exception {
        return send("GET", path, null, 200);
    }

    private JsonNode send(String method, String path, Object body, int status) throws Exception {
        return json.readTree(
                raw(
                                method,
                                path,
                                body == null ? null : json.writeValueAsBytes(body),
                                Map.of(
                                        "Content-Type",
                                        "application/json",
                                        "Idempotency-Key",
                                        UUID.randomUUID().toString()),
                                status)
                        .body());
    }

    private HttpResponse<byte[]> raw(
            String method, String path, byte[] body, Map<String, String> headers, int status)
            throws Exception {
        var request =
                HttpRequest.newBuilder(URI.create(api + path))
                        .timeout(Duration.ofSeconds(15))
                        .header("Authorization", "Bearer " + TOKEN);
        headers.forEach(request::header);
        request.method(
                method,
                body == null
                        ? HttpRequest.BodyPublishers.noBody()
                        : HttpRequest.BodyPublishers.ofByteArray(body));
        var response = http.send(request.build(), HttpResponse.BodyHandlers.ofByteArray());
        assertThat(response.statusCode())
                .as("%s %s: %s", method, path, new String(response.body(), StandardCharsets.UTF_8))
                .isEqualTo(status);
        return response;
    }

    private JsonNode object(String value) {
        try {
            return json.readTree(value);
        } catch (Exception failure) {
            throw new AssertionError("Expected tool JSON: " + value, failure);
        }
    }

    private JsonNode awaitTerminal(Turn turn) throws Exception {
        long deadline = System.nanoTime() + Duration.ofSeconds(25).toNanos();
        JsonNode last;
        do {
            last = get(turn.path());
            if (List.of("completed", "failed", "cancelled", "blocked")
                    .contains(last.path("status").asText())) {
                waitCommandsFinished();
                return last;
            }
            Thread.sleep(100);
        } while (System.nanoTime() < deadline);
        throw new AssertionError(
                "Turn did not terminate: " + last + "; protocol failures=" + model.failures());
    }

    private void assertCompleted(Turn turn, String answer) throws Exception {
        var snapshot = awaitTerminal(turn);
        assertThat(snapshot.path("status").asText())
                .as("%s; protocol failures=%s", snapshot, model.failures())
                .isEqualTo("completed");
        assertThat(snapshot.path("result").asText()).isEqualTo(answer);
    }

    private void waitCommandsFinished() throws InterruptedException {
        if (sql == null) return;
        long deadline = System.nanoTime() + Duration.ofSeconds(15).toNanos();
        while (integer("SELECT count(*) FROM business.commands WHERE status<>'done'") != 0) {
            if (System.nanoTime() >= deadline)
                throw new AssertionError("Normal Worker still owns an unfinished command");
            Thread.sleep(50);
        }
    }

    private int integer(String query, Object... parameters) {
        return sql.queryForObject(query, Integer.class, parameters);
    }

    private void assertCounters(Turn turn, int rootModels, int rootTools, int leafModels) {
        assertCounters(turn, rootModels, rootTools, leafModels, 0);
    }

    private void assertCounters(
            Turn turn, int rootModels, int rootTools, int leafModels, int leafTools) {
        assertThat(
                        integer(
                                "SELECT model_calls FROM business.turn_policies WHERE turn_id=?",
                                turn.id()))
                .isEqualTo(rootModels);
        assertThat(
                        integer(
                                "SELECT tool_calls FROM business.turn_policies WHERE turn_id=?",
                                turn.id()))
                .isEqualTo(rootTools);
        assertThat(
                        integer(
                                "SELECT model_calls FROM business.child_invocations WHERE root_turn_id=?",
                                turn.id()))
                .isEqualTo(leafModels);
        assertThat(
                        integer(
                                "SELECT tool_calls FROM business.child_invocations WHERE root_turn_id=?",
                                turn.id()))
                .isEqualTo(leafTools);
    }

    private void assertNoAssistant(Turn turn) {
        assertThat(
                        integer(
                                "SELECT count(*) FROM business.journal WHERE turn_id=? AND role='assistant'",
                                turn.id()))
                .isZero();
    }

    private void assertUnfinishedChild(Turn turn) {
        assertThat(
                        integer(
                                "SELECT count(*) FROM business.child_invocations WHERE root_turn_id=? AND status='completed'",
                                turn.id()))
                .isZero();
        assertThat(
                        integer(
                                "SELECT count(*) FROM business.capability_steps WHERE turn_id=? AND namespace=? AND step='result'",
                                turn.id(),
                                "child:" + CALL))
                .isZero();
    }

    private List<Map<String, Object>> stateVersions() {
        return sql.queryForList(
                "SELECT namespace,user_id,session_id,state_key,version,deleted FROM agent_runtime.states ORDER BY namespace,user_id,session_id,state_key");
    }

    private String childNamespace(PrivateAgentTarget target) {
        return ExecutionScope.hash(
                List.of(
                        "financeclaw-state-v1",
                        OWNER.tenant(),
                        OWNER.subject(),
                        target.agentId(),
                        target.releaseHash()));
    }

    private List<Map<String, Object>> childStateVersions(PrivateAgentTarget target) {
        String namespace = childNamespace(target);
        return stateVersions().stream()
                .filter(row -> namespace.equals(row.get("namespace")))
                .toList();
    }

    private void assertLateReplyRejected(Turn turn) {
        assertThat(model.requests(PrivateAgentLoopback.PARENT)).hasSize(1);
        assertThat(model.requests(PrivateAgentLoopback.LEAF)).hasSize(1);
        assertCounters(turn, 2, 1, 1);
        assertUnfinishedChild(turn);
        assertNoAssistant(turn);
        assertThat(
                        integer(
                                "SELECT count(*) FROM agent_runtime.states WHERE payload::text LIKE '%LATE_CHILD_ANSWER_MUST_NOT_COMMIT%' OR payload::text LIKE '%LATE_PARENT_SUCCESS_MUST_NOT_COMMIT%'"))
                .isZero();
        assertThat(
                        integer(
                                "SELECT count(*) FROM agent_runtime.work_contexts WHERE (payload::text || transcript::text) LIKE '%LATE_CHILD_ANSWER_MUST_NOT_COMMIT%' OR (payload::text || transcript::text) LIKE '%LATE_PARENT_SUCCESS_MUST_NOT_COMMIT%'"))
                .isZero();
    }
}
