package com.financeclaw.m5;

import static org.assertj.core.api.Assertions.*;

import com.fasterxml.jackson.databind.*;
import com.financeclaw.api.TaskEvents;
import com.financeclaw.app.artifact.ArtifactService;
import com.financeclaw.app.execution.ExecutionLeaseService;
import com.financeclaw.app.execution.port.AgentExecutionPort;
import com.financeclaw.app.governance.GrantService;
import com.financeclaw.app.memory.*;
import com.financeclaw.app.release.ReleaseService;
import com.financeclaw.app.report.ReportService;
import com.financeclaw.bootstrap.FinanceClawApplication;
import com.financeclaw.channel.*;
import com.financeclaw.infrastructure.agentscope.capability.BusinessCapabilities;
import com.financeclaw.infrastructure.agentscope.memory.AgentScopeMemoryModels;
import com.financeclaw.infrastructure.channel.FeishuMapper;
import com.financeclaw.infrastructure.governance.GovernanceMapper;
import com.financeclaw.infrastructure.memory.MemoryMapper;
import com.financeclaw.infrastructure.state.RuntimeMapper;
import com.financeclaw.infrastructure.task.*;
import com.financeclaw.support.PayloadJson;
import com.financeclaw.worker.*;
import com.financeclawmemory.MemoryWorkerApplication;
import com.financeclawsetup.DatabaseInitializationApplication;
import java.net.*;
import java.net.http.*;
import java.nio.file.*;
import java.time.Duration;
import java.util.*;
import javax.sql.DataSource;
import org.apache.ibatis.session.SqlSessionFactory;
import org.flywaydb.core.Flyway;
import org.junit.jupiter.api.*;
import org.junit.jupiter.params.ParameterizedTest;
import org.junit.jupiter.params.provider.ValueSource;
import org.springframework.boot.WebApplicationType;
import org.springframework.boot.builder.SpringApplicationBuilder;
import org.springframework.boot.web.servlet.context.ServletWebServerApplicationContext;
import org.springframework.context.ConfigurableApplicationContext;
import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.jdbc.datasource.DriverManagerDataSource;
import org.springframework.scheduling.annotation.ScheduledAnnotationBeanPostProcessor;
import org.springframework.web.servlet.mvc.method.annotation.RequestMappingHandlerMapping;
import org.testcontainers.containers.PostgreSQLContainer;

/** 真实统一业务上下文及受限记忆、初始化上下文；不使用 mock bean 或额外扫描。 */
class RoleAssemblyIT {
    private static final PostgreSQLContainer<?> PG =
            new PostgreSQLContainer<>("postgres:16.10-alpine")
                    .withDatabaseName("r6")
                    .withUsername("r6")
                    .withPassword("test-only");
    private static final HttpClient HTTP =
            HttpClient.newBuilder().connectTimeout(Duration.ofSeconds(5)).build();
    private static final Map<String, Object> INVENTORY = new TreeMap<>();
    private static final String TOKEN = "r6-local-fixture";
    private static JdbcTemplate sql;

    @BeforeAll
    static void prepareDatabase() throws Exception {
        PG.start();
        sql =
                new JdbcTemplate(
                        new DriverManagerDataSource(
                                PG.getJdbcUrl(), PG.getUsername(), PG.getPassword()));
        try (var migration =
                start(
                        DatabaseInitializationApplication.class,
                        false,
                        Map.of("spring.flyway.enabled", "true"))) {
            assertThat(migration.getBean(Flyway.class).info().current().getVersion().toString())
                    .isEqualTo("4");
        }
        sql.execute("CREATE ROLE financeclaw_memory LOGIN PASSWORD 'test-memory-only'");
        sql.execute(
                Files.readString(Path.of("ops/memory-worker-grants.sql"))
                        .replace("ON DATABASE financeclaw_java", "ON DATABASE r6"));
    }

    @AfterAll
    static void closeDatabase() throws Exception {
        try {
            Files.createDirectories(Path.of("target/r6"));
            Files.writeString(
                    Path.of("target/r6/role-inventory.json"),
                    PayloadJson.canonical(INVENTORY) + "\n");
        } finally {
            PG.stop();
        }
    }

    @BeforeEach
    void clearDatabase() {
        sql.execute("TRUNCATE business.conversations CASCADE");
        sql.execute("TRUNCATE memory.owners,memory.jobs,memory.operations,memory.audit CASCADE");
    }

    private static ConfigurableApplicationContext start(
            Class<?> source, boolean web, Map<String, String> overrides) {
        var properties = new LinkedHashMap<String, String>();
        properties.put("spring.datasource.url", PG.getJdbcUrl());
        properties.put("spring.datasource.username", PG.getUsername());
        properties.put("spring.datasource.password", PG.getPassword());
        properties.put("spring.datasource.hikari.maximum-pool-size", "3");
        properties.put("spring.flyway.enabled", "false");
        properties.put("server.port", "0");
        properties.put("spring.main.banner-mode", "off");
        properties.put("management.endpoint.health.group.readiness.include", "db");
        properties.put("financeclaw.auth.token", TOKEN);
        properties.put("financeclaw.auth.tenant", "local");
        properties.put("financeclaw.auth.subject", "developer");
        properties.put("financeclaw.feishu.enabled", "false");
        properties.put("financeclaw.business.hotel.enabled", "false");
        properties.put("financeclaw.business.taibu.enabled", "false");
        properties.put("financeclaw.business.ziwei.enabled", "false");
        properties.put("financeclaw.model.name", "offline");
        properties.put("financeclaw.fallback.name", "");
        properties.put("financeclaw.memory.enabled", "false");
        properties.putAll(overrides);
        return new SpringApplicationBuilder(source)
                .web(web ? WebApplicationType.SERVLET : WebApplicationType.NONE)
                .run(
                        properties.entrySet().stream()
                                .map(e -> "--" + e.getKey() + "=" + e.getValue())
                                .toArray(String[]::new));
    }

    private static ConfigurableApplicationContext application(
            String profile, boolean web, Map<String, String> overrides) {
        var properties = new HashMap<String, String>();
        if (profile.equals("standalone")) properties.put("spring.profiles.active", "standalone");
        properties.put("management.endpoint.health.group.readiness.include", "db,runtimeStore");
        properties.putAll(overrides);
        return start(FinanceClawApplication.class, web, properties);
    }

    private static Set<String> mappers(ConfigurableApplicationContext context) {
        var factories = context.getBeansOfType(SqlSessionFactory.class).values();
        var result = new TreeSet<String>();
        for (var factory : factories)
            factory.getConfiguration()
                    .getMapperRegistry()
                    .getMappers()
                    .forEach(type -> result.add(type.getSimpleName()));
        return result;
    }

    private static Set<String> controllers(ConfigurableApplicationContext context) {
        var result = new TreeSet<String>();
        context.getBean("requestMappingHandlerMapping", RequestMappingHandlerMapping.class)
                .getHandlerMethods()
                .values()
                .forEach(
                        handler -> {
                            if (handler.getBeanType()
                                    .getPackageName()
                                    .equals("com.financeclaw.api"))
                                result.add(handler.getBeanType().getSimpleName());
                        });
        return result;
    }

    private static void absent(ConfigurableApplicationContext context, Class<?>... types) {
        for (var type : types)
            assertThat(context.getBeanNamesForType(type)).as(type.getName()).isEmpty();
    }

    private static void inventory(String role, ConfigurableApplicationContext context) {
        var beans = new TreeSet<String>();
        for (String name : context.getBeanDefinitionNames()) {
            var type = context.getType(name);
            if (type != null && type.getName().startsWith("com.financeclaw"))
                beans.add(type.getName());
        }
        assertThat(beans)
                .noneMatch(
                        name ->
                                name.startsWith("com.financeclaw.application.")
                                        || name.startsWith("com.financeclaw.fixture."));
        INVENTORY.put(role, Map.of("beans", beans, "mappers", mappers(context)));
    }

    private static HttpResponse<String> request(
            ConfigurableApplicationContext context,
            String path,
            String method,
            String body,
            boolean auth)
            throws Exception {
        int port = ((ServletWebServerApplicationContext) context).getWebServer().getPort();
        var request =
                HttpRequest.newBuilder(URI.create("http://127.0.0.1:" + port + path))
                        .timeout(Duration.ofSeconds(10));
        if (auth) request.header("Authorization", "Bearer " + TOKEN);
        if (body != null)
            request.header("Content-Type", "application/json")
                    .header("Idempotency-Key", "r6-submission");
        return HTTP.send(
                request.method(
                                method,
                                body == null
                                        ? HttpRequest.BodyPublishers.noBody()
                                        : HttpRequest.BodyPublishers.ofString(body))
                        .build(),
                HttpResponse.BodyHandlers.ofString());
    }

    @ParameterizedTest
    @ValueSource(strings = {"default", "standalone"})
    void unifiedApplicationLoadsHttpExecutionDeliveryAndAllRequiredMappers(String profile)
            throws Exception {
        try (var context = application(profile, true, Map.of())) {
            assertThat(controllers(context))
                    .containsExactlyInAnyOrder(
                            "ArtifactContentController",
                            "TaskController",
                            "AgentController",
                            "GovernanceController",
                            "MemoryController",
                            "UserProfileController",
                            "OperationsController",
                            "FeishuController");
            assertThat(mappers(context))
                    .containsExactlyInAnyOrder(
                            "TaskMapper",
                            "GovernanceMapper",
                            "RuntimeMapper",
                            "MemoryMapper",
                            "OperationsMapper",
                            "FeishuMapper",
                            "ReleaseMapper",
                            "AgentMapper");
            assertThat(context.getBean(GrantService.class)).isNotNull();
            assertThat(context.getBean(ArtifactService.class)).isNotNull();
            assertThat(context.getBean(ReportService.class)).isNotNull();
            assertThat(context.getBean(TaskWorker.class)).isNotNull();
            assertThat(context.getBean(AgentExecutionPort.class)).isNotNull();
            assertThat(context.getBean(BusinessCapabilities.class)).isNotNull();
            assertThat(context.getBean(FeishuDelivery.class)).isNotNull();
            assertThat(context.getBean(AgentScopeFeishu.class)).isNotNull();
            assertThat(
                            context.getBean(ScheduledAnnotationBeanPostProcessor.class)
                                    .getScheduledTasks())
                    .extracting(task -> task.getTask().getRunnable().toString())
                    .containsExactlyInAnyOrder(
                            "com.financeclaw.worker.TaskWorker.poll",
                            "com.financeclaw.worker.TaskWorker.recover",
                            "com.financeclaw.worker.TaskWorker.heartbeat",
                            "com.financeclaw.channel.FeishuDelivery.scheduled",
                            "com.financeclaw.channel.FeishuDelivery.scheduledRecovery");
            assertThat(context.containsBean("executionHeartbeatScheduler")).isTrue();
            assertThat(
                            request(context, "/actuator/health/readiness", "GET", null, false)
                                    .statusCode())
                    .isEqualTo(200);
            assertThat(request(context, "/v1/operations", "GET", null, false).statusCode())
                    .isEqualTo(401);
            assertThat(request(context, "/v1/operations", "GET", null, true).statusCode())
                    .isEqualTo(200);
            assertThat(request(context, "/v1/memory/profile.md", "GET", null, false).statusCode())
                    .isEqualTo(401);
            assertThat(request(context, "/v1/memory/profile.md", "GET", null, true).statusCode())
                    .isEqualTo(200);
            inventory(profile, context);
        }
    }

    @Test
    void memoryRoleKeepsOnlyMemoryMapperAndCannotUseBusinessSchemas() throws Exception {
        try (var context =
                start(
                        MemoryWorkerApplication.class,
                        true,
                        Map.of(
                                "spring.datasource.username",
                                "financeclaw_memory",
                                "spring.datasource.password",
                                "test-memory-only",
                                "financeclaw.memory.embedding.revision",
                                "role-fixture",
                                "financeclaw.business.ziwei.enabled",
                                "true",
                                "financeclaw.feishu.enabled",
                                "true",
                                "management.endpoint.health.group.readiness.include",
                                "db,memoryStore"))) {
            assertThat(mappers(context)).containsExactly("MemoryMapper");
            assertThat(controllers(context)).isEmpty();
            absent(
                    context,
                    GrantService.class,
                    ArtifactService.class,
                    ReportService.class,
                    ReleaseService.class,
                    TaskWorker.class,
                    AgentExecutionPort.class,
                    FeishuDelivery.class,
                    FeishuService.class,
                    FeishuSettings.class,
                    TaskMapper.class,
                    GovernanceMapper.class,
                    RuntimeMapper.class,
                    FeishuMapper.class,
                    OperationsMapper.class);
            assertThat(context.getBean(MemoryMapper.class).isMemoryWorkerRoleRestricted()).isTrue();
            var restricted = new JdbcTemplate(context.getBean(DataSource.class));
            for (String schema : List.of("business", "agent_runtime", "channel", "control_plane")) {
                assertThat(
                                restricted.queryForObject(
                                        "SELECT has_schema_privilege(current_user, ?, 'USAGE')",
                                        Boolean.class,
                                        schema))
                        .isFalse();
            }
            assertThat(
                            restricted.queryForObject(
                                    "SELECT has_schema_privilege(current_user, 'control_plane', 'CREATE')",
                                    Boolean.class))
                    .isFalse();
            assertThat(context.getBean(MemoryProcessingService.class)).isNotNull();
            assertThat(
                            request(context, "/actuator/health/readiness", "GET", null, false)
                                    .statusCode())
                    .isEqualTo(200);
            assertThat(request(context, "/v1/conversations", "GET", null, false).statusCode())
                    .isEqualTo(404);
            inventory("memory", context);
        }
    }

    @ParameterizedTest
    @ValueSource(strings = {"USAGE", "CREATE"})
    void accidentalControlPlanePrivilegePreventsMemoryWorkerStartup(String privilege) {
        sql.execute("GRANT " + privilege + " ON SCHEMA control_plane TO financeclaw_memory");
        try {
            assertThatThrownBy(
                            () -> {
                                try (var context =
                                        start(
                                                MemoryWorkerApplication.class,
                                                false,
                                                Map.of(
                                                        "spring.datasource.username",
                                                        "financeclaw_memory",
                                                        "spring.datasource.password",
                                                        "test-memory-only",
                                                        "financeclaw.memory.embedding.revision",
                                                        "role-fixture"))) {
                                    // A successful context is closed before the assertion fails.
                                }
                            })
                    .hasRootCauseMessage("Memory worker requires a restricted database role");
        } finally {
            sql.execute("REVOKE " + privilege + " ON SCHEMA control_plane FROM financeclaw_memory");
        }
    }

    @Test
    void initializationRoleLoadsOnlySchemaCreationAndGrants() {
        try (var context =
                start(
                        DatabaseInitializationApplication.class,
                        false,
                        Map.of(
                                "spring.flyway.enabled",
                                "true",
                                "financeclaw.business.ziwei.enabled",
                                "true",
                                "financeclaw.feishu.enabled",
                                "true"))) {
            assertThat(context.getBean(Flyway.class).info().current().getVersion().toString())
                    .isEqualTo("4");
            assertThat(mappers(context)).isEmpty();
            absent(
                    context,
                    SqlSessionFactory.class,
                    GrantService.class,
                    ArtifactService.class,
                    ReportService.class,
                    ReleaseService.class,
                    TaskWorker.class,
                    AgentExecutionPort.class,
                    MemoryProcessingService.class,
                    MemoryMapper.class,
                    AgentScopeMemoryModels.class,
                    FeishuSettings.class);
            inventory("init-db", context);
        }
    }

    @Test
    void unifiedHttpKeepsGrantReplayOwnershipAndQueuedCancellation() throws Exception {
        try (var api =
                application("default", true, Map.of("financeclaw.worker.enabled", "false"))) {
            var json = new ObjectMapper();
            var created = request(api, "/v1/conversations", "POST", "{}", true);
            assertThat(created.statusCode()).isEqualTo(201);
            String conversation = json.readTree(created.body()).path("conversation_id").asText();
            String base = "/v1/conversations/" + conversation;
            var grant = request(api, base + "/grant", "GET", null, true);
            assertThat(grant.statusCode()).isEqualTo(200);
            long revision = json.readTree(grant.body()).path("revision").asLong();
            String update = "{\"revision\":" + revision + ",\"scopes\":[\"artifact:read\"]}";
            var changed = request(api, base + "/grant", "PUT", update, true);
            assertThat(changed.statusCode()).isEqualTo(200);
            assertThat(json.readTree(changed.body()).path("revision").asLong())
                    .isEqualTo(revision + 1);
            assertThat(request(api, base + "/grant", "PUT", update, true).body())
                    .isEqualTo(changed.body());
            assertThat(
                            request(
                                            api,
                                            base + "/grant",
                                            "PUT",
                                            update.replace("artifact:read", "report:write"),
                                            true)
                                    .statusCode())
                    .isEqualTo(409);
            String other =
                    api.getBean(com.financeclaw.app.task.SubmitTaskService.class)
                            .createConversation(
                                    new com.financeclaw.domain.Task.Owner(
                                            "other-tenant", "other-owner"));
            assertThat(
                            request(api, "/v1/conversations/" + other + "/grant", "GET", null, true)
                                    .statusCode())
                    .isEqualTo(404);
            var accepted =
                    request(
                            api,
                            base + "/turns",
                            "POST",
                            "{\"message\":\"queued cancellation\"}",
                            true);
            assertThat(accepted.statusCode()).isEqualTo(202);
            String turn = json.readTree(accepted.body()).path("turn_id").asText();
            String path = base + "/turns/" + turn;
            var cancelled = request(api, path + "/cancel", "POST", "{}", true);
            assertThat(cancelled.statusCode()).isEqualTo(202);
            assertThat(
                            json.readTree(request(api, path, "GET", null, true).body())
                                    .path("status")
                                    .asText())
                    .isEqualTo("cancelled");
            var replay = request(api, path + "/cancel", "POST", "{}", true);
            assertThat(replay.statusCode()).isEqualTo(202);
            var replayBody = json.readTree(replay.body());
            assertThat(replayBody.path("idempotent_replay").asBoolean()).isTrue();
            assertThat(replayBody.path("revision"))
                    .isEqualTo(json.readTree(cancelled.body()).path("revision"));
            assertThat(replayBody.path("turn_id").asText()).isEqualTo(turn);
            assertThat(replayBody.path("status").asText()).isEqualTo("cancelled");
            var deliveries = request(api, "/v1/feishu/deliveries", "GET", null, true);
            assertThat(deliveries.statusCode()).isEqualTo(200);
            assertThat(json.readTree(deliveries.body()).isEmpty()).isTrue();
        }
    }

    @Test
    void oneJvmAcceptsExecutesAndPublishesResultsToItsOwnEventStream() throws Exception {
        // Disable automatic claims only so the stream can subscribe before execution begins.
        try (var context =
                application("default", true, Map.of("financeclaw.worker.enabled", "false"))) {
            var json = new ObjectMapper();
            var conversation = request(context, "/v1/conversations", "POST", "{}", true);
            assertThat(conversation.statusCode()).isEqualTo(201);
            String id = json.readTree(conversation.body()).path("conversation_id").asText();
            var accepted =
                    request(
                            context,
                            "/v1/conversations/" + id + "/turns",
                            "POST",
                            "{\"message\":\"single JVM execution\"}",
                            true);
            assertThat(accepted.statusCode()).isEqualTo(202);
            String turn = json.readTree(accepted.body()).path("turn_id").asText();
            String path = "/v1/conversations/" + id + "/turns/" + turn;
            assertThat(
                            json.readTree(request(context, path, "GET", null, true).body())
                                    .path("status")
                                    .asText())
                    .isEqualTo("queued");
            var stream =
                    context.getBean(TaskEvents.class)
                            .open(
                                    new com.financeclaw.domain.Task.Owner("local", "developer"),
                                    id,
                                    turn,
                                    1)
                            .filter(frame -> frame.data() != null)
                            .collectList()
                            .toFuture();
            var claim = context.getBean(ExecutionLeaseService.class).claim("same-jvm", 60000);
            assertThat(claim).isNotNull();
            context.getBean(TaskWorker.class).execute(claim);
            var frames = stream.get(10, java.util.concurrent.TimeUnit.SECONDS);
            assertThat(frames).isNotEmpty();
            assertThat(frames).anyMatch(frame -> "running".equals(frame.data().status()));
            assertThat(frames.getLast().data().status()).isEqualTo("completed");
            assertThat(frames.getLast().id()).isNotNull();
            var result = json.readTree(request(context, path, "GET", null, true).body());
            assertThat(result.path("status").asText()).isEqualTo("completed");
            assertThat(result.path("result").asText()).isNotBlank();
            assertThat(Long.parseLong(frames.getLast().id()))
                    .isEqualTo(result.path("revision").asLong());
            assertThat(sql.queryForObject("SELECT count(*) FROM business.attempts", Long.class))
                    .isOne();
            assertThat(
                            sql.queryForObject(
                                    "SELECT count(*) FROM business.journal WHERE role='assistant'",
                                    Long.class))
                    .isOne();
        }
    }
}
