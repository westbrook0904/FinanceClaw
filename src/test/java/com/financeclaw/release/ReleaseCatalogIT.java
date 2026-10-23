package com.financeclaw.release;

import static org.assertj.core.api.Assertions.*;
import static org.mockito.ArgumentMatchers.*;
import static org.mockito.Mockito.*;

import com.financeclaw.app.release.ReleaseIdentity;
import com.financeclaw.app.release.ReleaseService;
import com.financeclaw.domain.Governance.Blocked;
import com.financeclaw.domain.Task.Problem;
import com.financeclaw.domain.release.BusinessFeatures;
import com.financeclaw.domain.release.Deployment;
import com.financeclaw.domain.release.DeploymentChange;
import com.financeclaw.domain.release.ReleaseDefinition;
import com.financeclaw.infrastructure.release.MyBatisReleaseRepository;
import com.financeclaw.infrastructure.release.ReleaseMapper;
import com.financeclaw.support.PayloadJson;
import java.util.List;
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

/** 真实 PostgreSQL 发布恢复、并发切换与不可变性验证，不使用内存发布目录。 */
class ReleaseCatalogIT {
    private static final PostgreSQLContainer<?> PG =
            new PostgreSQLContainer<>("postgres:16.10-alpine")
                    .withDatabaseName("releases")
                    .withUsername("releases")
                    .withPassword("test-only");
    private static DataSource source;
    private static JdbcTemplate sql;
    private TransactionTemplate tx;

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
    void reset() {
        sql.execute(
                "TRUNCATE control_plane.deployment_changes,control_plane.deployments,"
                        + "control_plane.release_manifests CASCADE");
        tx = new TransactionTemplate(new DataSourceTransactionManager(source));
    }

    @Test
    void restartRegistersChangedConfigurationWithoutReplacingPersistentPointerOrHistory() {
        var original = definition("v1");
        var service = service(original);
        String oldHash = service.currentHash();
        var published = definition("v2");
        service.publish(published);
        var selected = service.current();

        var startupConfiguration = definition("v3-from-env");
        var restarted = service(startupConfiguration);
        assertThat(restarted.current()).isEqualTo(selected);
        assertThat(restarted.require(oldHash)).isEqualTo(original);
        assertThat(restarted.require(ReleaseIdentity.hash(startupConfiguration)))
                .isEqualTo(startupConfiguration);
        assertThat(restarted.history(0, 100))
                .containsExactly(
                        new DeploymentChange("default", 1, oldHash, null),
                        new DeploymentChange("default", 2, selected.releaseHash(), oldHash));
        assertThat(restarted.activate(oldHash, 2)).isEqualTo(new Deployment("default", oldHash, 3));
        assertThat(restarted.history(2, 1))
                .containsExactly(
                        new DeploymentChange("default", 3, oldHash, selected.releaseHash()));
    }

    @Test
    void concurrentBootstrapsOnlyCreateOnePointerAndOneInitialReceipt() throws Exception {
        var barrier = new CyclicBarrier(2);
        try (var workers = Executors.newFixedThreadPool(2)) {
            var first = workers.submit(() -> bootstrapAfterBarrier(barrier, "startup-one"));
            var second = workers.submit(() -> bootstrapAfterBarrier(barrier, "startup-two"));
            assertThat(first.get(10, TimeUnit.SECONDS)).isEqualTo(second.get(10, TimeUnit.SECONDS));
        }
        assertThat(
                        sql.queryForObject(
                                "SELECT count(*) FROM control_plane.release_manifests", Long.class))
                .isEqualTo(2L);
        assertThat(
                        sql.queryForObject(
                                "SELECT count(*) FROM control_plane.deployment_changes",
                                Long.class))
                .isEqualTo(1L);
    }

    @Test
    void concurrentPublishersUsingSameRevisionHaveExactlyOneWinner() throws Exception {
        var service = service(definition("v1"));
        String firstHash = service.register(definition("candidate-one"));
        String secondHash = service.register(definition("candidate-two"));
        var otherProcess = service(definition("v1"));
        var barrier = new CyclicBarrier(2);
        try (var workers = Executors.newFixedThreadPool(2)) {
            var first = workers.submit(() -> activateAfterBarrier(service, firstHash, barrier));
            var second =
                    workers.submit(() -> activateAfterBarrier(otherProcess, secondHash, barrier));
            assertThat(List.of(first.get(10, TimeUnit.SECONDS), second.get(10, TimeUnit.SECONDS)))
                    .containsExactlyInAnyOrder("published", "release_revision_changed");
        }
        assertThat(service.current().revision()).isEqualTo(2);
        assertThat(service.history(0, 100)).hasSize(2);
        assertThat(service.currentHash()).isIn(firstHash, secondHash);
    }

    @Test
    void failedAuditWriteRollsBackPointerChange() throws Exception {
        var repository = spy(repository());
        var service = new ReleaseService(() -> definition("v1"), repository, tx);
        var previous = service.current();
        String next = service.register(definition("v2"));
        doThrow(new IllegalStateException("audit_write_failed"))
                .when(repository)
                .insertChange(argThat(change -> change.revision() == 2));
        assertThatThrownBy(() -> service.activate(next, 1)).hasMessage("audit_write_failed");
        assertThat(service.current()).isEqualTo(previous);
        assertThat(service.history(0, 100)).hasSize(1);
    }

    @Test
    void manifestsAndHistoryCannotBeUpdatedOrDeletedEvenByTheirOwner() {
        service(definition("v1"));
        assertThatThrownBy(
                        () ->
                                sql.update(
                                        "UPDATE control_plane.release_manifests SET manifest='{}'"))
                .hasMessageContaining("immutable release record");
        assertThatThrownBy(() -> sql.update("DELETE FROM control_plane.release_manifests"))
                .hasMessageContaining("immutable release record");
        assertThatThrownBy(
                        () -> sql.update("UPDATE control_plane.deployment_changes SET revision=2"))
                .hasMessageContaining("immutable release record");
        assertThatThrownBy(() -> sql.update("DELETE FROM control_plane.deployment_changes"))
                .hasMessageContaining("immutable release record");
    }

    @Test
    void unknownOrMismatchedManifestIsBlockedWithoutFabricatingHistoricalVersions() {
        var service = service(definition("v1"));
        String unknown = "f".repeat(64);
        assertThatThrownBy(() -> service.require(unknown))
                .isInstanceOf(Blocked.class)
                .hasMessage("release_unavailable");
        sql.update(
                "INSERT INTO control_plane.release_manifests(release_hash,manifest) VALUES(?,?::jsonb)",
                unknown,
                PayloadJson.canonical(definition("not-the-requested-hash")));
        assertThatThrownBy(() -> service.activate(unknown, 1)).hasMessage("release_unavailable");
        assertThat(service.current().revision()).isEqualTo(1);
        assertThat(service.history(0, 100)).hasSize(1);
    }

    @Test
    void corruptedExistingManifestIsNotReplacedByStartupRegistration() {
        var initial = definition("v1");
        String hash = ReleaseIdentity.hash(initial);
        sql.update(
                "INSERT INTO control_plane.release_manifests(release_hash,manifest) VALUES(?,?::jsonb)",
                hash,
                PayloadJson.canonical(definition("corrupted")));
        assertThatThrownBy(() -> service(initial)).hasMessage("release_unavailable");
        assertThat(sql.queryForObject("SELECT count(*) FROM control_plane.deployments", Long.class))
                .isZero();
    }

    @Test
    void malformedPersistedManifestCannotBecomeAnExecutionDefinition() {
        var service = service(definition("v1"));
        String invalid = "a".repeat(64);
        sql.update(
                "INSERT INTO control_plane.release_manifests(release_hash,manifest) VALUES(?,'{}')",
                invalid);
        assertThatThrownBy(() -> service.require(invalid))
                .isInstanceOf(Blocked.class)
                .hasMessage("release_unavailable");
    }

    private Deployment bootstrapAfterBarrier(CyclicBarrier barrier, String version)
            throws Exception {
        barrier.await(5, TimeUnit.SECONDS);
        return service(definition(version)).current();
    }

    private String activateAfterBarrier(ReleaseService service, String hash, CyclicBarrier barrier)
            throws Exception {
        barrier.await(5, TimeUnit.SECONDS);
        try {
            service.activate(hash, 1);
            return "published";
        } catch (Problem conflict) {
            return conflict.getMessage();
        }
    }

    private ReleaseService service(ReleaseDefinition definition) {
        try {
            return new ReleaseService(() -> definition, repository(), tx);
        } catch (RuntimeException failure) {
            throw failure;
        } catch (Exception failure) {
            throw new IllegalStateException(failure);
        }
    }

    private static MyBatisReleaseRepository repository() throws Exception {
        var configuration = new Configuration();
        configuration.setMapUnderscoreToCamelCase(true);
        configuration.setCacheEnabled(false);
        configuration.setLocalCacheScope(LocalCacheScope.STATEMENT);
        configuration.addMapper(ReleaseMapper.class);
        var factory = new SqlSessionFactoryBean();
        factory.setDataSource(source);
        factory.setConfiguration(configuration);
        return new MyBatisReleaseRepository(
                new SqlSessionTemplate(factory.getObject()).getMapper(ReleaseMapper.class));
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
}
