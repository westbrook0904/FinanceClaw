package com.financeclaw.m4;

import static org.assertj.core.api.Assertions.*;
import static org.mockito.Mockito.*;

import com.financeclaw.app.memory.*;
import com.financeclaw.app.memory.port.*;
import com.financeclaw.app.task.TaskEventHub;
import com.financeclaw.domain.ExecutionScope;
import com.financeclaw.domain.Task.*;
import com.financeclaw.domain.memory.MemoryPolicy;
import com.financeclaw.infrastructure.memory.*;
import java.nio.file.*;
import java.util.List;
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

/** 实际文件、PostgreSQL 事务与用户锁共同验证直接编辑、快照和遗忘边界。 */
class ServerProfileDirectoryIT {
    private static final PostgreSQLContainer<?> PG =
            new PostgreSQLContainer<>("postgres:16.10-alpine");
    private static final Owner ALICE = new Owner("tenant", "alice");
    private static final Owner BOB = new Owner("tenant", "bob");
    private static JdbcTemplate sql;
    private static TransactionTemplate tx;
    private static MemoryRepository db;
    @TempDir Path root;
    private DirectoryProfileDocuments documents;
    private UserProfileService profiles;
    private MemoryService memory;

    @BeforeAll
    static void database() throws Exception {
        PG.start();
        var source =
                new DriverManagerDataSource(PG.getJdbcUrl(), PG.getUsername(), PG.getPassword());
        Flyway.configure().dataSource(source).load().migrate();
        sql = new JdbcTemplate(source);
        tx = new TransactionTemplate(new JdbcTransactionManager(source));
        var configuration = new Configuration();
        configuration.setMapUnderscoreToCamelCase(true);
        configuration.setCacheEnabled(false);
        configuration.setLocalCacheScope(LocalCacheScope.STATEMENT);
        configuration.addMapper(MemoryMapper.class);
        var factory = new SqlSessionFactoryBean();
        factory.setDataSource(source);
        factory.setConfiguration(configuration);
        db =
                new MyBatisMemoryRepository(
                        new SqlSessionTemplate(factory.getObject()).getMapper(MemoryMapper.class));
    }

    @AfterAll
    static void stop() {
        PG.stop();
    }

    @BeforeEach
    void reset() {
        sql.execute("TRUNCATE business.conversations CASCADE");
        sql.execute("TRUNCATE memory.owners,memory.jobs,memory.operations,memory.audit CASCADE");
        documents = new DirectoryProfileDocuments(root);
        var settings = new MemoryPolicy(false, false, "disabled", true);
        profiles = new UserProfileService(db, tx, settings, documents);
        memory = new MemoryService(db, tx, settings, forgetting(documents), profiles);
    }

    @Test
    void directAndAtomicEditsOnlyAffectNewTurnsAndStableFilesDoNotWriteVersions() throws Exception {
        profiles.read(ALICE);
        Files.writeString(path(ALICE, 1), "旧画像：中文回答");
        var old = admit("old", ALICE);
        Path replacement = path(ALICE, 1).resolveSibling("replacement.md");
        Files.writeString(replacement, "新画像：详细说明");
        Files.move(
                replacement,
                path(ALICE, 1),
                StandardCopyOption.ATOMIC_MOVE,
                StandardCopyOption.REPLACE_EXISTING);
        assertThat(memory.context(old)).contains("旧画像").doesNotContain("新画像");
        var current = admit("current", ALICE);
        assertThat(memory.context(current)).contains("新画像").doesNotContain("旧画像");
        admit("unchanged", ALICE);
        assertThat(profiles.read(ALICE).revision()).isEqualTo(2);
        assertThat(
                        sql.queryForObject(
                                "SELECT count(*) FROM memory.audit WHERE action='profile_file_update'",
                                Integer.class))
                .isEqualTo(2);
        Files.delete(path(ALICE, 1));
        // 文件读取异常或删除不会改变既有 Turn 的已冻结正文。
        assertThat(memory.context(current)).contains("新画像");
    }

    @Test
    void forgettingErasesOnlyTheOldOwnerEpochAndCannotReimportRestoredOldFiles() throws Exception {
        profiles.read(ALICE);
        profiles.read(BOB);
        Files.writeString(path(ALICE, 1), "alice-private");
        Files.writeString(path(BOB, 1), "bob-private");
        var old = admit("old", ALICE);
        assertThat(memory.forget(ALICE, 1, "forget-files")).isEqualTo(2);
        assertThat(path(ALICE, 1)).doesNotExist();
        assertThat(path(BOB, 1)).hasContent("bob-private");
        assertThatThrownBy(() -> memory.context(old)).hasMessage("memory_privacy_changed");
        // 模拟单独恢复了旧文件备份；新 epoch 永远不会采样旧路径。
        Files.createDirectories(path(ALICE, 1).getParent());
        Files.writeString(path(ALICE, 1), "restored-forgotten-profile");
        var current = admit("new", ALICE);
        assertThat(memory.context(current)).isEmpty();
        Files.writeString(path(ALICE, 2), "new-epoch-input");
        assertThat(memory.forget(ALICE, 1, "forget-files")).isEqualTo(2);
        assertThat(path(ALICE, 1)).doesNotExist();
        assertThat(path(ALICE, 2)).hasContent("new-epoch-input");
    }

    @Test
    void fileEraseFailureLeavesSqlEpochAndProfileUnchanged() throws Exception {
        profiles.read(ALICE);
        Files.writeString(path(ALICE, 1), "retained-until-forget-succeeds");
        profiles.read(ALICE);
        var failed = mock(ProfileDocuments.class);
        doThrow(new Problem(503, "profile_file_unavailable")).when(failed).eraseThrough(ALICE, 1);
        assertThatThrownBy(() -> forgetting(failed).forget(ALICE, 1, "failed-forget"))
                .hasMessage("profile_file_unavailable");
        var privacy = tx.execute(status -> db.lock(ALICE));
        assertThat(privacy.epoch()).isEqualTo(1);
        assertThat(privacy.purgePending()).isFalse();
        assertThat(db.userProfile(ALICE).markdown()).isEqualTo("retained-until-forget-succeeds");
        assertThat(db.operation(ALICE, "failed-forget")).isNull();
    }

    @Test
    void concurrentAdmissionsDoNotUpgradeSharedLocksOrDuplicateTheProfileRevision()
            throws Exception {
        profiles.read(ALICE);
        Files.writeString(path(ALICE, 1), "same-server-profile");
        try (var executor = Executors.newFixedThreadPool(2)) {
            var first = executor.submit(() -> admit("first", ALICE));
            var second = executor.submit(() -> admit("second", ALICE));
            assertThat(memory.context(first.get(10, TimeUnit.SECONDS)))
                    .contains("same-server-profile");
            assertThat(memory.context(second.get(10, TimeUnit.SECONDS)))
                    .contains("same-server-profile");
        }
        assertThat(profiles.read(ALICE).revision()).isEqualTo(1);
        assertThat(sql.queryForObject("SELECT count(*) FROM memory.turn_snapshots", Integer.class))
                .isEqualTo(2);
    }

    private MemoryForgetService forgetting(ProfileDocuments files) {
        return new MemoryForgetService(
                db,
                tx,
                new JdbcForgetRecoveryRepository(sql.getDataSource()),
                mock(ForgetLedger.class),
                new TaskEventHub(),
                files);
    }

    private Path path(Owner owner, long epoch) {
        return root.resolve(ExecutionScope.hash(List.of(owner.tenant(), owner.subject())))
                .resolve("epoch-" + epoch)
                .resolve("profile.md");
    }

    private Turn admit(String id, Owner owner) {
        String conversation = "conversation-" + id;
        sql.update(
                "INSERT INTO business.conversations(id,tenant,subject) VALUES(?,?,?)",
                conversation,
                owner.tenant(),
                owner.subject());
        sql.update(
                "INSERT INTO business.turns(id,conversation_id,tenant,subject,idempotency_key,request_hash,message,release_hash,status) VALUES(?,?,?,?,?,'hash','request','release','running')",
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
        tx.executeWithoutResult(status -> memory.admit(turn));
        return turn;
    }
}
