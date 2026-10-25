package com.financeclaw.p3;

import static org.assertj.core.api.Assertions.*;

import com.financeclaw.app.release.AgentPlanValidator;
import com.financeclaw.app.release.AgentReleaseIdentity;
import com.financeclaw.app.release.ComposedAgentReleaseIdentity;
import com.financeclaw.app.release.ReleaseIdentity;
import com.financeclaw.app.release.ReleaseService;
import com.financeclaw.domain.Governance.Blocked;
import com.financeclaw.domain.Task.Owner;
import com.financeclaw.domain.Task.Turn;
import com.financeclaw.domain.agent.AgentDraft;
import com.financeclaw.domain.agent.AgentExecutionPlan;
import com.financeclaw.domain.agent.AgentReleaseManifest;
import com.financeclaw.domain.agent.ComposedAgentReleaseManifest;
import com.financeclaw.domain.release.BusinessFeatures;
import com.financeclaw.domain.release.ReleaseDefinition;
import com.financeclaw.infrastructure.execution.CatalogExecutionRelease;
import com.financeclaw.infrastructure.release.MyBatisReleaseRepository;
import com.financeclaw.infrastructure.release.ReleaseMapper;
import com.financeclaw.support.PayloadJson;
import java.util.List;
import java.util.UUID;
import java.util.function.Supplier;
import javax.sql.DataSource;
import org.apache.ibatis.session.Configuration;
import org.apache.ibatis.session.LocalCacheScope;
import org.flywaydb.core.Flyway;
import org.junit.jupiter.api.*;
import org.junit.jupiter.api.parallel.Execution;
import org.junit.jupiter.api.parallel.ExecutionMode;
import org.junit.jupiter.params.ParameterizedTest;
import org.junit.jupiter.params.provider.CsvSource;
import org.junit.jupiter.params.provider.NullAndEmptySource;
import org.junit.jupiter.params.provider.ValueSource;
import org.mybatis.spring.SqlSessionFactoryBean;
import org.mybatis.spring.SqlSessionTemplate;
import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.jdbc.datasource.DataSourceTransactionManager;
import org.springframework.jdbc.datasource.DriverManagerDataSource;
import org.springframework.transaction.support.TransactionTemplate;
import org.testcontainers.containers.PostgreSQLContainer;

/**
 * 发布解析的真实 PostgreSQL/MyBatis 边界：一次 JOIN 查询必须保留原格式、摘要、基础发布和 owner 校验。 不启动 Spring Web/Worker，不 mock
 * 仓库；计数窗口只包住待验证的解析，fixture 与核查走原始数据源。 损坏记录通过独立 INSERT 模拟不可信恢复数据，不关闭不可变 trigger，也不改写已有发布。
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
@Execution(ExecutionMode.SAME_THREAD)
class ReleaseLookupIT {
    private static final Owner OWNER = new Owner("release-lookup", "alice");
    private static final String AGENT = "d1a5c2dc-93aa-4692-8fb6-dab14cb7888e";
    private static final String PROMPT = "Frozen lookup prompt.";
    private static final String ARTIFACT = "a".repeat(64);
    private final PostgreSQLContainer<?> postgres =
            new PostgreSQLContainer<>("postgres:16.10-alpine");
    private final JdbcExecutionCounter counter = new JdbcExecutionCounter();
    private JdbcTemplate raw;
    private ReleaseService releases;
    private CatalogExecutionRelease execution;
    private TransactionTemplate transactions;
    private MyBatisReleaseRepository repository;
    private ReleaseDefinition base;
    private String baseHash;

    record Fixture(String format, String hash, Object manifest, AgentExecutionPlan plan) {}

    record Observed<T>(T value, Throwable failure, JdbcExecutionCounter.Measurement sql) {}

    @BeforeAll
    void start() throws Exception {
        try {
            postgres.start();
            DataSource original =
                    new DriverManagerDataSource(
                            postgres.getJdbcUrl(), postgres.getUsername(), postgres.getPassword());
            raw = new JdbcTemplate(original);
            Flyway.configure().dataSource(original).load().migrate();
            DataSource counted = counter.wrap(original);
            var configuration = new Configuration();
            configuration.setMapUnderscoreToCamelCase(true);
            configuration.setCacheEnabled(false);
            configuration.setLocalCacheScope(LocalCacheScope.STATEMENT);
            configuration.addMapper(ReleaseMapper.class);
            var factory = new SqlSessionFactoryBean();
            factory.setDataSource(counted);
            factory.setConfiguration(configuration);
            var mapper = new SqlSessionTemplate(factory.getObject()).getMapper(ReleaseMapper.class);
            repository = new MyBatisReleaseRepository(mapper);
            transactions = new TransactionTemplate(new DataSourceTransactionManager(counted));
        } catch (Exception | Error failure) {
            postgres.stop();
            throw failure;
        }
    }

    @BeforeEach
    void reset() {
        raw.execute(
                "TRUNCATE control_plane.deployment_changes,control_plane.deployments,"
                        + "control_plane.release_manifests CASCADE");
        // 每个反例都有另一个真实、可用的 current；解析错误不能通过回退 current 被掩盖。
        releases =
                new ReleaseService(
                        () -> definition("global-current", 12), repository, transactions);
        execution = new CatalogExecutionRelease(releases);
        base = definition("frozen-base", 12);
        base.validate();
        baseHash = ReleaseIdentity.hash(base);
    }

    @AfterAll
    void stop() {
        postgres.stop();
    }

    @ParameterizedTest
    @ValueSource(strings = {"legacy-v1", "agent-v2", "agent-v3"})
    void eachPhysicalFormatResolvesWithOneJdbcExecutionAndItsExactFrozenProjection(String format) {
        var fixture = validFixture(format);
        installBase();
        if (!format.equals("legacy-v1")) insert(fixture.hash(), format, fixture.manifest());
        var observed = observe(() -> releases.resolve(fixture.hash()));
        assertThat(observed.failure()).isNull();
        assertOneLookup(observed.sql());
        var resolved = observed.value();
        if (format.equals("legacy-v1")) {
            assertThat(resolved.agentId()).isEqualTo(AgentDraft.BUILTIN_AGENT);
            assertThat(resolved.systemPrompt()).isEqualTo(AgentDraft.BUILTIN_PROMPT);
            assertThat(resolved.tenant()).isNull();
            assertThat(resolved.subject()).isNull();
            assertThat(resolved.definition()).isEqualTo(base);
            assertThat(resolved.plan()).isNull();
        } else {
            resolved.requireIdentity(OWNER, AGENT);
            assertThat(resolved.systemPrompt()).isEqualTo(PROMPT);
            assertThat(resolved.tenant()).isEqualTo(OWNER.tenant());
            assertThat(resolved.subject()).isEqualTo(OWNER.subject());
            if (format.equals("agent-v2")) {
                assertThat(resolved.definition()).isEqualTo(base);
                assertThat(resolved.plan()).isNull();
            } else {
                assertThat(resolved.plan()).isEqualTo(fixture.plan());
                assertThat(resolved.definition()).isEqualTo(fixture.plan().definition());
                // 显式空计划保持空权限，不能误用 JOIN 读到的完整基础配置作为执行投影。
                assertThat(resolved.definition().scopes()).isEmpty();
                assertThat(resolved.definition()).isNotEqualTo(base);
            }
        }
        // 不同事务再次读取仍然 execute；本轮优化没有引入清单或 owner 的进程缓存。
        var repeated = observe(() -> releases.resolve(fixture.hash()));
        assertThat(repeated.value()).isEqualTo(resolved);
        assertThat(repeated.failure()).isNull();
        assertOneLookup(repeated.sql());
    }

    @ParameterizedTest
    @ValueSource(strings = {"legacy-v1", "agent-v2", "agent-v3"})
    void validDecodedBodiesWithMismatchedStoredHashesRemainUnavailable(String format) {
        var original = validFixture(format);
        installBaseUnlessLegacy(format);
        Object tampered;
        String tamperedHash;
        if (format.equals("legacy-v1")) {
            var body = definition("tampered-but-valid", 12);
            body.validate();
            tampered = body;
            tamperedHash = ReleaseIdentity.hash(body);
        } else if (format.equals("agent-v2")) {
            var body = v2(baseHash, "Tampered prompt.", AgentReleaseManifest.RUNTIME_IDENTITY);
            body.validate();
            tampered = body;
            tamperedHash = AgentReleaseIdentity.hash(body);
        } else {
            var body =
                    v3(
                            baseHash,
                            "Tampered prompt.",
                            validPlan(),
                            ComposedAgentReleaseManifest.FORMAT,
                            ComposedAgentReleaseManifest.RUNTIME_IDENTITY);
            body.validate();
            AgentPlanValidator.validate(body.plan(), base, OWNER.tenant());
            tampered = body;
            tamperedHash = ComposedAgentReleaseIdentity.hash(body);
        }
        assertThat(tamperedHash)
                .as("fixture is valid JSON with a different actual digest")
                .isNotEqualTo(original.hash());
        insert(original.hash(), format, tampered);
        assertRejected(observe(() -> releases.resolve(original.hash())), "release_unavailable");
    }

    @Test
    void missingHashDoesNotSubstituteTheValidCurrentDeployment() {
        String current = releases.currentHash();
        assertThat(releases.resolve(current).definition().version()).isEqualTo("global-current");
        String absent = "f".repeat(64);
        assertThat(absent).isNotEqualTo(current);
        assertThat(
                        raw.queryForObject(
                                "SELECT count(*) FROM control_plane.release_manifests "
                                        + "WHERE release_hash=?",
                                Integer.class,
                                absent))
                .isZero();
        assertRejected(observe(() -> releases.resolve(absent)), "release_unavailable");
        assertThat(releases.currentHash()).isEqualTo(current);
    }

    @ParameterizedTest
    @NullAndEmptySource
    @ValueSource(strings = {" "})
    void absentOrBlankInputIsRejectedBeforeAnyJdbcExecution(String hash) {
        var observed = observe(() -> releases.resolve(hash));
        assertThat(observed.failure())
                .isInstanceOf(Blocked.class)
                .hasMessage("release_unavailable");
        assertThat(observed.sql().statements()).isZero();
        assertThat(observed.sql().families()).isEmpty();
    }

    @ParameterizedTest
    @CsvSource({
        "agent-v2,missing",
        "agent-v3,missing",
        "agent-v3,non-legacy",
        "agent-v3,hash-mismatch",
        "agent-v3,malformed"
    })
    void joinedBaseMustExistBeLegacyAndMatchItsOwnDigest(String format, String corruption) {
        String referencedHash = baseHash;
        switch (corruption) {
            case "missing" ->
                    assertThat(
                                    raw.queryForObject(
                                            "SELECT count(*) FROM control_plane.release_manifests WHERE release_hash=?",
                                            Integer.class,
                                            baseHash))
                            .isZero();
            case "non-legacy" -> {
                // 基础位置放入完全有效的 v2；即使它自己最终指向 legacy，也禁止递归继承 Agent。
                installBase();
                var nested =
                        v2(baseHash, "Valid nested agent.", AgentReleaseManifest.RUNTIME_IDENTITY);
                nested.validate();
                referencedHash = AgentReleaseIdentity.hash(nested);
                insert(referencedHash, "agent-v2", nested);
                assertThat(releases.resolve(referencedHash).definition()).isEqualTo(base);
            }
            case "hash-mismatch" -> {
                var changed = definition("different-base-body", 12);
                changed.validate();
                assertThat(ReleaseIdentity.hash(changed)).isNotEqualTo(baseHash);
                insert(baseHash, "legacy-v1", changed);
            }
            case "malformed" -> insertJson(baseHash, "legacy-v1", "{}");
            default -> throw new AssertionError("Unknown base corruption");
        }
        var fixture = fixture(format, referencedHash, validPlan());
        // 外层摘要与领域格式都有效；此处失败只能来自所引用基础清单，不是无效 root fixture。
        assertValidOuter(fixture);
        insert(fixture.hash(), format, fixture.manifest());
        assertRejected(observe(() -> releases.resolve(fixture.hash())), "release_unavailable");
    }

    @ParameterizedTest
    @ValueSource(strings = {"tenant", "subject", "agent-id"})
    void oneLookupStillRequiresTheExactTurnOwnerAndAgent(String mismatch) {
        var fixture = validFixture("agent-v3");
        installBase();
        insert(fixture.hash(), fixture.format(), fixture.manifest());
        var validTurn = turn(OWNER, AGENT, fixture.hash());
        var allowed = observe(() -> execution.requireForTurn(validTurn));
        assertThat(allowed.failure()).isNull();
        assertThat(allowed.value().maxModelCalls()).isEqualTo(base.maxModelCalls());
        assertThat(allowed.value().scopes()).isEmpty();
        assertOneLookup(allowed.sql());
        Owner forgedOwner =
                switch (mismatch) {
                    case "tenant" -> new Owner("another-tenant", OWNER.subject());
                    case "subject" -> new Owner(OWNER.tenant(), "another-user");
                    default -> OWNER;
                };
        String forgedAgent = mismatch.equals("agent-id") ? UUID.randomUUID().toString() : AGENT;
        assertRejected(
                observe(
                        () ->
                                execution.requireForTurn(
                                        turn(forgedOwner, forgedAgent, fixture.hash()))),
                "agent_identity_mismatch");
        // 同线程先允许、再拒绝、再允许，防止按 hash 缓存了前一次 owner 的授权结论。
        var stillAllowed = observe(() -> execution.requireForTurn(validTurn));
        assertThat(stillAllowed.failure()).isNull();
        assertThat(stillAllowed.value()).isEqualTo(allowed.value());
        assertOneLookup(stillAllowed.sql());
    }

    @Test
    void selfConsistentV3DigestCannotAuthorizeAPlanAboveItsFrozenBaseBudget() {
        installBase();
        var invalidPlan = overBudgetPlan();
        assertThatThrownBy(() -> AgentPlanValidator.validate(invalidPlan, base, OWNER.tenant()))
                .isInstanceOf(Blocked.class)
                .hasMessage("agent_plan_invalid");
        var fixture = fixture("agent-v3", baseHash, invalidPlan);
        assertValidOuter(fixture);
        insert(fixture.hash(), fixture.format(), fixture.manifest());
        assertRejected(observe(() -> releases.resolve(fixture.hash())), "agent_plan_invalid");
    }

    @ParameterizedTest
    @ValueSource(
            strings = {
                "hash-before-runtime",
                "manifest-before-base",
                "runtime-before-base",
                "base-before-plan"
            })
    void joinedInvalidBaseDoesNotChangeTheEstablishedValidationOrder(String precedence) {
        insertJson(baseHash, "legacy-v1", "{}");
        String runtime =
                precedence.equals("hash-before-runtime") || precedence.equals("runtime-before-base")
                        ? "agentscope-unsupported"
                        : ComposedAgentReleaseManifest.RUNTIME_IDENTITY;
        String format =
                precedence.equals("manifest-before-base")
                        ? "agent-release/unsupported"
                        : ComposedAgentReleaseManifest.FORMAT;
        var plan = precedence.equals("base-before-plan") ? overBudgetPlan() : validPlan();
        var body = v3(baseHash, PROMPT, plan, format, runtime);
        String actualHash = ComposedAgentReleaseIdentity.hash(body);
        String storedHash = precedence.equals("hash-before-runtime") ? "c".repeat(64) : actualHash;
        String expected =
                switch (precedence) {
                    case "manifest-before-base" -> "agent_manifest_invalid";
                    case "runtime-before-base" -> "agent_runtime_incompatible";
                    default -> "release_unavailable";
                };
        if (precedence.equals("hash-before-runtime")) {
            assertThat(actualHash).isNotEqualTo(storedHash);
            assertThatThrownBy(body::validate).hasMessage("agent_runtime_incompatible");
        } else {
            assertThat(actualHash).isEqualTo(storedHash);
            if (precedence.equals("base-before-plan")) {
                body.validate();
                assertThatThrownBy(() -> AgentPlanValidator.validate(plan, base, OWNER.tenant()))
                        .hasMessage("agent_plan_invalid");
            } else {
                assertThatThrownBy(body::validate).hasMessage(expected);
            }
        }
        insert(storedHash, "agent-v3", body);
        // malformed base 在适配器中只作为缺失/无效投影返回，不能提前覆盖外层错误。
        assertRejected(observe(() -> releases.resolve(storedHash)), expected);
    }

    @ParameterizedTest
    @ValueSource(strings = {"v2-as-v3", "v3-as-v2"})
    void physicalFormatSelectsTheDecoderInsteadOfGuessingTheJsonShape(String mismatch) {
        String originalFormat = mismatch.equals("v2-as-v3") ? "agent-v2" : "agent-v3";
        String wrongFormat = mismatch.equals("v2-as-v3") ? "agent-v3" : "agent-v2";
        var fixture = validFixture(originalFormat);
        installBase();
        insert(fixture.hash(), wrongFormat, fixture.manifest());
        assertRejected(observe(() -> releases.resolve(fixture.hash())), "release_unavailable");
    }

    private Fixture validFixture(String format) {
        var fixture = fixture(format, baseHash, validPlan());
        assertValidOuter(fixture);
        AgentPlanValidator.validate(validPlan(), base, OWNER.tenant());
        return fixture;
    }

    private Fixture fixture(String format, String referencedBase, AgentExecutionPlan plan) {
        return switch (format) {
            case "legacy-v1" -> new Fixture(format, baseHash, base, null);
            case "agent-v2" -> {
                var body = v2(referencedBase, PROMPT, AgentReleaseManifest.RUNTIME_IDENTITY);
                yield new Fixture(format, AgentReleaseIdentity.hash(body), body, null);
            }
            case "agent-v3" -> {
                var body =
                        v3(
                                referencedBase,
                                PROMPT,
                                plan,
                                ComposedAgentReleaseManifest.FORMAT,
                                ComposedAgentReleaseManifest.RUNTIME_IDENTITY);
                yield new Fixture(format, ComposedAgentReleaseIdentity.hash(body), body, plan);
            }
            default -> throw new AssertionError("Unknown physical format");
        };
    }

    /** 正反例均先核对外层到底有没有越过摘要、领域格式这一层，避免 fixture 自身坏在更早位置。 */
    private static void assertValidOuter(Fixture fixture) {
        if (fixture.manifest() instanceof ReleaseDefinition legacy) {
            legacy.validate();
            assertThat(ReleaseIdentity.hash(legacy)).isEqualTo(fixture.hash());
        } else if (fixture.manifest() instanceof AgentReleaseManifest v2) {
            v2.validate();
            assertThat(AgentReleaseIdentity.hash(v2)).isEqualTo(fixture.hash());
        } else if (fixture.manifest() instanceof ComposedAgentReleaseManifest v3) {
            v3.validate();
            assertThat(ComposedAgentReleaseIdentity.hash(v3)).isEqualTo(fixture.hash());
        } else throw new AssertionError("Unexpected manifest type");
    }

    private AgentExecutionPlan validPlan() {
        var projection =
                new ReleaseDefinition(
                        base.version(),
                        base.agent(),
                        List.of(),
                        base.primary(),
                        base.fallback(),
                        base.maxModelCalls(),
                        base.maxToolCalls(),
                        base.business());
        return new AgentExecutionPlan("primary", ARTIFACT, List.of(), List.of(), projection);
    }

    private AgentExecutionPlan overBudgetPlan() {
        var valid = validPlan();
        var low = valid.definition();
        var expanded =
                new ReleaseDefinition(
                        low.version(),
                        low.agent(),
                        low.scopes(),
                        low.primary(),
                        low.fallback(),
                        base.maxModelCalls() + 1,
                        low.maxToolCalls(),
                        low.business());
        expanded.validate();
        return new AgentExecutionPlan(
                valid.modelProfile(),
                valid.runtimeArtifactHash(),
                valid.requestedCapabilities(),
                valid.capabilities(),
                expanded);
    }

    private static AgentReleaseManifest v2(String referencedBase, String prompt, String runtime) {
        return new AgentReleaseManifest(
                AgentReleaseManifest.FORMAT,
                AGENT,
                OWNER.tenant(),
                OWNER.subject(),
                1,
                "Lookup agent",
                prompt,
                referencedBase,
                runtime);
    }

    private static ComposedAgentReleaseManifest v3(
            String referencedBase,
            String prompt,
            AgentExecutionPlan plan,
            String format,
            String runtime) {
        return new ComposedAgentReleaseManifest(
                format,
                AGENT,
                OWNER.tenant(),
                OWNER.subject(),
                1,
                "Lookup composed agent",
                prompt,
                referencedBase,
                plan,
                runtime);
    }

    private static ReleaseDefinition definition(String version, int models) {
        return new ReleaseDefinition(
                version,
                "finance_agent",
                ReleaseDefinition.SCOPES,
                ReleaseDefinition.OFFLINE,
                null,
                models,
                32,
                new BusinessFeatures(List.of(), "lookup-packaged-skills", "{}", null));
    }

    private static Turn turn(Owner owner, String agent, String hash) {
        return new Turn(
                "lookup-turn",
                "lookup-conversation",
                owner.tenant(),
                owner.subject(),
                "lookup-key",
                "lookup-request-hash",
                "Look up the frozen release.",
                hash,
                "running",
                1,
                null,
                null,
                agent);
    }

    private void installBase() {
        insert(baseHash, "legacy-v1", base);
    }

    private void installBaseUnlessLegacy(String format) {
        if (!format.equals("legacy-v1")) installBase();
    }

    private void insert(String hash, String format, Object manifest) {
        insertJson(hash, format, PayloadJson.canonical(manifest));
    }

    private void insertJson(String hash, String format, String json) {
        assertThat(
                        raw.update(
                                "INSERT INTO control_plane.release_manifests"
                                        + "(release_hash,manifest_format,manifest) VALUES(?,?,?::jsonb)",
                                hash,
                                format,
                                json))
                .isOne();
    }

    private <T> Observed<T> observe(Supplier<T> operation) {
        counter.begin("release-lookup");
        T value = null;
        Throwable failure = null;
        try {
            value = operation.get();
        } catch (Throwable caught) {
            failure = caught;
        }
        return new Observed<>(value, failure, counter.finish());
    }

    private static void assertRejected(Observed<?> observed, String code) {
        assertThat(observed.value()).isNull();
        assertThat(observed.failure()).isInstanceOf(Blocked.class).hasMessage(code);
        assertOneLookup(observed.sql());
    }

    private static void assertOneLookup(JdbcExecutionCounter.Measurement measurement) {
        assertThat(measurement.statements()).isOne();
        assertThat(measurement.count("release_lookup")).isOne();
        assertThat(measurement.count("other")).isZero();
        assertThat(measurement.executions()).hasSize(1);
        assertThat(measurement.executions().getFirst().templateSha256()).matches("[0-9a-f]{64}");
        assertThat(measurement.count("release_legacy")).isZero();
        assertThat(measurement.count("release_agent_v2")).isZero();
        assertThat(measurement.count("release_agent_v3")).isZero();
    }
}
