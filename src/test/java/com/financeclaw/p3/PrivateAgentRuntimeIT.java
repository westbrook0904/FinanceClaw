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
import com.financeclaw.infrastructure.agentscope.skills.PackagedSkills;
import com.financeclaw.support.PayloadDigest;
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

    @BeforeAll
    void startApplication() throws Exception {
        try {
            postgres.start();
            model = new PrivateAgentLoopback();
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
            http =
                    HttpClient.newBuilder()
                            .version(HttpClient.Version.HTTP_1_1)
                            .connectTimeout(Duration.ofSeconds(5))
                            .build();
            sql = new JdbcTemplate(application.getBean(DataSource.class));
            agents = application.getBean(AgentService.class);
            releases = application.getBean(ReleaseService.class);
            assertThat(application.getBean(TaskWorker.class)).isNotNull();
        } catch (Exception | Error failure) {
            stopApplication();
            throw failure;
        }
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
            String status = get(turn.path()).path("status").asText();
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

    private Published publish(int rootModels, boolean catalog) {
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
        if (catalog) requested.add("function:search_catalog");
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
                .isZero();
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

    private List<Map<String, Object>> childStateVersions(PrivateAgentTarget target) {
        String namespace =
                ExecutionScope.hash(
                        List.of(
                                "financeclaw-state-v1",
                                OWNER.tenant(),
                                OWNER.subject(),
                                target.agentId(),
                                target.releaseHash()));
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
