package com.financeclaw.runtime;

import static org.assertj.core.api.Assertions.*;
import static org.awaitility.Awaitility.await;

import com.financeclaw.api.TaskEvents;
import com.financeclaw.app.agent.AgentService;
import com.financeclaw.app.agent.port.ResourceAdministration;
import com.financeclaw.app.execution.*;
import com.financeclaw.app.governance.GrantService;
import com.financeclaw.app.runtime.*;
import com.financeclaw.app.task.*;
import com.financeclaw.app.task.port.TaskEventAccess;
import com.financeclaw.bootstrap.FinanceClawApplication;
import com.financeclaw.domain.Task.*;
import com.financeclaw.infrastructure.runtime.*;
import java.math.BigDecimal;
import java.nio.file.*;
import java.security.MessageDigest;
import java.time.Duration;
import java.util.*;
import java.util.concurrent.*;
import java.util.jar.*;
import javax.sql.DataSource;
import org.junit.jupiter.api.*;
import org.junit.jupiter.api.io.TempDir;
import org.springframework.boot.WebApplicationType;
import org.springframework.boot.builder.SpringApplicationBuilder;
import org.springframework.context.ConfigurableApplicationContext;
import org.springframework.http.codec.ServerSentEvent;
import org.springframework.jdbc.core.JdbcTemplate;
import org.testcontainers.containers.PostgreSQLContainer;

/** 两个独立Spring上下文/事件总线共享真实PostgreSQL；不运行模型或自动worker。 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class RuntimeCoordinationIT {
    private final PostgreSQLContainer<?> pg = new PostgreSQLContainer<>("postgres:16.10-alpine");
    private final Owner alice = new Owner("runtime-it", "alice");
    private final Owner publisher = new Owner("runtime-publisher", "owner");
    @TempDir static Path temporary;
    private Path artifacts;
    private ConfigurableApplicationContext first, second;
    private JdbcTemplate sql;
    private SubmitTaskService submission;
    private TaskQueryService reads;

    @BeforeAll
    void start() throws Exception {
        pg.start();
        artifacts = Files.createDirectories(temporary.resolve("runtime")).toRealPath();
        first = startInstance();
        second = startInstance();
        await().atMost(Duration.ofSeconds(10))
                .until(
                        () ->
                                first.getBean(PersistentEventSignals.class).isConnected()
                                        && second.getBean(PersistentEventSignals.class)
                                                .isConnected());
        sql = new JdbcTemplate(first.getBean(DataSource.class));
        submission = first.getBean(SubmitTaskService.class);
        reads = first.getBean(TaskQueryService.class);
        assertThat(first.getBean(TaskEventHub.class))
                .isNotSameAs(second.getBean(TaskEventHub.class));
    }

    private ConfigurableApplicationContext startInstance() {
        return new SpringApplicationBuilder(FinanceClawApplication.class)
                .web(WebApplicationType.NONE)
                .run(
                        "--spring.datasource.url=" + pg.getJdbcUrl(),
                        "--spring.datasource.username=" + pg.getUsername(),
                        "--spring.datasource.password=" + pg.getPassword(),
                        "--spring.config.import=",
                        "--financeclaw.worker.enabled=false",
                        "--financeclaw.scheduler.enabled=false",
                        "--financeclaw.scheduler.dispatch-enabled=false",
                        "--financeclaw.feishu.enabled=false",
                        "--financeclaw.integrations.enabled=false",
                        "--financeclaw.memory.enabled=false",
                        "--financeclaw.business.hotel.enabled=false",
                        "--financeclaw.business.taibu.enabled=false",
                        "--financeclaw.sse.notification-receive-timeout-ms=100",
                        "--financeclaw.sse.notification-reconnect-ms=100",
                        "--financeclaw.sse.heartbeat-ms=30000",
                        "--financeclaw.runtime.artifact-directory=" + artifacts);
    }

    @AfterAll
    void stop() {
        try {
            if (second != null) second.close();
        } finally {
            try {
                if (first != null) first.close();
            } finally {
                pg.stop();
            }
        }
    }

    @BeforeEach
    void clear() {
        sql.execute(
                "TRUNCATE business.conversations,memory.owners,business.runtime_workers,business.runtime_artifacts CASCADE");
    }

    private Turn admit() {
        String conversation = submission.createConversation(alice);
        var accepted =
                submission.submit(
                        alice, conversation, UUID.randomUUID().toString(), "private-input");
        return reads.get(alice, conversation, accepted.turnId());
    }

    private record OpenStream(
            CompletableFuture<List<ServerSentEvent<Event>>> result,
            List<ServerSentEvent<Event>> delivered)
            implements AutoCloseable {
        @Override
        public void close() {
            result.cancel(true);
        }
    }

    private OpenStream open(Turn turn) throws Exception {
        var firstFrame = new CountDownLatch(1);
        var delivered = new CopyOnWriteArrayList<ServerSentEvent<Event>>();
        var future =
                second.getBean(TaskEvents.class)
                        .open(alice, turn.conversationId(), turn.id(), 0)
                        .filter(f -> f.data() != null)
                        .doOnNext(
                                f -> {
                                    delivered.add(f);
                                    firstFrame.countDown();
                                })
                        .collectList()
                        .toFuture();
        if (!firstFrame.await(5, TimeUnit.SECONDS)) {
            future.cancel(true);
            throw new AssertionError("initial replay unavailable");
        }
        return new OpenStream(future, delivered);
    }

    @Test
    void durableEventsCrossInstancesAndReconnectCursorHasNoDuplicates() throws Exception {
        Turn turn = admit();
        try (var stream = open(turn)) {
            first.getBean(CancelTaskService.class)
                    .cancel(alice, turn.conversationId(), turn.id(), "cancel-remote");
            assertThat(stream.result().get(5, TimeUnit.SECONDS))
                    .extracting(ServerSentEvent::id)
                    .containsExactly("1", "2");
        }
        var replay =
                second.getBean(TaskEvents.class)
                        .open(alice, turn.conversationId(), turn.id(), 1)
                        .filter(f -> f.data() != null)
                        .collectList()
                        .block(Duration.ofSeconds(5));
        assertThat(replay)
                .singleElement()
                .satisfies(
                        f -> {
                            assertThat(f.id()).isEqualTo("2");
                            assertThat(f.data().status()).isEqualTo("cancelled");
                        });
        assertThat(
                        second.getBean(TaskEvents.class)
                                .open(alice, turn.conversationId(), turn.id(), 2)
                                .collectList()
                                .block())
                .isEmpty();
    }

    @Test
    void remoteGrantRevocationClosesIdleStreamAndRejectsLocalTail() throws Exception {
        Turn turn = admit();
        var localHub = second.getBean(TaskEventHub.class);
        var claim =
                new Claim(
                        new Command(
                                "test-command",
                                turn.id(),
                                "running",
                                "test-attempt",
                                1,
                                true,
                                null),
                        turn);
        try (var stream = open(turn);
                var progress = localHub.begin(claim)) {
            var grants = first.getBean(GrantService.class);
            var grant = grants.grant(alice, turn.conversationId());
            grants.changeGrant(
                    alice, turn.conversationId(), "revoke-remote", grant.revision(), List.of());
            // Only the first instance receives local invalidation; the second must query authority.
            localHub.progress(claim, "text_delta", "must-not-leak", null);
            assertThatThrownBy(() -> stream.result().get(5, TimeUnit.SECONDS))
                    .hasRootCauseMessage("authorization_changed");
            assertThat(stream.delivered()).allSatisfy(f -> assertThat(f.data().text()).isNull());
        }
    }

    @Test
    void remotePrivacyBarrierClosesIdleStreamBeforeAnyMorePayload() throws Exception {
        Turn turn = admit();
        try (var stream = open(turn)) {
            // A committed purge barrier models another instance starting forget before local
            // notification.
            sql.update(
                    "UPDATE memory.owners SET purge_pending=true WHERE tenant=? AND subject=?",
                    alice.tenant(),
                    alice.subject());
            assertThatThrownBy(() -> stream.result().get(5, TimeUnit.SECONDS))
                    .hasRootCauseMessage("memory_privacy_changed");
            assertThat(stream.delivered()).hasSize(1);
            assertThatThrownBy(() -> second.getBean(TaskEventAccess.class).check(turn))
                    .hasMessage("memory_privacy_changed");
        }
    }

    @Test
    void sharedAgentRevocationDoesNotChangeTaskOwnerAndStopsRemoteStream() throws Exception {
        String agent = UUID.randomUUID().toString();
        var agents = first.getBean(AgentService.class);
        agents.create(publisher, agent, "Shared runtime fixture", "Answer briefly.");
        String release = agents.publish(publisher, agent, 1, 0).releaseHash();
        var grants = first.getBean(ResourceAdministration.class);
        grants.change(publisher, "agent", agent, alice, release, 0, true);
        String conversation = submission.createConversation(alice, agent);
        var accepted =
                submission.submit(alice, conversation, "shared-runtime", "caller-owned-input");
        var turn = reads.get(alice, conversation, accepted.turnId());
        assertThat(turn.subject()).isEqualTo(alice.subject());
        assertThatThrownBy(() -> reads.get(publisher, conversation, turn.id()))
                .hasMessage("not_found");
        try (var stream = open(turn)) {
            grants.change(publisher, "agent", agent, alice, release, 1, false);
            assertThatThrownBy(() -> stream.result().get(5, TimeUnit.SECONDS))
                    .hasRootCauseMessage("agent_access_denied");
            assertThat(stream.delivered()).hasSize(1);
        }
    }

    @Test
    void workerExpiryProtocolAndStopCannotResurrectRegistrationOrAlterCommandAuthority() {
        var registry = first.getBean(WorkerRegistry.class);
        String worker = UUID.randomUUID().toString();
        registry.register(worker, 2);
        assertThat(registry.heartbeat(worker, 1)).isTrue();
        assertThat(registry.requireEligible(worker, "agentscope").activeAttempts()).isOne();
        sql.update(
                "UPDATE business.runtime_workers SET protocol_version=2 WHERE worker_id=?", worker);
        assertThatThrownBy(() -> registry.requireEligible(worker, "agentscope"))
                .hasMessage("worker_unavailable");
        assertThat(registry.heartbeat(worker, 0)).isFalse();
        sql.update(
                "UPDATE business.runtime_workers SET protocol_version=1,expires_at=clock_timestamp()-interval '1 second' WHERE worker_id=?",
                worker);
        assertThat(registry.heartbeat(worker, 0)).isFalse();
        assertThatThrownBy(() -> registry.register(worker, 2)).hasMessage("worker_unavailable");
        String live = UUID.randomUUID().toString();
        registry.register(live, 1);
        Turn turn = admit();
        var claim = first.getBean(ExecutionLeaseService.class).claim(live, 60000);
        assertThat(claim.turn().id()).isEqualTo(turn.id());
        registry.stop(live);
        assertThat(registry.heartbeat(live, 0)).isFalse();
        assertThat(
                        sql.queryForObject(
                                "SELECT status FROM business.commands WHERE id=?",
                                String.class,
                                claim.command().id()))
                .isEqualTo("running");
        sql.update(
                "UPDATE business.commands SET lease_until=clock_timestamp()-interval '1 second' WHERE id=?",
                claim.command().id());
        assertThat(second.getBean(RecoverTaskService.class).recoverExpired()).isOne();
        assertThat(reads.get(alice, turn.conversationId(), turn.id()).status())
                .isEqualTo("blocked");
        assertThat(
                        sql.queryForObject(
                                "SELECT count(*) FROM business.attempts WHERE command_id=?",
                                Integer.class,
                                claim.command().id()))
                .isOne();
    }

    @Test
    void workerRenewalRechecksExpiryAfterWaitingForRowLock() throws Exception {
        var gateway = new JdbcWorkerRegistry(first.getBean(DataSource.class));
        String id = UUID.randomUUID().toString();
        gateway.register(id, "a".repeat(64), 1, 2000);
        try (var holder = first.getBean(DataSource.class).getConnection();
                var pool = Executors.newSingleThreadExecutor()) {
            holder.setAutoCommit(false);
            try (var statement =
                    holder.prepareStatement(
                            "SELECT worker_id FROM business.runtime_workers WHERE worker_id=? FOR UPDATE")) {
                statement.setString(1, id);
                statement.executeQuery().close();
            }
            var waiting = pool.submit(() -> gateway.heartbeat(id, "a".repeat(64), 0, 15000));
            try {
                await().atMost(Duration.ofSeconds(3))
                        .until(
                                () ->
                                        sql.queryForObject(
                                                        "SELECT count(*) FROM pg_stat_activity WHERE wait_event_type='Lock' AND query LIKE '%UPDATE business.runtime_workers w SET%'",
                                                        Integer.class)
                                                > 0);
                await().atMost(Duration.ofSeconds(4)).until(() -> !gateway.get(id).live());
            } finally {
                holder.commit();
            }
            assertThat(waiting.get(3, TimeUnit.SECONDS)).isFalse();
            assertThat(gateway.get(id).live()).isFalse();
        }
    }

    @Test
    void controlledHistoricalArtifactRegistrationVerifiesBytesWithoutExecutingAnything()
            throws Exception {
        Path archive = jar("history.jar", "class-version-one");
        String digest = digest(archive);
        var catalog = first.getBean(RuntimeArtifactCatalog.class);
        var registered = catalog.register("history.jar", digest, 1, List.of("agentscope"));
        assertThat(registered.runtimeHash()).matches("[a-f0-9]{64}");
        assertThat(catalog.register("history.jar", digest, 1, List.of("agentscope")))
                .isEqualTo(registered);
        assertThat(second.getBean(RuntimeArtifactCatalog.class).verify(digest))
                .isEqualTo(registered);
        assertThat(catalog.list(50)).containsExactly(registered);
        assertThatThrownBy(
                        () ->
                                sql.update(
                                        "UPDATE business.runtime_artifacts SET relative_path='changed.jar' WHERE artifact_hash=?",
                                        digest))
                .isInstanceOf(org.springframework.dao.DataAccessException.class);
        assertThatThrownBy(
                        () ->
                                sql.update(
                                        "DELETE FROM business.runtime_artifacts WHERE artifact_hash=?",
                                        digest))
                .isInstanceOf(org.springframework.dao.DataAccessException.class);
        Files.copy(archive, artifacts.resolve("alias.jar"), StandardCopyOption.REPLACE_EXISTING);
        assertThatThrownBy(() -> catalog.register("alias.jar", digest, 1, List.of("agentscope")))
                .hasMessage("runtime_artifact_registration_conflict");
        assertThatThrownBy(
                        () -> catalog.register("../history.jar", digest, 1, List.of("agentscope")))
                .hasMessage("invalid_artifact_path");
        assertThatThrownBy(
                        () ->
                                catalog.register(
                                        archive.toString(), digest, 1, List.of("agentscope")))
                .hasMessage("invalid_artifact_path");
        Path symlink = artifacts.resolve("linked.jar");
        Files.deleteIfExists(symlink);
        Files.createSymbolicLink(symlink, archive);
        assertThatThrownBy(() -> catalog.register("linked.jar", digest, 1, List.of("agentscope")))
                .hasMessage("artifact_symlink");
        assertThatThrownBy(() -> catalog.register("history.jar", digest, 2, List.of("agentscope")))
                .hasMessage("unsupported_runtime_contract");
        jar("history.jar", "class-version-two");
        assertThatThrownBy(() -> catalog.verify(digest))
                .hasMessage("runtime_artifact_hash_mismatch");
    }

    @Test
    void usageReportsCommittedBudgetCountsWithExplicitCostBasis() {
        Turn turn = admit();
        sql.update(
                "UPDATE business.turn_policies SET model_calls=3,tool_calls=2 WHERE turn_id=?",
                turn.id());
        var usage =
                new JdbcRuntimeUsage(
                                first.getBean(DataSource.class),
                                new BigDecimal("0.02"),
                                new BigDecimal("0.05"))
                        .snapshot();
        assertThat(usage.modelCalls()).isEqualTo(3);
        assertThat(usage.toolCalls()).isEqualTo(2);
        assertThat(usage.configuredCallCostEstimate()).isEqualByComparingTo("0.16");
        assertThat(usage.costBasis()).contains("not-provider-billing");
        assertThat(
                        new JdbcRuntimeUsage(
                                        first.getBean(DataSource.class),
                                        BigDecimal.ZERO,
                                        BigDecimal.ZERO)
                                .snapshot()
                                .configuredCallCostEstimate())
                .isNull();
    }

    @Test
    void actualAgentModelAndGovernedToolProduceRootedObservationsAcrossThreads() {
        Turn turn = admit();
        var claim =
                first.getBean(ExecutionLeaseService.class)
                        .claim(UUID.randomUUID().toString(), 60000);
        var registry = io.micrometer.observation.ObservationRegistry.create();
        var stopped = new CopyOnWriteArrayList<io.micrometer.observation.Observation.Context>();
        registry.observationConfig()
                .observationHandler(
                        new io.micrometer.observation.ObservationHandler<
                                io.micrometer.observation.Observation.Context>() {
                            @Override
                            public boolean supportsContext(
                                    io.micrometer.observation.Observation.Context c) {
                                return true;
                            }

                            @Override
                            public void onStop(io.micrometer.observation.Observation.Context c) {
                                stopped.add(c);
                            }
                        });
        var observations = new RuntimeObservations(registry);
        var nativeRuntime =
                new com.financeclaw.infrastructure.agentscope.execution.TaskAgentRuntime(
                        first.getBean(ExecutionPolicyService.class),
                        first.getBean(ExecutionLeaseService.class),
                        first.getBean(com.financeclaw.app.release.ReleaseService.class),
                        first.getBean(com.financeclaw.app.release.port.Credentials.class),
                        first.getBean(
                                com.financeclaw.app.execution.port.ExecutionContextGateway.class),
                        first.getBean(com.financeclaw.app.artifact.ArtifactService.class),
                        first.getBean(com.financeclaw.app.report.ReportService.class),
                        first.getBean(com.financeclaw.infrastructure.state.RuntimeMapper.class),
                        first.getBean(
                                com.financeclaw.infrastructure.agentscope.capability
                                        .BusinessCapabilities.class),
                        15000) {
                    @Override
                    protected io.agentscope.core.model.Model model(Claim invocation) {
                        var count = new java.util.concurrent.atomic.AtomicInteger();
                        return new io.agentscope.core.model.Model() {
                            public String getModelName() {
                                return "observation-fixture";
                            }

                            public reactor.core.publisher.Flux<
                                            io.agentscope.core.model.ChatResponse>
                                    stream(
                                            List<io.agentscope.core.message.Msg> messages,
                                            List<io.agentscope.core.model.ToolSchema> tools,
                                            io.agentscope.core.model.GenerateOptions options) {
                                return reactor.core.publisher.Flux.defer(
                                                () -> {
                                                    List<io.agentscope.core.message.ContentBlock>
                                                            content =
                                                                    count.getAndIncrement() == 0
                                                                            ? List.of(
                                                                                    io.agentscope
                                                                                            .core
                                                                                            .message
                                                                                            .ToolUseBlock
                                                                                            .builder()
                                                                                            .id(
                                                                                                    "private-call-id")
                                                                                            .name(
                                                                                                    "search_catalog")
                                                                                            .input(
                                                                                                    Map
                                                                                                            .of(
                                                                                                                    "city",
                                                                                                                    "private-city"))
                                                                                            .content(
                                                                                                    "{\"city\":\"private-city\"}")
                                                                                            .build())
                                                                            : List.of(
                                                                                    io.agentscope
                                                                                            .core
                                                                                            .message
                                                                                            .TextBlock
                                                                                            .builder()
                                                                                            .text(
                                                                                                    "done")
                                                                                            .build());
                                                    return reactor.core.publisher.Flux.just(
                                                            io.agentscope.core.model.ChatResponse
                                                                    .builder()
                                                                    .content(content)
                                                                    .build());
                                                })
                                        .subscribeOn(reactor.core.scheduler.Schedulers.parallel());
                            }
                        };
                    }
                };
        nativeRuntime.configureObservations(observations);
        nativeRuntime.configureAgentAccess(
                first.getBean(com.financeclaw.app.agent.port.AgentAccessGateway.class));
        var metrics = new io.micrometer.core.instrument.simple.SimpleMeterRegistry();
        try {
            var outcome =
                    new ObservedAgentExecution(nativeRuntime, metrics, observations).execute(claim);
            assertThat(outcome.status()).isEqualTo("completed");
            var root =
                    stopped.stream()
                            .filter(
                                    c ->
                                            "attempt"
                                                    .equals(
                                                            c.getLowCardinalityKeyValue("kind")
                                                                    .getValue()))
                            .findFirst()
                            .orElseThrow();
            assertThat(
                            stopped.stream()
                                    .filter(
                                            c ->
                                                    "model"
                                                            .equals(
                                                                    c.getLowCardinalityKeyValue(
                                                                                    "kind")
                                                                            .getValue())))
                    .hasSize(2);
            assertThat(
                            stopped.stream()
                                    .filter(
                                            c ->
                                                    "tool"
                                                            .equals(
                                                                    c.getLowCardinalityKeyValue(
                                                                                    "kind")
                                                                            .getValue())))
                    .hasSize(1);
            assertThat(stopped)
                    .allSatisfy(
                            c -> {
                                assertThat(c.getHighCardinalityKeyValues()).isEmpty();
                                assertThat(c.getError()).isNull();
                                assertThat(c.getLowCardinalityKeyValues())
                                        .extracting(v -> v.getKey())
                                        .containsExactlyInAnyOrder("kind", "outcome");
                                if (c != root)
                                    assertThat(c.getParentObservation().getContextView())
                                            .isSameAs(root);
                            });
            assertThat(
                            sql.queryForObject(
                                    "SELECT tool_calls FROM business.turn_policies WHERE turn_id=?",
                                    Integer.class,
                                    turn.id()))
                    .isOne();
        } finally {
            metrics.close();
        }
    }

    private Path jar(String name, String body) throws Exception {
        Path target = artifacts.resolve(name);
        try (var jar = new JarOutputStream(Files.newOutputStream(target))) {
            jar.putNextEntry(
                    new JarEntry(
                            "com/financeclaw/infrastructure/agentscope/RuntimeArtifacts.class"));
            jar.write(body.getBytes(java.nio.charset.StandardCharsets.UTF_8));
            jar.closeEntry();
        }
        return target;
    }

    private String digest(Path file) throws Exception {
        return HexFormat.of()
                .formatHex(MessageDigest.getInstance("SHA-256").digest(Files.readAllBytes(file)));
    }
}
