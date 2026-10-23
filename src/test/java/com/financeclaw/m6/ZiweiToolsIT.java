package com.financeclaw.m6;

import static org.assertj.core.api.Assertions.*;

import com.financeclaw.app.memory.MemoryService;
import com.financeclaw.app.release.ReleaseService;
import com.financeclaw.app.ziwei.ZiweiService;
import com.financeclaw.bootstrap.FinanceClawApplication;
import com.financeclaw.domain.Governance.*;
import com.financeclaw.domain.Task.*;
import com.financeclaw.domain.release.BusinessFeatures;
import com.financeclaw.domain.release.ReleaseDefinition;
import com.financeclaw.domain.release.ZiweiPolicy;
import com.financeclaw.fixture.GovernanceFixture;
import com.financeclaw.fixture.TaskFixture;
import com.financeclaw.infrastructure.agentscope.execution.*;
import com.financeclaw.infrastructure.agentscope.skills.PackagedSkills;
import com.financeclaw.infrastructure.state.*;
import com.financeclaw.infrastructure.ziwei.ZiweiSchemas;
import com.financeclaw.support.PayloadJson;
import com.financeclaw.tools.*;
import com.financeclaw.ziwei.*;
import io.agentscope.core.message.*;
import io.agentscope.core.model.*;
import io.agentscope.core.tool.*;
import java.time.*;
import java.util.*;
import java.util.concurrent.*;
import javax.sql.DataSource;
import org.junit.jupiter.api.*;
import org.junit.jupiter.params.ParameterizedTest;
import org.junit.jupiter.params.provider.EnumSource;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.boot.test.context.SpringBootTest;
import org.springframework.context.annotation.Import;
import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.test.context.*;
import org.testcontainers.containers.PostgreSQLContainer;
import reactor.core.publisher.Flux;

@Import({TaskFixture.class, GovernanceFixture.class})
@SpringBootTest(
        classes = FinanceClawApplication.class,
        properties = {
            "financeclaw.worker.enabled=false", "financeclaw.integrations.enabled=false",
            "financeclaw.memory.enabled=true", "financeclaw.business.ziwei.enabled=true",
            "financeclaw.business.ziwei.development=true",
                    "financeclaw.business.ziwei.key-version=test-v1",
            "ZIWEI_HMAC_KEY=synthetic-m6c-key-only-01234567890123456789"
        })
class ZiweiToolsIT {
    static final PostgreSQLContainer<?> PG =
            new PostgreSQLContainer<>("postgres:16.10-alpine")
                    .withDatabaseName("m6c")
                    .withUsername("m6c")
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
    @Autowired org.springframework.context.ApplicationContext taskContext;
    @Autowired GovernanceFixture governance;
    @Autowired MemoryService memory;
    @Autowired ReleaseService releases;
    @Autowired RuntimeMapper states;
    @Autowired DataSource source;
    @Autowired org.springframework.core.env.ConfigurableEnvironment environment;
    JdbcTemplate sql;
    final Owner alice = new Owner("local", "alice"), bob = new Owner("local", "bob");

    @BeforeEach
    void reset() {
        sql = new JdbcTemplate(source);
        sql.execute("TRUNCATE business.conversations CASCADE");
        sql.execute("TRUNCATE memory.owners,memory.jobs,memory.operations,memory.audit CASCADE");
        publish(ZiweiDomainTest.POLICY);
    }

    void publish(ZiweiPolicy policy) {
        releases.publish(
                new ReleaseDefinition(
                        "m6c-tests",
                        "finance_agent",
                        ReleaseDefinition.SCOPES,
                        ReleaseDefinition.OFFLINE,
                        null,
                        12,
                        32,
                        new BusinessFeatures(List.of(), PackagedSkills.hash(), "{}", policy)));
    }

    @Test
    void reducedGrantDoesNotRegisterZiweiAndDirectAdapterAlsoDenies() {
        String conversation = tasks.createConversation(alice);
        governance.changeGrant(
                alice,
                conversation,
                "limited",
                governance.grant(alice, conversation).revision(),
                List.of("artifact:read"));
        tasks.submit(alice, conversation, "limited", "read");
        var claim = claim();
        try (var tools = tools(claim)) {
            assertThat(tools.toolkit.getToolNames()).noneMatch(ZiweiSchemas::supports);
            assertThatThrownBy(
                            () ->
                                    taskContext
                                            .getBean(ZiweiService.class)
                                            .run(
                                                    claim,
                                                    "ziwei_natal_chart",
                                                    "direct",
                                                    ZiweiDomainTest.input()))
                    .hasMessage("ziwei_denied");
        }
    }

    @Test
    void archiveAndResultStepRollbackTogetherWhileKeyBindingSurvives() {
        var accepted = submit(alice);
        var claim = claim();
        var db =
                taskContext.getBean(
                        com.financeclaw.infrastructure.governance.GovernanceMapper.class);
        var fault =
                new ZiweiService(
                        taskContext.getBean(
                                com.financeclaw.app.execution.ExecutionPolicyService.class),
                        taskContext.getBean(
                                com.financeclaw.app.execution.CapabilityStepService.class),
                        taskContext.getBean(com.financeclaw.app.ziwei.ZiweiInputService.class),
                        taskContext.getBean(
                                com.financeclaw.app.ziwei.port.ZiweiConfiguration.class),
                        (current, artifact) -> {
                            assertThat(
                                            org.springframework.transaction.support
                                                    .TransactionSynchronizationManager
                                                    .isActualTransactionActive())
                                    .isTrue();
                            governance.archive(current, artifact, "ziwei:read");
                            throw new IllegalStateException("after_ziwei_archive");
                        });
        assertThatThrownBy(
                        () ->
                                fault.run(
                                        claim,
                                        "ziwei_natal_chart",
                                        "atomic",
                                        ZiweiDomainTest.input()))
                .hasMessage("after_ziwei_archive");
        assertThat(
                        sql.queryForObject(
                                "SELECT count(*) FROM business.artifacts WHERE turn_id=?",
                                Integer.class,
                                accepted.turnId()))
                .isZero();
        assertThat(db.getCapabilityStep(accepted.turnId(), "ziwei:atomic", "result")).isNull();
        assertThat(db.getCapabilityStep(accepted.turnId(), "ziwei:key", "binding")).isNotNull();
        var service = taskContext.getBean(ZiweiService.class);
        var first = service.run(claim, "ziwei_natal_chart", "atomic", ZiweiDomainTest.input());
        assertThat(service.run(claim, "ziwei_natal_chart", "atomic", ZiweiDomainTest.input()))
                .isEqualTo(first);
        assertThat(
                        sql.queryForObject(
                                "SELECT count(*) FROM business.artifacts WHERE turn_id=?",
                                Integer.class,
                                accepted.turnId()))
                .isEqualTo(1);
    }

    @Test
    void persistedCurrentResultStepReplaysWithoutCalculationAndStillChecksAuthorization()
            throws Exception {
        var input = ZiweiDomainTest.input();
        var owner = new Owner("r5b-tenant", "r5b-owner");
        var policy = ZiweiPolicy.development("ZIWEI_HMAC_KEY", "r5b-v1", "Asia/Shanghai");
        publish(policy);
        var accepted = submit(owner);
        var claim = claim();
        var key = "synthetic-r5b-key-only-01234567890123456789";
        environment
                .getPropertySources()
                .addFirst(
                        new org.springframework.core.env.MapPropertySource(
                                "r5b-key", Map.of("ZIWEI_HMAC_KEY", key)));
        try {
            var db =
                    taskContext.getBean(
                            com.financeclaw.infrastructure.governance.GovernanceMapper.class);
            String binding =
                    com.financeclaw.app.ziwei.ZiweiIdentity.binding(
                            ZiweiPolicy.key(key), policy, accepted.turnId());
            db.insertCapabilityStep(
                    accepted.turnId(),
                    "ziwei:key",
                    "binding",
                    com.financeclaw.support.PayloadDigest.sha256(PayloadJson.canonical(binding)),
                    "{\"bound\":true}");
            String stored = "{\"saved_receipt\":true,\"status\":\"success\"}";
            db.insertCapabilityStep(
                    accepted.turnId(),
                    "ziwei:saved",
                    "result",
                    com.financeclaw.support.PayloadDigest.sha256(
                            PayloadJson.canonical(
                                    List.of(
                                            "ziwei_natal_chart",
                                            input,
                                            com.financeclaw.app.ziwei.ZiweiIdentity.fingerprint(
                                                    new com.financeclaw.infrastructure.agentscope
                                                                    .capability
                                                                    .AgentScopeZiweiInputs()
                                                            .inspect(
                                                                    "ziwei_natal_chart",
                                                                    input,
                                                                    new com.financeclaw.ziwei
                                                                            .ZiweiDates.QueryClock(
                                                                            java.time.Instant.parse(
                                                                                    "2025-12-31T16:00:00Z"),
                                                                            "Asia/Shanghai"))
                                                            .value(),
                                                    owner,
                                                    policy,
                                                    ZiweiPolicy.key(key))))),
                    stored);
            var service =
                    new ZiweiService(
                            taskContext.getBean(
                                    com.financeclaw.app.execution.ExecutionPolicyService.class),
                            taskContext.getBean(
                                    com.financeclaw.app.execution.CapabilityStepService.class),
                            taskContext.getBean(com.financeclaw.app.ziwei.ZiweiInputService.class),
                            taskContext.getBean(
                                    com.financeclaw.app.ziwei.port.ZiweiConfiguration.class),
                            (current, artifact) -> {
                                throw new AssertionError("persisted receipt must not republish");
                            });
            assertThat(service.run(claim, "ziwei_natal_chart", "saved", input).text())
                    .isEqualTo(stored);
            var changed = new TreeMap<>(input);
            changed.put("focus", "career");
            assertThatThrownBy(() -> service.run(claim, "ziwei_natal_chart", "saved", changed))
                    .hasMessage("step_input_conflict");
            var grant = governance.grant(owner, accepted.conversationId());
            governance.changeGrant(
                    owner,
                    accepted.conversationId(),
                    "r5b-revoke",
                    grant.revision(),
                    List.of("artifact:read"));
            assertThatThrownBy(() -> service.run(claim, "ziwei_natal_chart", "saved", input))
                    .hasMessage("authorization_changed");
            assertThat(
                            sql.queryForObject(
                                    "SELECT count(*) FROM business.artifacts WHERE turn_id=?",
                                    Integer.class,
                                    accepted.turnId()))
                    .isZero();
        } finally {
            environment.getPropertySources().remove("r5b-key");
        }
    }

    @Test
    void projectionBudgetFailureLeavesNoResultReceiptOrArtifact() {
        var p = ZiweiDomainTest.POLICY;
        publish(
                new ZiweiPolicy(
                        p.convention(),
                        p.engine(),
                        p.calendar(),
                        p.tzdb(),
                        p.schemaHash(),
                        p.keyVersion(),
                        p.keyRef(),
                        p.queryTimezone(),
                        32,
                        1024,
                        2097152));
        submit(alice);
        var claim = claim();
        try (var tools = tools(claim)) {
            assertThat(call(tools, "budget", "ziwei_natal_chart", ZiweiDomainTest.input()))
                    .containsEntry("code", "ZIWEI_CONTEXT_BUDGET_EXCEEDED");
            assertThat(sql.queryForObject("SELECT count(*) FROM business.artifacts", Integer.class))
                    .isZero();
            assertThat(
                            taskContext
                                    .getBean(
                                            com.financeclaw.infrastructure.governance
                                                    .GovernanceMapper.class)
                                    .getCapabilityStep(claim.turn().id(), "ziwei:budget", "result"))
                    .isNull();
        }
    }

    @Test
    void keyDriftCannotReplayAnExistingResultUnderTheSameRelease() {
        submit(alice);
        var claim = claim();
        try (var tools = tools(claim)) {
            call(tools, "key", "ziwei_natal_chart", ZiweiDomainTest.input());
            environment
                    .getPropertySources()
                    .addFirst(
                            new org.springframework.core.env.MapPropertySource(
                                    "m6c-key-drift",
                                    Map.of("ZIWEI_HMAC_KEY", ZiweiDomainTest.KEY + "rotated")));
            try {
                assertThatThrownBy(
                                () ->
                                        call(
                                                tools,
                                                "key",
                                                "ziwei_natal_chart",
                                                ZiweiDomainTest.input()))
                        .hasMessage("step_input_conflict");
                assertThat(
                                sql.queryForObject(
                                        "SELECT count(*) FROM business.artifacts", Integer.class))
                        .isEqualTo(1);
            } finally {
                environment.getPropertySources().remove("m6c-key-drift");
            }
        }
    }

    @Test
    void concurrentDuplicateCallCommitsOneArchive() throws Exception {
        submit(alice);
        var claim = claim();
        try (var tools = tools(claim);
                var executor = Executors.newVirtualThreadPerTaskExecutor()) {
            var a =
                    executor.submit(
                            () ->
                                    call(
                                            tools,
                                            "duplicate",
                                            "ziwei_natal_chart",
                                            ZiweiDomainTest.input()));
            var b =
                    executor.submit(
                            () ->
                                    call(
                                            tools,
                                            "duplicate",
                                            "ziwei_natal_chart",
                                            ZiweiDomainTest.input()));
            assertThat(a.get(30, TimeUnit.SECONDS)).isEqualTo(b.get(30, TimeUnit.SECONDS));
            assertThat(sql.queryForObject("SELECT count(*) FROM business.artifacts", Integer.class))
                    .isEqualTo(1);
        }
    }

    @Test
    void artifactReadAndExecutionUseCompatiblePrivacyGrantLockOrder() throws Exception {
        submit(alice);
        var claim = claim();
        try (var tools = tools(claim);
                var executor = Executors.newVirtualThreadPerTaskExecutor()) {
            var response = call(tools, "locks", "ziwei_natal_chart", ZiweiDomainTest.input());
            var ownerLocked = new CountDownLatch(1);
            var continueExecution = new CountDownLatch(1);
            var executing =
                    executor.submit(
                            () ->
                                    tasks.fenced(
                                            claim,
                                            () -> {
                                                memory.privacy(alice);
                                                ownerLocked.countDown();
                                                try {
                                                    if (!continueExecution.await(
                                                            10, TimeUnit.SECONDS))
                                                        throw new AssertionError(
                                                                "lock test timed out");
                                                } catch (InterruptedException e) {
                                                    throw new AssertionError(e);
                                                }
                                                return tasks.checkPolicy(claim);
                                            }));
            assertThat(ownerLocked.await(10, TimeUnit.SECONDS)).isTrue();
            var reading =
                    executor.submit(
                            () -> governance.read(alice, artifact(response), 0, 1, "/facts"));
            try {
                long deadline = System.nanoTime() + TimeUnit.SECONDS.toNanos(5);
                boolean waiting = false;
                while (!waiting && System.nanoTime() < deadline) {
                    waiting =
                            Boolean.TRUE.equals(
                                    sql.queryForObject(
                                            "SELECT EXISTS(SELECT 1 FROM pg_stat_activity WHERE datname=current_database() AND wait_event_type='Lock' AND query ILIKE '%memory.owners%')",
                                            Boolean.class));
                    if (!waiting) Thread.sleep(10);
                }
                assertThat(waiting).as("artifact read reaches the held privacy lock").isTrue();
            } finally {
                continueExecution.countDown();
            }
            assertThat(executing.get(10, TimeUnit.SECONDS)).isNotNull();
            assertThat(reading.get(10, TimeUnit.SECONDS).count()).isEqualTo(1);
        }
    }

    Accepted submit(Owner owner) {
        return tasks.submit(
                owner, tasks.createConversation(owner), UUID.randomUUID().toString(), "请查看本次盘面");
    }

    Claim claim() {
        return tasks.claim("m6c-test", 120000);
    }

    GovernedTools tools(Claim claim) {
        var t =
                new GovernedTools(
                        tasks.policy(),
                        tasks.releases(),
                        tasks.credentials(),
                        governance.artifacts(),
                        governance.reports(),
                        claim);
        t.business(
                null,
                taskContext.getBean(
                        com.financeclaw.infrastructure.agentscope.capability.BusinessCapabilities
                                .class));
        return t;
    }

    Map<String, Object> call(
            GovernedTools tools, String id, String name, Map<String, Object> input) {
        var block =
                tools.toolkit
                        .getTool(name)
                        .callAsync(
                                ToolCallParam.builder()
                                        .toolUseBlock(
                                                com.financeclaw.infrastructure.agentscope
                                                        .NativeToolCalls.call(id, name, input))
                                        .input(input)
                                        .build())
                        .block(Duration.ofSeconds(30));
        return PayloadJson.object(((TextBlock) block.getOutput().getFirst()).getText());
    }

    String artifact(Map<String, Object> response) {
        return (String) ((Map<?, ?>) response.get("artifact")).get("artifact_id");
    }

    @ParameterizedTest
    @EnumSource(ZiweiDates.Level.class)
    void allFiveNativeToolsArchiveScopedFactsWithAtomicReplay(ZiweiDates.Level level) {
        var accepted = submit(alice);
        var claim = claim();
        var input = ZiweiDomainTest.input();
        if (level != ZiweiDates.Level.NATAL) input.put("on_date", "2026-02-17");
        try (var tools = tools(claim)) {
            assertThat(tools.toolkit.getTool(level.toolName())).isInstanceOf(ToolBase.class);
            var response = call(tools, "fixed-call", level.toolName(), input);
            assertThat(response).containsEntry("status", "success");
            assertThat(ZiweiCalculation.bytes(response)).isLessThanOrEqualTo(15024);
            assertThat(call(tools, "fixed-call", level.toolName(), input)).isEqualTo(response);
            assertThat(sql.queryForObject("SELECT count(*) FROM business.artifacts", Integer.class))
                    .isEqualTo(1);
            assertThat(governance.read(alice, artifact(response), 0, 1, "/facts").count())
                    .isEqualTo(1);
            assertThatThrownBy(() -> governance.read(bob, artifact(response), 0, 1))
                    .hasMessage("not_found");
            assertThat(
                            taskContext
                                    .getBean(
                                            com.financeclaw.infrastructure.governance
                                                    .GovernanceMapper.class)
                                    .getArtifactScope(artifact(response)))
                    .isEqualTo("ziwei:read");
            var changed = new TreeMap<>(input);
            changed.put("focus", "career");
            assertThatThrownBy(() -> call(tools, "fixed-call", level.toolName(), changed))
                    .hasMessage("step_input_conflict");
            var grant = governance.grant(alice, accepted.conversationId());
            governance.changeGrant(
                    alice,
                    accepted.conversationId(),
                    "revoke",
                    grant.revision(),
                    List.of("artifact:read"));
            assertThatThrownBy(() -> governance.read(alice, artifact(response), 0, 1, "/facts"))
                    .hasMessage("artifact_scope_denied");
            assertThatThrownBy(() -> call(tools, "new-call", level.toolName(), input))
                    .hasMessage("authorization_changed");
        }
    }

    @Test
    void differentOwnersHaveIndependentFingerprintsAndNoCrossOwnerReplay() throws Exception {
        submit(alice);
        var a = claim();
        submit(bob);
        var b = claim();
        try (var executor = Executors.newVirtualThreadPerTaskExecutor()) {
            var fa =
                    executor.submit(
                            () -> {
                                try (var t = tools(a)) {
                                    return call(
                                            t,
                                            "same",
                                            "ziwei_natal_chart",
                                            ZiweiDomainTest.input());
                                }
                            });
            var fb =
                    executor.submit(
                            () -> {
                                try (var t = tools(b)) {
                                    return call(
                                            t,
                                            "same",
                                            "ziwei_natal_chart",
                                            ZiweiDomainTest.input());
                                }
                            });
            var ar = fa.get(30, TimeUnit.SECONDS);
            var br = fb.get(30, TimeUnit.SECONDS);
            assertThat(((Map<?, ?>) ar.get("projection")).get("birth_fingerprint"))
                    .isNotEqualTo(((Map<?, ?>) br.get("projection")).get("birth_fingerprint"));
            assertThat(artifact(ar)).isNotEqualTo(artifact(br));
            assertThatThrownBy(() -> governance.read(bob, artifact(ar), 0, 1, "/facts"))
                    .hasMessage("not_found");
        }
    }

    @Test
    void privacyEpochInvalidatesPriorArtifactsAndPendingExecution() {
        submit(alice);
        var claim = claim();
        try (var tools = tools(claim)) {
            var response = call(tools, "privacy", "ziwei_natal_chart", ZiweiDomainTest.input());
            var before = memory.privacy(alice);
            memory.forget(alice, before.epoch(), "forget");
            assertThatThrownBy(() -> governance.read(alice, artifact(response), 0, 1, "/facts"))
                    .hasMessage("privacy_epoch_changed");
            assertThatThrownBy(
                            () ->
                                    call(
                                            tools,
                                            "privacy",
                                            "ziwei_natal_chart",
                                            ZiweiDomainTest.input()))
                    .hasMessage("memory_privacy_changed");
        }
    }

    @Test
    void invalidRangeAndOversizedProjectionDoNotCreateArtifacts() {
        submit(alice);
        var claim = claim();
        try (var tools = tools(claim)) {
            var input = ZiweiDomainTest.input();
            input.put("date_range", Map.of("start", "2026-01-01", "end", "2026-02-02"));
            assertThat(call(tools, "range", "ziwei_daily_chart", input))
                    .containsEntry("status", "rejected")
                    .containsEntry("code", "ZIWEI_RANGE_LIMIT");
            assertThat(sql.queryForObject("SELECT count(*) FROM business.artifacts", Integer.class))
                    .isZero();
        }
    }

    TaskAgentRuntime runtime(Map<String, Object> input) {
        return new TaskAgentRuntime(
                tasks.policy(),
                tasks.leases(),
                tasks.releases(),
                tasks.credentials(),
                tasks.context(),
                governance.artifacts(),
                governance.reports(),
                states,
                taskContext.getBean(
                        com.financeclaw.infrastructure.agentscope.capability.BusinessCapabilities
                                .class),
                30000) {
            @Override
            protected Model model(Claim c) {
                return new Model() {
                    public String getModelName() {
                        return "m6c-scripted";
                    }

                    public Flux<ChatResponse> stream(
                            List<Msg> messages, List<ToolSchema> tools, GenerateOptions options) {
                        boolean resolved =
                                messages.stream()
                                        .flatMap(
                                                m ->
                                                        m
                                                                .getContentBlocks(
                                                                        ToolResultBlock.class)
                                                                .stream())
                                        .anyMatch(r -> !r.isSuspended());
                        return Flux.just(
                                ChatResponse.builder()
                                        .id("synthetic")
                                        .content(
                                                resolved
                                                        ? List.of(
                                                                TextBlock.builder()
                                                                        .text(
                                                                                "已核对事实与覆盖区间。仅供传统文化参考。")
                                                                        .build())
                                                        : List.of(
                                                                com.financeclaw.infrastructure
                                                                        .agentscope.NativeToolCalls
                                                                        .call(
                                                                                "clarify",
                                                                                "ziwei_yearly_chart",
                                                                                input)))
                                        .build());
                    }
                };
            }
        };
    }

    void run(Claim claim, TaskAgentRuntime runtime) throws Exception {
        try (var worker =
                com.financeclaw.m1.TaskWorkerFixture.create(
                        taskContext, runtime, false, 1, 120000, 2000)) {
            worker.execute(claim);
        }
    }

    @Test
    void nativeSuspensionResumesOnlyCompleteAnswerWithOriginalTurnClockAndRelease()
            throws Exception {
        var accepted = submit(alice);
        sql.update(
                "UPDATE business.turns SET created_at='2025-12-31T16:00:00Z' WHERE id=?",
                accepted.turnId());
        var input = ZiweiDomainTest.input();
        ((Map<?, ?>) input.get("birth")).remove("sex_for_chart");
        input.put("year_offset", 0);
        var first = claim();
        run(first, runtime(input));
        assertThat(tasks.get(alice, accepted.conversationId(), accepted.turnId()).status())
                .isEqualTo("waiting");
        var i =
                governance
                        .interactions(alice, accepted.conversationId(), accepted.turnId())
                        .getFirst();
        assertThat(i.question()).doesNotContain("1990", "上海").contains("排盘性别");
        assertThatThrownBy(
                        () ->
                                governance.answer(
                                        alice,
                                        i.id(),
                                        "incomplete",
                                        new Answer(
                                                "input",
                                                i.revision(),
                                                null,
                                                null,
                                                Map.of("birth", Map.of("time_basis", "civil")))))
                .hasMessage("ziwei_input_incomplete");
        assertThat(
                        taskContext
                                .getBean(
                                        com.financeclaw.infrastructure.governance.GovernanceMapper
                                                .class)
                                .getInteractionById(i.id())
                                .status())
                .isEqualTo("pending");
        var answer =
                new Answer(
                        "input",
                        i.revision(),
                        null,
                        null,
                        Map.of("birth", Map.of("sex_for_chart", "male")));
        String originalRelease = first.turn().releaseHash();
        publish(ZiweiPolicy.development("ZIWEI_HMAC_KEY", "next-release", "America/New_York"));
        assertThat(releases.currentHash()).isNotEqualTo(originalRelease);
        governance.answer(alice, i.id(), "complete", answer);
        assertThat(governance.answer(alice, i.id(), "complete", answer).idempotentReplay())
                .isTrue();
        var resumed = claim();
        assertThat(resumed.turn().releaseHash()).isEqualTo(originalRelease);
        assertThat(resumed.command().attemptId()).isNotEqualTo(first.command().attemptId());
        run(resumed, runtime(input));
        assertThat(tasks.get(alice, accepted.conversationId(), accepted.turnId()).status())
                .isEqualTo("completed");
        String payload =
                sql.queryForObject(
                        "SELECT payload FROM business.artifacts WHERE turn_id=?",
                        String.class,
                        accepted.turnId());
        assertThat(PayloadJson.object(payload).get("target"))
                .isEqualTo(
                        Map.of(
                                "start",
                                "2026-01-01",
                                "end",
                                "2027-01-01",
                                "timezone",
                                "Asia/Shanghai"));
        assertThat(sql.queryForObject("SELECT count(*) FROM business.artifacts", Integer.class))
                .isEqualTo(1);
        assertThat(sql.queryForObject("SELECT count(*) FROM memory.facts", Integer.class)).isZero();
    }
}
