package com.financeclaw.agent;

import static org.assertj.core.api.Assertions.*;
import static org.mockito.ArgumentMatchers.*;
import static org.mockito.Mockito.*;

import com.financeclaw.app.agent.AgentService;
import com.financeclaw.app.release.AgentReleaseIdentity;
import com.financeclaw.app.release.ReleaseIdentity;
import com.financeclaw.app.release.ReleaseService;
import com.financeclaw.domain.Task.Owner;
import com.financeclaw.domain.Task.Problem;
import com.financeclaw.domain.agent.*;
import com.financeclaw.domain.release.*;
import com.financeclaw.infrastructure.agent.AgentMapper;
import com.financeclaw.infrastructure.agent.MyBatisAgentRepository;
import com.financeclaw.infrastructure.release.MyBatisReleaseRepository;
import com.financeclaw.infrastructure.release.ReleaseMapper;
import java.util.List;
import java.util.UUID;
import java.util.concurrent.*;
import javax.sql.DataSource;
import org.apache.ibatis.session.Configuration;
import org.apache.ibatis.session.LocalCacheScope;
import org.flywaydb.core.Flyway;
import org.junit.jupiter.api.*;
import org.mybatis.spring.SqlSessionFactoryBean;
import org.mybatis.spring.SqlSessionTemplate;
import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.jdbc.datasource.DataSourceTransactionManager;
import org.springframework.jdbc.datasource.DriverManagerDataSource;
import org.springframework.transaction.support.TransactionTemplate;
import org.testcontainers.containers.PostgreSQLContainer;

/** 真实 PostgreSQL 上验证 owner 隔离、草稿 CAS、发布原子性和新旧清单兼容。 */
class AgentCatalogIT {
    private static final PostgreSQLContainer<?> PG =
            new PostgreSQLContainer<>("postgres:16.10-alpine")
                    .withDatabaseName("agents")
                    .withUsername("agents")
                    .withPassword("test-only");
    private static DataSource source;
    private static JdbcTemplate sql;
    private final Owner owner = new Owner("tenant", "owner");
    private TransactionTemplate tx;
    private MyBatisAgentRepository repository;
    private MyBatisReleaseRepository releaseRepository;
    private ReleaseService releases;
    private AgentService agents;

    @BeforeAll
    static void initialize() {
        PG.start();
        source = new DriverManagerDataSource(PG.getJdbcUrl(), PG.getUsername(), PG.getPassword());
        sql = new JdbcTemplate(source);
        Flyway.configure().dataSource(source).load().migrate();
    }

    @AfterAll
    static void close() {
        PG.stop();
    }

    @BeforeEach
    void reset() throws Exception {
        sql.execute(
                "TRUNCATE business.conversations,control_plane.agents,control_plane.deployments,"
                        + "control_plane.deployment_changes,control_plane.release_manifests CASCADE");
        tx = new TransactionTemplate(new DataSourceTransactionManager(source));
        var session = session();
        repository = new MyBatisAgentRepository(session.getMapper(AgentMapper.class));
        releaseRepository = new MyBatisReleaseRepository(session.getMapper(ReleaseMapper.class));
        releases = new ReleaseService(() -> definition("legacy-v1"), releaseRepository, tx);
        agents = new AgentService(repository, releases, tx);
    }

    @Test
    void createReplayReturnsOriginalDraftAfterEditsAndDefaultReleaseChange() {
        String id = id();
        var initial = agents.create(owner, id, "Original", "Original prompt");
        var edited = agents.update(owner, id, 1, "Edited", "Edited prompt");
        releases.publish(definition("legacy-v2"));
        assertThat(agents.create(owner, id, "Original", "Original prompt")).isEqualTo(initial);
        assertThat(agents.get(owner, id)).isEqualTo(edited);
        assertThat(edited.baseReleaseHash()).isEqualTo(initial.baseReleaseHash());
        assertThat(edited.baseReleaseHash()).isNotEqualTo(releases.currentHash());
        assertThatThrownBy(() -> agents.create(owner, id, "Different", "Original prompt"))
                .hasMessage("agent_id_conflict");
        assertThat(count("control_plane.agents")).isEqualTo(1);
        assertThat(count("control_plane.agent_draft_revisions")).isEqualTo(2);
    }

    @Test
    void anotherOwnerCannotReadEditPublishResolveOrReuseTheIdentity() {
        String id = id();
        agents.create(owner, id, "Private", "Private prompt");
        for (var other : List.of(new Owner("tenant", "other"), new Owner("other", "owner"))) {
            assertThatThrownBy(() -> agents.get(other, id)).hasMessage("not_found");
            assertThatThrownBy(() -> agents.update(other, id, 1, "x", "x")).hasMessage("not_found");
            assertThatThrownBy(() -> agents.publish(other, id, 1, 0)).hasMessage("not_found");
            assertThatThrownBy(() -> agents.resolveTarget(other, id)).hasMessage("not_found");
            assertThatThrownBy(() -> agents.history(other, id, 0, 20)).hasMessage("not_found");
            assertThatThrownBy(() -> agents.create(other, id, "Private", "Private prompt"))
                    .hasMessage("not_found");
            assertThat(agents.list(other))
                    .extracting(AgentDefinition::id)
                    .containsExactly("finance_agent");
        }
        assertThat(agents.list(owner))
                .extracting(AgentDefinition::id)
                .containsExactly("finance_agent", id);
    }

    @Test
    void draftChangesDoNotAffectPublishedPromptAndNewReleaseRetainsOriginalBase() {
        String id = id();
        var draft = agents.create(owner, id, "Agent", "First prompt");
        assertThatThrownBy(() -> agents.resolveTarget(owner, id)).hasMessage("agent_not_published");
        var defaultPointer = releases.current();
        var published = agents.publish(owner, id, 1, 0);
        var firstTarget = agents.resolveTarget(owner, id);
        agents.update(owner, id, 1, "Agent updated", "Second prompt");
        assertThat(agents.resolveTarget(owner, id)).isEqualTo(firstTarget);
        assertThat(releases.resolve(published.releaseHash()).systemPrompt())
                .isEqualTo("First prompt");
        releases.publish(definition("new-default"));
        var second = agents.publish(owner, id, 2, 1);
        assertThat(second.revision()).isEqualTo(2);
        assertThat(releases.resolve(second.releaseHash()).systemPrompt())
                .isEqualTo("Second prompt");
        assertThat(releases.resolve(second.releaseHash()).definition())
                .isEqualTo(definition("legacy-v1"));
        assertThat(agents.get(owner, id).baseReleaseHash()).isEqualTo(draft.baseReleaseHash());
        assertThat(agents.history(owner, id, 0, 20))
                .containsExactly(
                        new DeploymentChange("agent:" + id, 1, published.releaseHash(), null),
                        new DeploymentChange(
                                "agent:" + id, 2, second.releaseHash(), published.releaseHash()));
        assertThat(releases.current().revision()).isEqualTo(defaultPointer.revision() + 1);
        assertThat(releases.currentHash())
                .isEqualTo(ReleaseIdentity.hash(definition("new-default")));
    }

    @Test
    void builtinIsVirtualReadOnlyAndKeepsOriginalLegacyIdentity() {
        var builtin = agents.get(owner, "finance_agent");
        assertThat(builtin.revision()).isZero();
        assertThat(builtin.systemPrompt()).isEqualTo(AgentDraft.BUILTIN_PROMPT);
        assertThat(count("control_plane.agents")).isZero();
        assertThat(agents.resolveTarget(owner, "finance_agent"))
                .isEqualTo(new ResolvedTarget("finance_agent", releases.currentHash()));
        assertThat(releases.resolve(releases.currentHash()).definition())
                .isEqualTo(definition("legacy-v1"));
        assertThat(releases.currentHash()).isEqualTo(ReleaseIdentity.hash(definition("legacy-v1")));
        assertThatThrownBy(() -> agents.create(owner, "finance_agent", "x", "x"))
                .hasMessage("agent_read_only");
        assertThatThrownBy(() -> agents.update(owner, "finance_agent", 0, "x", "x"))
                .hasMessage("agent_read_only");
        assertThatThrownBy(() -> agents.publish(owner, "finance_agent", 0, 0))
                .hasMessage("agent_read_only");
    }

    @Test
    void concurrentCreationWithSameIdentityPreservesSingleInitialDraft() throws Exception {
        String id = id();
        var barrier = new CyclicBarrier(2);
        try (var pool = Executors.newFixedThreadPool(2)) {
            var first =
                    pool.submit(
                            () -> {
                                barrier.await(5, TimeUnit.SECONDS);
                                return agents.create(owner, id, "Agent", "prompt");
                            });
            var second =
                    pool.submit(
                            () -> {
                                barrier.await(5, TimeUnit.SECONDS);
                                return agents.create(owner, id, "Agent", "prompt");
                            });
            assertThat(first.get(10, TimeUnit.SECONDS)).isEqualTo(second.get(10, TimeUnit.SECONDS));
        }
        assertThat(count("control_plane.agents")).isEqualTo(1);
        assertThat(count("control_plane.agent_draft_revisions")).isEqualTo(1);
    }

    @Test
    void concurrentDraftUpdatesHaveOneWinnerAndNoSkippedRevision() throws Exception {
        String id = id();
        agents.create(owner, id, "Agent", "prompt");
        assertConcurrentOutcomes(
                () -> agents.update(owner, id, 1, "First", "first"),
                () -> agents.update(owner, id, 1, "Second", "second"),
                "agent_draft_revision_changed");
        assertThat(agents.get(owner, id).revision()).isEqualTo(2);
        assertThat(count("control_plane.agent_draft_revisions")).isEqualTo(2);
    }

    @Test
    void concurrentFirstPublicationHasOneWinnerAndNoOrphanCandidate() throws Exception {
        String id = id();
        agents.create(owner, id, "Agent", "prompt");
        assertConcurrentOutcomes(
                () -> agents.publish(owner, id, 1, 0),
                () -> agents.publish(owner, id, 1, 0),
                "release_revision_changed");
        assertThat(agents.history(owner, id, 0, 20)).hasSize(1);
        assertThat(count("control_plane.release_manifests")).isEqualTo(2);
    }

    @Test
    void staleDraftOrDeploymentRevisionRollsBackEntirePublication() {
        String id = id();
        agents.create(owner, id, "Agent", "first");
        agents.update(owner, id, 1, "Agent", "second");
        assertThatThrownBy(() -> agents.publish(owner, id, 1, 0))
                .hasMessage("agent_draft_revision_changed");
        assertThatThrownBy(() -> agents.publish(owner, id, 2, 7))
                .hasMessage("release_revision_changed");
        assertThat(count("control_plane.release_manifests")).isEqualTo(1);
        assertThat(releases.currentForAgent(id)).isNull();
        assertThat(agents.history(owner, id, 0, 20)).isEmpty();
    }

    @Test
    void publicationAuditFailureRollsBackManifestAndPointer() {
        var failing = spy(releaseRepository);
        doThrow(new IllegalStateException("audit_failed"))
                .when(failing)
                .insertChange(argThat(change -> change.deployment().startsWith("agent:")));
        var service = new AgentService(repository, ReleaseService.open(failing, tx), tx);
        String id = id();
        service.create(owner, id, "Agent", "prompt");
        assertThatThrownBy(() -> service.publish(owner, id, 1, 0)).hasMessage("audit_failed");
        assertThat(count("control_plane.release_manifests")).isEqualTo(1);
        assertThat(releases.currentForAgent(id)).isNull();
        assertThat(service.history(owner, id, 0, 20)).isEmpty();
    }

    @Test
    void draftInsertFailureRollsBackHeadRevision() {
        String id = id();
        var initial = agents.create(owner, id, "Agent", "prompt");
        var failing = spy(repository);
        doThrow(new IllegalStateException("draft_failed"))
                .when(failing)
                .insertDraft(argThat(draft -> draft.revision() == 2));
        var service = new AgentService(failing, releases, tx);
        assertThatThrownBy(() -> service.update(owner, id, 1, "changed", "changed"))
                .hasMessage("draft_failed");
        assertThat(agents.get(owner, id)).isEqualTo(initial);
        assertThat(count("control_plane.agent_draft_revisions")).isEqualTo(1);
    }

    @Test
    void agentReleaseCannotBecomeGlobalDefaultOrAnotherAgentsDeployment() {
        String id = id();
        agents.create(owner, id, "Agent", "prompt");
        String hash = agents.publish(owner, id, 1, 0).releaseHash();
        var defaultPointer = releases.current();
        assertThatThrownBy(() -> releases.activate(hash, defaultPointer.revision()))
                .hasMessage("release_unavailable");
        assertThatThrownBy(() -> releases.activateForAgent(id(), hash, 0))
                .hasMessage("agent_release_mismatch");
        assertThatThrownBy(() -> releases.activateForAgent(id, defaultPointer.releaseHash(), 1))
                .hasMessage("agent_release_mismatch");
        assertThat(releases.current()).isEqualTo(defaultPointer);
    }

    @Test
    void missingNestedTamperedAndIncompatibleManifestsAreRejected() {
        String id = id();
        var draft = agents.create(owner, id, "Agent", "prompt");
        var manifest =
                AgentReleaseManifest.from(
                        new AgentDefinition(id, owner.tenant(), owner.subject(), 1), draft);
        String hash = releases.registerAgent(manifest);
        var nested = manifest(id(), hash, "nested", AgentReleaseManifest.RUNTIME_IDENTITY);
        assertThatThrownBy(() -> releases.registerAgent(nested)).hasMessage("release_unavailable");
        var missing =
                manifest(id(), "f".repeat(64), "missing", AgentReleaseManifest.RUNTIME_IDENTITY);
        assertThatThrownBy(() -> releases.registerAgent(missing)).hasMessage("release_unavailable");
        var incompatible = manifest(id(), draft.baseReleaseHash(), "prompt", "future-runtime");
        String incompatibleHash = AgentReleaseIdentity.hash(incompatible);
        releaseRepository.insertAgentManifestIfAbsent(incompatibleHash, incompatible);
        assertThatThrownBy(() -> releases.resolve(incompatibleHash))
                .hasMessage("agent_runtime_incompatible");
        var original =
                manifest(
                        id(),
                        draft.baseReleaseHash(),
                        "original",
                        AgentReleaseManifest.RUNTIME_IDENTITY);
        String originalHash = AgentReleaseIdentity.hash(original);
        releaseRepository.insertAgentManifestIfAbsent(
                originalHash,
                manifest(
                        original.agentId(),
                        draft.baseReleaseHash(),
                        "tampered",
                        original.runtimeIdentity()));
        assertThatThrownBy(() -> releases.resolve(originalHash)).hasMessage("release_unavailable");
    }

    @Test
    void draftsOwnersAndBoundExecutionTargetsAreImmutableInDatabase() {
        String id = id();
        var draft = agents.create(owner, id, "Agent", "prompt");
        assertThatThrownBy(
                        () ->
                                sql.update(
                                        "UPDATE control_plane.agents SET subject='other' WHERE id=?",
                                        id))
                .hasMessageContaining("agent owner and identity are immutable");
        assertThatThrownBy(
                        () ->
                                sql.update(
                                        "UPDATE control_plane.agent_draft_revisions SET system_prompt='changed' WHERE agent_id=?",
                                        id))
                .hasMessageContaining("immutable release record");
        assertThatThrownBy(
                        () ->
                                sql.update(
                                        "DELETE FROM control_plane.agent_draft_revisions WHERE agent_id=?",
                                        id))
                .hasMessageContaining("immutable release record");
        sql.update(
                "INSERT INTO business.conversations(id,tenant,subject,release_hash,agent_id) VALUES('conversation',?,?,?,?)",
                owner.tenant(),
                owner.subject(),
                draft.baseReleaseHash(),
                id);
        sql.update(
                "INSERT INTO business.turns(id,conversation_id,tenant,subject,idempotency_key,request_hash,message,release_hash,status,agent_id)"
                        + " VALUES('turn','conversation',?,?,'key','request','message',?,'queued',?)",
                owner.tenant(),
                owner.subject(),
                draft.baseReleaseHash(),
                id);
        assertThatThrownBy(
                        () ->
                                sql.update(
                                        "UPDATE business.conversations SET agent_id='finance_agent' WHERE id='conversation'"))
                .hasMessageContaining("conversation release is immutable");
        assertThatThrownBy(
                        () ->
                                sql.update(
                                        "UPDATE business.turns SET agent_id='finance_agent' WHERE id='turn'"))
                .hasMessageContaining("turn target is immutable");
        assertThatThrownBy(
                        () ->
                                sql.update(
                                        "UPDATE business.turns SET release_hash='different' WHERE id='turn'"))
                .hasMessageContaining("turn target is immutable");
    }

    @Test
    void restartResolvesBothPublishedAgentAndLegacyWithoutCurrentEnvironment() throws Exception {
        String id = id();
        agents.create(owner, id, "Agent", "saved prompt");
        var published = agents.publish(owner, id, 1, 0);
        var stored = releases.resolve(published.releaseHash());
        var freshSession = session();
        var reopened =
                ReleaseService.open(
                        new MyBatisReleaseRepository(freshSession.getMapper(ReleaseMapper.class)),
                        tx);
        var restarted =
                new AgentService(
                        new MyBatisAgentRepository(freshSession.getMapper(AgentMapper.class)),
                        reopened,
                        tx);
        assertThat(reopened.resolve(published.releaseHash())).isEqualTo(stored);
        assertThat(restarted.resolveTarget(owner, id))
                .isEqualTo(new ResolvedTarget(id, published.releaseHash()));
        assertThat(reopened.require(reopened.currentHash())).isEqualTo(definition("legacy-v1"));
    }

    private void assertConcurrentOutcomes(Callable<?> first, Callable<?> second, String conflict)
            throws Exception {
        var barrier = new CyclicBarrier(2);
        try (var pool = Executors.newFixedThreadPool(2)) {
            var a = pool.submit(() -> outcome(first, barrier));
            var b = pool.submit(() -> outcome(second, barrier));
            assertThat(List.of(a.get(10, TimeUnit.SECONDS), b.get(10, TimeUnit.SECONDS)))
                    .containsExactlyInAnyOrder("success", conflict);
        }
    }

    private static String outcome(Callable<?> action, CyclicBarrier barrier) throws Exception {
        barrier.await(5, TimeUnit.SECONDS);
        try {
            action.call();
            return "success";
        } catch (Problem conflict) {
            return conflict.getMessage();
        }
    }

    private static AgentReleaseManifest manifest(
            String id, String base, String prompt, String runtime) {
        return new AgentReleaseManifest(
                AgentReleaseManifest.FORMAT,
                id,
                "tenant",
                "owner",
                1,
                "Agent",
                prompt,
                base,
                runtime);
    }

    private static long count(String table) {
        return sql.queryForObject("SELECT count(*) FROM " + table, Long.class);
    }

    private static String id() {
        return UUID.randomUUID().toString();
    }

    private static ReleaseDefinition definition(String version) {
        return new ReleaseDefinition(
                version,
                "finance_agent",
                ReleaseDefinition.SCOPES,
                ReleaseDefinition.OFFLINE,
                null,
                12,
                32,
                new BusinessFeatures(List.of(), "test-skills", "{}", null));
    }

    private static SqlSessionTemplate session() throws Exception {
        var configuration = new Configuration();
        configuration.setMapUnderscoreToCamelCase(true);
        configuration.setCacheEnabled(false);
        configuration.setLocalCacheScope(LocalCacheScope.STATEMENT);
        configuration.addMapper(AgentMapper.class);
        configuration.addMapper(ReleaseMapper.class);
        var factory = new SqlSessionFactoryBean();
        factory.setDataSource(source);
        factory.setConfiguration(configuration);
        return new SqlSessionTemplate(factory.getObject());
    }
}
