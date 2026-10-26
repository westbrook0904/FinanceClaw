package com.financeclaw.agent;

import static org.assertj.core.api.Assertions.*;

import com.financeclaw.app.agent.AgentService;
import com.financeclaw.app.release.ReleaseService;
import com.financeclaw.domain.Task.Owner;
import com.financeclaw.domain.Task.Problem;
import com.financeclaw.domain.agent.AgentDefinition;
import com.financeclaw.domain.authorization.ResourceGrant;
import com.financeclaw.domain.release.*;
import com.financeclaw.infrastructure.agent.*;
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

/** 真实 PG 证明授权修订、审计原子性、caller/publisher 分离和撤销的后续边界生效。 */
class ResourceAuthorizationIT {
    private static final PostgreSQLContainer<?> PG =
            new PostgreSQLContainer<>("postgres:16.10-alpine")
                    .withDatabaseName("authorization")
                    .withUsername("authorization")
                    .withPassword("test-only");
    private static DataSource source;
    private static JdbcTemplate sql;
    private final Owner publisher = new Owner("publisher-tenant", "publisher");
    private final Owner caller = new Owner("caller-tenant", "caller");
    private TransactionTemplate tx;
    private AgentMapper agentMapper;
    private ResourceAccessMapper mapper;
    private ReleaseService releases;
    private AgentService agents;
    private ResourceAccessService access;

    @BeforeAll
    static void start() {
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
                "TRUNCATE control_plane.resource_grants,control_plane.resource_grant_audit,"
                        + "business.conversations,control_plane.agents,control_plane.deployments,"
                        + "control_plane.deployment_changes,control_plane.release_manifests CASCADE");
        tx = new TransactionTemplate(new DataSourceTransactionManager(source));
        var config = new Configuration();
        config.setMapUnderscoreToCamelCase(true);
        config.setCacheEnabled(false);
        config.setLocalCacheScope(LocalCacheScope.STATEMENT);
        config.addMapper(AgentMapper.class);
        config.addMapper(ResourceAccessMapper.class);
        config.addMapper(ReleaseMapper.class);
        var factory = new SqlSessionFactoryBean();
        factory.setDataSource(source);
        factory.setConfiguration(config);
        var session = new SqlSessionTemplate(factory.getObject());
        agentMapper = session.getMapper(AgentMapper.class);
        mapper = session.getMapper(ResourceAccessMapper.class);
        releases =
                new ReleaseService(
                        () -> definition(),
                        new MyBatisReleaseRepository(session.getMapper(ReleaseMapper.class)),
                        tx);
        access =
                new ResourceAccessService(
                        mapper, agentMapper, releases, tx, catalog("SEARCH_TOKEN"));
        agents = new AgentService(new MyBatisAgentRepository(agentMapper), releases, tx);
        agents.configureAgentAccess(access);
    }

    @Test
    void sharingPreservesCallerDataIdentityAndNeverGrantsManagement() {
        String id = create();
        String hash = agents.publish(publisher, id, 1, 0).releaseHash();
        assertThatThrownBy(() -> access.requireInvocation(caller, id, hash))
                .hasMessage("agent_access_denied");
        var grant = access.change(publisher, "agent", id, caller, hash, 0, true);
        var authorization = access.requireInvocation(caller, id, hash);
        assertThat(authorization.resourceOwner()).isEqualTo(publisher);
        assertThat(authorization.grantRevision()).isEqualTo(grant.revision());
        assertThat(authorization.resolvedRelease()).isEqualTo(releases.resolve(hash));
        assertThat(agents.resolveTarget(caller, id).releaseHash()).isEqualTo(hash);
        assertThat(access.listSharedTargets(caller))
                .singleElement()
                .satisfies(
                        target -> {
                            assertThat(target.resourceOwner()).isEqualTo(publisher);
                            assertThat(target.releaseHash()).isEqualTo(hash);
                        });
        assertThatThrownBy(() -> agents.get(caller, id)).hasMessage("not_found");
        assertThatThrownBy(() -> agents.update(caller, id, 1, "Hijack", "Hijack"))
                .hasMessage("not_found");
        assertThatThrownBy(() -> agents.publish(caller, id, 1, 1)).hasMessage("not_found");
        assertThatThrownBy(
                        () ->
                                access.change(
                                        caller, "agent", id, new Owner("x", "y"), hash, 0, true))
                .hasMessage("not_found");
        assertThat(access.requireInvocation(publisher, id, hash).grantRevision()).isZero();
    }

    @Test
    void exactReleaseGrantDoesNotSilentlyAuthorizeANewDeploymentAndRevocationAppliesImmediately() {
        String id = create();
        String first = agents.publish(publisher, id, 1, 0).releaseHash();
        access.change(publisher, "agent", id, caller, first, 0, true);
        agents.update(publisher, id, 1, "Agent", "New prompt");
        String second = agents.publish(publisher, id, 2, 1).releaseHash();
        assertThat(access.requireInvocation(caller, id, first).resourceOwner())
                .isEqualTo(publisher);
        assertThatThrownBy(() -> access.requireInvocation(caller, id, second))
                .hasMessage("agent_access_denied");
        assertThatThrownBy(() -> agents.resolveTarget(caller, id))
                .isInstanceOf(Problem.class)
                .hasMessage("not_found");
        access.change(publisher, "agent", id, caller, first, 1, false);
        assertThatThrownBy(() -> access.requireInvocation(caller, id, first))
                .hasMessage("agent_access_denied");
        assertThat(access.listSharedTargets(caller)).isEmpty();
        assertThat(access.history(publisher, "agent", id, caller, 0, 50))
                .extracting(ResourceGrant::revision)
                .containsExactly(1L, 2L);
        access.change(publisher, "agent", id, caller, second, 2, true);
        assertThat(access.requireInvocation(caller, id, second).grantRevision()).isEqualTo(3);
        assertThatThrownBy(() -> access.requireInvocation(caller, id, first))
                .hasMessage("agent_access_denied");
    }

    @Test
    void grantCasHasOneWinnerAndEverySuccessfulRevisionHasAnImmutableAudit() throws Exception {
        String id = create();
        String hash = agents.publish(publisher, id, 1, 0).releaseHash();
        access.change(publisher, "agent", id, caller, hash, 0, true);
        var gate = new CyclicBarrier(2);
        try (var pool = Executors.newFixedThreadPool(2)) {
            Callable<String> update =
                    () -> {
                        gate.await(5, TimeUnit.SECONDS);
                        try {
                            access.change(publisher, "agent", id, caller, hash, 1, false);
                            return "success";
                        } catch (Problem conflict) {
                            return conflict.getMessage();
                        }
                    };
            var a = pool.submit(update);
            var b = pool.submit(update);
            assertThat(List.of(a.get(10, TimeUnit.SECONDS), b.get(10, TimeUnit.SECONDS)))
                    .containsExactlyInAnyOrder("success", "resource_grant_revision_changed");
        }
        assertThat(access.history(publisher, "agent", id, caller, 0, 50))
                .extracting(ResourceGrant::revision)
                .containsExactly(1L, 2L);
        assertThatThrownBy(
                        () ->
                                sql.update(
                                        "UPDATE control_plane.resource_grant_audit SET enabled=true"))
                .isInstanceOf(org.springframework.dao.DataAccessException.class);
        assertThatThrownBy(() -> sql.update("DELETE FROM control_plane.resource_grant_audit"))
                .isInstanceOf(org.springframework.dao.DataAccessException.class);
    }

    @Test
    void auditFailureRollsBackGrantMutation() {
        String id = create();
        String hash = agents.publish(publisher, id, 1, 0).releaseHash();
        sql.execute(
                "CREATE FUNCTION control_plane.fail_authorization_audit() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN RAISE EXCEPTION 'audit unavailable'; END $$");
        sql.execute(
                "CREATE TRIGGER fail_authorization_audit BEFORE INSERT ON control_plane.resource_grant_audit FOR EACH ROW EXECUTE FUNCTION control_plane.fail_authorization_audit()");
        try {
            assertThatThrownBy(() -> access.change(publisher, "agent", id, caller, hash, 0, true))
                    .isInstanceOf(org.springframework.dao.DataAccessException.class);
            assertThat(mapper.grant("agent", id, caller)).isNull();
        } finally {
            sql.execute(
                    "DROP TRIGGER fail_authorization_audit ON control_plane.resource_grant_audit");
            sql.execute("DROP FUNCTION control_plane.fail_authorization_audit()");
        }
    }

    @Test
    void credentialResourceGrantIsSeparateFromAgentSharingAndPinnedToConfiguredReference() {
        assertThatThrownBy(() -> access.requireCredential(caller, "SEARCH_TOKEN"))
                .hasMessage("credential_access_denied");
        access.requireCredential(publisher, "SEARCH_TOKEN");
        access.requireCredential(caller, "");
        assertThatThrownBy(() -> access.requireCredential(publisher, "UNREGISTERED_TOKEN"))
                .hasMessage("credential_access_denied");
        access.change(publisher, "credential", "search", caller, null, 0, true);
        access.requireCredential(caller, "SEARCH_TOKEN");
        assertThatThrownBy(() -> access.requireModel(caller))
                .hasMessage("credential_access_denied");
        access.change(publisher, "credential", "model", caller, null, 0, true);
        access.requireModel(caller);
        var rebound =
                new ResourceAccessService(
                        mapper, agentMapper, releases, tx, catalog("OTHER_TOKEN"));
        assertThatThrownBy(() -> rebound.requireCredential(caller, "OTHER_TOKEN"))
                .hasMessage("credential_access_denied");
        rebound.change(publisher, "credential", "search", caller, null, 1, true);
        rebound.requireCredential(caller, "OTHER_TOKEN");
        rebound.change(publisher, "credential", "search", caller, null, 2, false);
        assertThatThrownBy(() -> rebound.requireCredential(caller, "OTHER_TOKEN"))
                .hasMessage("credential_access_denied");
        assertThatThrownBy(
                        () ->
                                access.change(
                                        caller, "credential", "search", publisher, null, 0, true))
                .hasMessage("not_found");
        assertThatThrownBy(
                        () ->
                                access.change(
                                        publisher,
                                        "credential",
                                        "not-registered",
                                        caller,
                                        null,
                                        0,
                                        true))
                .hasMessage("not_found");
    }

    @Test
    void archiveRequiresOwnerAndDraftCasAndClosesExistingInvocationsWithoutDeletingHistory() {
        String id = create();
        String hash = agents.publish(publisher, id, 1, 0).releaseHash();
        access.change(publisher, "agent", id, caller, hash, 0, true);
        assertThatThrownBy(() -> access.archive(caller, id, 1)).hasMessage("not_found");
        assertThatThrownBy(() -> access.archive(publisher, id, 2))
                .hasMessage("agent_draft_revision_changed");
        access.archive(publisher, id, 1);
        assertThatThrownBy(() -> access.requireInvocation(publisher, id, hash))
                .hasMessage("agent_access_denied");
        assertThatThrownBy(() -> access.requireInvocation(caller, id, hash))
                .hasMessage("agent_access_denied");
        assertThatThrownBy(() -> agents.get(publisher, id)).hasMessage("not_found");
        assertThatThrownBy(() -> agents.create(publisher, id, "Agent", "Prompt"))
                .hasMessage("not_found");
        assertThat(agents.list(publisher))
                .extracting(AgentDefinition::id)
                .containsExactly("finance_agent");
        assertThat(access.listSharedTargets(caller)).isEmpty();
        assertThat(releases.resolve(hash).agentId()).isEqualTo(id);
        assertThat(
                        sql.queryForObject(
                                "SELECT count(*) FROM control_plane.resource_grant_audit",
                                Long.class))
                .isEqualTo(1);
    }

    @Test
    void releaseOfAnotherPublisherCannotBeGrantedUnderThisResourceIdentity() {
        String id = create();
        agents.publish(publisher, id, 1, 0);
        String other = UUID.randomUUID().toString();
        agents.create(caller, other, "Other", "Prompt");
        String hash = agents.publish(caller, other, 1, 0).releaseHash();
        assertThatThrownBy(() -> access.change(publisher, "agent", id, caller, hash, 0, true))
                .hasMessage("agent_identity_mismatch");
        assertThat(mapper.grant("agent", id, caller)).isNull();
        assertThatThrownBy(() -> access.change(publisher, "agent", id, publisher, hash, 0, true))
                .isInstanceOf(RuntimeException.class);
    }

    @Test
    void checkpointAuthorizationKeepsAgentGrantStableUntilCommit() throws Exception {
        String id = create();
        String hash = agents.publish(publisher, id, 1, 0).releaseHash();
        access.change(publisher, "agent", id, caller, hash, 0, true);
        mutationWaitsForAuthorizedTransaction(
                () -> access.requireInvocation(caller, id, hash),
                () -> access.change(publisher, "agent", id, caller, hash, 1, false));
        assertThatThrownBy(() -> access.requireInvocation(caller, id, hash))
                .hasMessage("agent_access_denied");
    }

    @Test
    void checkpointAuthorizationKeepsOwnerAgentActiveUntilCommit() throws Exception {
        String id = create();
        String hash = agents.publish(publisher, id, 1, 0).releaseHash();
        mutationWaitsForAuthorizedTransaction(
                () -> access.requireInvocation(publisher, id, hash),
                () -> access.archive(publisher, id, 1));
        assertThatThrownBy(() -> access.requireInvocation(publisher, id, hash))
                .hasMessage("agent_access_denied");
    }

    @Test
    void checkpointAuthorizationKeepsCredentialGrantStableUntilCommit() throws Exception {
        access.change(publisher, "credential", "search", caller, null, 0, true);
        mutationWaitsForAuthorizedTransaction(
                () -> access.requireCredential(caller, "SEARCH_TOKEN"),
                () -> access.change(publisher, "credential", "search", caller, null, 1, false));
        assertThatThrownBy(() -> access.requireCredential(caller, "SEARCH_TOKEN"))
                .hasMessage("credential_access_denied");
    }

    /** Observe PostgreSQL's actual blocker relationship, not a timing-only Future assertion. */
    private void mutationWaitsForAuthorizedTransaction(Runnable authorize, Runnable mutation)
            throws Exception {
        var readerPid = new CompletableFuture<Integer>();
        var writerPid = new CompletableFuture<Integer>();
        var release = new CountDownLatch(1);
        try (var pool = Executors.newFixedThreadPool(2)) {
            var reader =
                    pool.submit(
                            () ->
                                    tx.executeWithoutResult(
                                            status -> {
                                                authorize.run();
                                                readerPid.complete(
                                                        sql.queryForObject(
                                                                "SELECT pg_backend_pid()",
                                                                Integer.class));
                                                try {
                                                    assertThat(release.await(10, TimeUnit.SECONDS))
                                                            .isTrue();
                                                } catch (InterruptedException interrupted) {
                                                    Thread.currentThread().interrupt();
                                                    throw new AssertionError(interrupted);
                                                }
                                                // A local effect/checkpoint would commit under the
                                                // same retained authorization.
                                                authorize.run();
                                            }));
            try {
                int holder = readerPid.get(5, TimeUnit.SECONDS);
                var writer =
                        pool.submit(
                                () ->
                                        tx.executeWithoutResult(
                                                status -> {
                                                    writerPid.complete(
                                                            sql.queryForObject(
                                                                    "SELECT pg_backend_pid()",
                                                                    Integer.class));
                                                    mutation.run();
                                                }));
                int waiter = writerPid.get(5, TimeUnit.SECONDS);
                long deadline = System.nanoTime() + TimeUnit.SECONDS.toNanos(5);
                boolean blocked;
                do {
                    blocked =
                            Boolean.TRUE.equals(
                                    sql.queryForObject(
                                            "SELECT ?=ANY(pg_blocking_pids(?))",
                                            Boolean.class,
                                            holder,
                                            waiter));
                    if (!blocked) Thread.sleep(10);
                } while (!blocked && !writer.isDone() && System.nanoTime() < deadline);
                assertThat(blocked)
                        .as("resource mutation must wait for the authorized checkpoint transaction")
                        .isTrue();
                release.countDown();
                reader.get(5, TimeUnit.SECONDS);
                writer.get(5, TimeUnit.SECONDS);
            } finally {
                release.countDown();
            }
        }
    }

    private String create() {
        String id = UUID.randomUUID().toString();
        agents.create(publisher, id, "Agent", "Prompt");
        return id;
    }

    private ServerCredentialCatalog catalog(String reference) {
        return new ServerCredentialCatalog(
                """
            [{"resourceId":"search","reference":"%s","tenant":"publisher-tenant","subject":"publisher"},
             {"resourceId":"model","reference":"@model","tenant":"publisher-tenant","subject":"publisher"}]
            """
                        .formatted(reference),
                publisher,
                "[{}]");
    }

    private static ReleaseDefinition definition() {
        return new ReleaseDefinition(
                "authorization-v1",
                "finance_agent",
                List.of(),
                ReleaseDefinition.OFFLINE,
                null,
                12,
                32,
                new BusinessFeatures(List.of(), "test-skills", "{}", null));
    }
}
