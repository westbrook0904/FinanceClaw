package com.financeclaw.m4;

import static org.assertj.core.api.Assertions.*;
import static org.awaitility.Awaitility.await;

import com.financeclaw.app.memory.*;
import com.financeclaw.app.memory.port.MemoryRepository;
import com.financeclaw.app.task.TaskEventHub;
import com.financeclaw.domain.ExecutionScope;
import com.financeclaw.domain.Memory.Privacy;
import com.financeclaw.domain.Task.Owner;
import com.financeclaw.domain.Task.Turn;
import com.financeclaw.domain.memory.MemoryPolicy;
import com.financeclaw.domain.memory.UserProfile;
import com.financeclaw.infrastructure.memory.*;
import java.nio.file.*;
import java.time.Duration;
import java.util.List;
import java.util.Properties;
import java.util.UUID;
import java.util.concurrent.*;
import org.apache.ibatis.session.Configuration;
import org.apache.ibatis.session.LocalCacheScope;
import org.flywaydb.core.Flyway;
import org.junit.jupiter.api.*;
import org.junit.jupiter.api.io.TempDir;
import org.mybatis.spring.SqlSessionFactoryBean;
import org.mybatis.spring.SqlSessionTemplate;
import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.jdbc.datasource.DriverManagerDataSource;
import org.springframework.jdbc.support.JdbcTransactionManager;
import org.springframework.transaction.support.TransactionTemplate;
import org.testcontainers.containers.PostgreSQLContainer;

/** 真实目录、独立服务实例及两个 PostgreSQL 验证共享采样和备份恢复的隐私边界。 */
class ProfileDirectoryRecoveryIT {
    private static final PostgreSQLContainer<?> PG =
            new PostgreSQLContainer<>("postgres:16.10-alpine");
    private static final PostgreSQLContainer<?> LEDGER_PG =
            new PostgreSQLContainer<>("postgres:16.10-alpine");
    private static final Owner ALICE = new Owner("tenant", "alice");
    private static final Owner BOB = new Owner("tenant", "bob");
    private static final MemoryPolicy PROFILE_ONLY =
            new MemoryPolicy(false, false, "disabled", true);
    private static final String FIRST_INSTANCE = "profile-directory-first";
    private static final String SECOND_INSTANCE = "profile-directory-second";
    private static final String BACKUP = "/tmp/profile-directory-before-forget.dump";
    private static Database database;
    private static PostgresForgetLedger ledger;

    @TempDir Path root;
    private DirectoryProfileDocuments documents;
    private UserProfileService profiles;
    private MemoryService memory;
    private PrivacyRecoveryService recovery;

    @BeforeAll
    static void databases() throws Exception {
        PG.start();
        LEDGER_PG.start();
        var source = source(PG);
        Flyway.configure().dataSource(source).load().migrate();
        database = database(source);
        var ledgerSource = source(LEDGER_PG);
        new JdbcTemplate(ledgerSource)
                .execute(
                        Files.readString(
                                Path.of(
                                        "src/main/resources/privacy-ledger/V1__forget_ledger.sql")));
        ledger = new PostgresForgetLedger(ledgerSource);
    }

    @AfterAll
    static void stop() {
        if (ledger != null) {
            ledger.close();
        }
        LEDGER_PG.stop();
        PG.stop();
    }

    @BeforeEach
    void reset() {
        database.sql().execute("TRUNCATE business.conversations,agent_runtime.states CASCADE");
        database.sql()
                .execute(
                        "TRUNCATE memory.owners,memory.jobs,memory.operations,memory.audit CASCADE");
        database.sql()
                .update(
                        "UPDATE memory.privacy_ledger_binding SET lineage=?,active=false",
                        UUID.randomUUID().toString());
        documents = new DirectoryProfileDocuments(root);
        profiles = profiles(database, documents);
        var forgetting =
                new MemoryForgetService(
                        database.memory(),
                        database.tx(),
                        database.recovery(),
                        ledger,
                        new TaskEventHub(),
                        documents);
        memory =
                new MemoryService(
                        database.memory(), database.tx(), PROFILE_ONLY, forgetting, profiles);
        recovery =
                new PrivacyRecoveryService(
                        database.memory(), database.recovery(), ledger, database.tx(), documents);
    }

    @Test
    void twoServiceInstancesSharingDirectoryPublishOneRevisionForConcurrentReads()
            throws Exception {
        profiles.read(ALICE);
        String markdown = "# 共享服务器画像\n- 回答偏好：简洁中文\n";
        Files.writeString(path(ALICE, 1), markdown);
        var firstSource = namedSource(FIRST_INSTANCE);
        var secondSource = namedSource(SECOND_INSTANCE);
        var first = profiles(database(firstSource), new DirectoryProfileDocuments(root));
        var second = profiles(database(secondSource), new DirectoryProfileDocuments(root));
        var ready = new CountDownLatch(2);
        var start = new CountDownLatch(1);

        try (var executor = Executors.newFixedThreadPool(2)) {
            var firstRead =
                    executor.submit(
                            () -> {
                                ready.countDown();
                                assertThat(start.await(10, TimeUnit.SECONDS)).isTrue();
                                return first.read(ALICE);
                            });
            var secondRead =
                    executor.submit(
                            () -> {
                                ready.countDown();
                                assertThat(start.await(10, TimeUnit.SECONDS)).isTrue();
                                return second.read(ALICE);
                            });
            assertThat(ready.await(10, TimeUnit.SECONDS)).isTrue();
            database.tx()
                    .executeWithoutResult(
                            status -> {
                                database.memory().lock(ALICE);
                                start.countDown();
                                // 先确认两个独立连接都已等待同一用户行，避免串行调度伪装成并发测试。
                                await().atMost(Duration.ofSeconds(10))
                                        .pollInterval(Duration.ofMillis(25))
                                        .untilAsserted(
                                                () ->
                                                        assertThat(
                                                                        database.sql()
                                                                                .queryForObject(
                                                                                        "SELECT count(*) FROM pg_stat_activity"
                                                                                                + " WHERE datname=current_database()"
                                                                                                + " AND application_name IN (?,?)"
                                                                                                + " AND wait_event_type='Lock'",
                                                                                        Integer
                                                                                                .class,
                                                                                        FIRST_INSTANCE,
                                                                                        SECOND_INSTANCE))
                                                                .isEqualTo(2));
                            });
            var expected = new UserProfile(1, 1, markdown);
            assertThat(firstRead.get(10, TimeUnit.SECONDS)).isEqualTo(expected);
            assertThat(secondRead.get(10, TimeUnit.SECONDS)).isEqualTo(expected);
            // 两实例再次读取稳定文件，仍不能写镜像、推进版本或制造重复审计。
            assertThat(first.read(ALICE)).isEqualTo(expected);
            assertThat(second.read(ALICE)).isEqualTo(expected);
            assertThat(database.memory().userProfile(ALICE)).isEqualTo(expected);
            assertThat(memory.privacy(ALICE).profileRevision()).isEqualTo(1);
            assertThat(
                            database.sql()
                                    .queryForObject(
                                            "SELECT count(*) FROM memory.audit WHERE tenant=? AND subject=?"
                                                    + " AND action='profile_file_update'",
                                            Integer.class,
                                            ALICE.tenant(),
                                            ALICE.subject()))
                    .isEqualTo(1);
            assertThat(
                            database.sql()
                                    .queryForObject(
                                            "SELECT count(*) FROM memory.user_profiles",
                                            Integer.class))
                    .isEqualTo(1);
        }
    }

    @Test
    void independentLedgerReplayErasesRestoredOldDirectoryAndPreservesNewEpoch() throws Exception {
        assertThat(recovery.bootstrap().facts()).isZero();
        profiles.read(ALICE);
        profiles.read(BOB);
        Files.writeString(path(ALICE, 1), "Alice before backup");
        Files.writeString(path(BOB, 1), "Bob keeps his profile");
        var oldTurn = admit("old", ALICE);
        var otherTurn = admit("other", BOB);
        Path savedProfile = root.resolve("alice-profile-backup.md");
        Files.copy(path(ALICE, 1), savedProfile);
        command("pg_dump", "-U", PG.getUsername(), "-d", PG.getDatabaseName(), "-Fc", "-f", BACKUP);

        assertThat(memory.forget(ALICE, 1, "after-backup")).isEqualTo(2);
        assertThat(path(ALICE, 1).getParent()).doesNotExist();
        profiles.read(ALICE);
        Files.writeString(path(ALICE, 2), "Alice new epoch profile");
        admit("before-restore", ALICE);
        var authority = ledger.snapshot(database.recovery().binding().lineage());
        assertThat(authority).hasSize(1);
        assertThat(authority.getFirst().fromEpoch()).isEqualTo(1);

        // 真正恢复旧业务库和旧文件；账本留在另一个容器，新 epoch 目录也独立保留。
        database.sql().execute("DROP SCHEMA memory,channel,agent_runtime,business CASCADE");
        command(
                "pg_restore",
                "-U",
                PG.getUsername(),
                "-d",
                PG.getDatabaseName(),
                "--clean",
                "--if-exists",
                BACKUP);
        Files.createDirectories(path(ALICE, 1).getParent());
        Files.copy(savedProfile, path(ALICE, 1));
        assertThat(database.memory().userProfile(ALICE).markdown())
                .isEqualTo("Alice before backup");
        assertThat(memory.privacy(ALICE).epoch()).isEqualTo(1);
        assertThat(path(ALICE, 2)).hasContent("Alice new epoch profile");
        assertThat(ledger.snapshot(database.recovery().binding().lineage())).isEqualTo(authority);

        var report = recovery.replay();
        assertThat(report.result()).isEqualTo("privacy_replayed");
        assertThat(report.facts()).isEqualTo(1);
        assertThat(path(ALICE, 1).getParent()).doesNotExist();
        assertThat(path(ALICE, 2)).hasContent("Alice new epoch profile");
        assertThat(path(BOB, 1)).hasContent("Bob keeps his profile");
        assertThat(memory.privacy(ALICE)).isEqualTo(new Privacy("tenant", "alice", 2, false, 0));
        assertThat(database.memory().userProfile(ALICE)).isNull();
        assertThat(database.memory().turnSnapshot(oldTurn.id(), ALICE)).isNull();
        assertThatThrownBy(() -> memory.context(oldTurn)).hasMessage("memory_privacy_changed");
        assertThat(memory.context(otherTurn)).contains("Bob keeps his profile");
        assertThat(database.recovery().unsafeRows()).isZero();

        var currentTurn = admit("after-replay", ALICE);
        assertThat(memory.context(currentTurn)).contains("Alice new epoch profile");
        var currentProfile = database.memory().userProfile(ALICE);
        var currentSnapshot = database.memory().turnSnapshot(currentTurn.id(), ALICE);
        long auditCount =
                database.sql().queryForObject("SELECT count(*) FROM memory.audit", Long.class);
        assertThat(currentProfile).isEqualTo(new UserProfile(2, 1, "Alice new epoch profile"));
        assertThat(currentSnapshot).isNotNull();

        // 历史事实重放始终使用 fromEpoch，不能把已采样的新画像和新 Turn 当成旧资料删除。
        assertThat(recovery.replay()).isEqualTo(report);
        assertThat(path(ALICE, 2)).hasContent("Alice new epoch profile");
        assertThat(database.memory().userProfile(ALICE)).isEqualTo(currentProfile);
        assertThat(database.memory().turnSnapshot(currentTurn.id(), ALICE))
                .isEqualTo(currentSnapshot);
        assertThat(database.sql().queryForObject("SELECT count(*) FROM memory.audit", Long.class))
                .isEqualTo(auditCount);
        assertThat(database.recovery().unsafeRows()).isZero();
        assertThat(ledger.snapshot(database.recovery().binding().lineage())).isEqualTo(authority);
    }

    private static DriverManagerDataSource source(PostgreSQLContainer<?> container) {
        return new DriverManagerDataSource(
                container.getJdbcUrl(), container.getUsername(), container.getPassword());
    }

    private static DriverManagerDataSource namedSource(String name) {
        var source = source(PG);
        var properties = new Properties();
        properties.setProperty("ApplicationName", name);
        source.setConnectionProperties(properties);
        return source;
    }

    private static Database database(DriverManagerDataSource source) throws Exception {
        var configuration = new Configuration();
        configuration.setMapUnderscoreToCamelCase(true);
        configuration.setCacheEnabled(false);
        configuration.setLocalCacheScope(LocalCacheScope.STATEMENT);
        configuration.addMapper(MemoryMapper.class);
        var factory = new SqlSessionFactoryBean();
        factory.setDataSource(source);
        factory.setConfiguration(configuration);
        var memory =
                new MyBatisMemoryRepository(
                        new SqlSessionTemplate(factory.getObject()).getMapper(MemoryMapper.class));
        return new Database(
                new JdbcTemplate(source),
                new TransactionTemplate(new JdbcTransactionManager(source)),
                memory,
                new JdbcForgetRecoveryRepository(source));
    }

    private static UserProfileService profiles(Database database, DirectoryProfileDocuments files) {
        return new UserProfileService(database.memory(), database.tx(), PROFILE_ONLY, files);
    }

    private Path path(Owner owner, long epoch) {
        return root.resolve(ExecutionScope.hash(List.of(owner.tenant(), owner.subject())))
                .resolve("epoch-" + epoch)
                .resolve("profile.md");
    }

    private Turn admit(String id, Owner owner) {
        String conversation = "conversation-" + id;
        database.sql()
                .update(
                        "INSERT INTO business.conversations(id,tenant,subject) VALUES(?,?,?)",
                        conversation,
                        owner.tenant(),
                        owner.subject());
        database.sql()
                .update(
                        "INSERT INTO business.turns(id,conversation_id,tenant,subject,idempotency_key,request_hash,message,release_hash,status)"
                                + " VALUES(?,?,?,?,?,'hash','request','release','running')",
                        id,
                        conversation,
                        owner.tenant(),
                        owner.subject(),
                        "key-" + id);
        var turn =
                new Turn(
                        id,
                        conversation,
                        owner.tenant(),
                        owner.subject(),
                        "key-" + id,
                        "hash",
                        "request",
                        "release",
                        "running",
                        1,
                        null,
                        null);
        database.tx().executeWithoutResult(status -> memory.admit(turn));
        return turn;
    }

    private static void command(String... arguments) throws Exception {
        var result = PG.execInContainer(arguments);
        assertThat(result.getExitCode()).as("PostgreSQL backup/restore command").isZero();
    }

    private record Database(
            JdbcTemplate sql,
            TransactionTemplate tx,
            MemoryRepository memory,
            JdbcForgetRecoveryRepository recovery) {}
}
