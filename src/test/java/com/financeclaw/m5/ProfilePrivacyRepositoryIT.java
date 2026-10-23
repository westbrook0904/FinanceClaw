package com.financeclaw.m5;

import static org.assertj.core.api.Assertions.*;

import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import com.financeclaw.infrastructure.memory.JdbcForgetRecoveryRepository;
import java.nio.file.Files;
import java.nio.file.Path;
import org.flywaydb.core.Flyway;
import org.junit.jupiter.api.AfterAll;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.params.ParameterizedTest;
import org.junit.jupiter.params.provider.ValueSource;
import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.jdbc.datasource.DriverManagerDataSource;
import org.springframework.jdbc.support.JdbcTransactionManager;
import org.springframework.transaction.support.TransactionTemplate;
import org.testcontainers.containers.PostgreSQLContainer;

/** Real recovery observations and grants, including inconsistent restored profile documents. */
class ProfilePrivacyRepositoryIT {
    static final PostgreSQLContainer<?> PG = new PostgreSQLContainer<>("postgres:16.10-alpine");
    static JdbcTemplate sql;
    static JdbcForgetRecoveryRepository recovery;
    static TransactionTemplate tx;

    @BeforeAll
    static void database() {
        PG.start();
        var source =
                new DriverManagerDataSource(PG.getJdbcUrl(), PG.getUsername(), PG.getPassword());
        Flyway.configure().dataSource(source).load().migrate();
        sql = new JdbcTemplate(source);
        recovery = new JdbcForgetRecoveryRepository(source);
        tx = new TransactionTemplate(new JdbcTransactionManager(source));
        sql.execute("CREATE ROLE financeclaw_app NOLOGIN");
        sql.execute("CREATE ROLE financeclaw_memory LOGIN PASSWORD 'profile-fixture'");
    }

    @AfterAll
    static void stop() {
        PG.stop();
    }

    @BeforeEach
    void reset() {
        sql.execute("TRUNCATE memory.owners,memory.user_profiles CASCADE");
        sql.update("INSERT INTO memory.owners(tenant,subject) VALUES('profile-test','owner')");
    }

    @Test
    void absentDefaultAndExplicitlyClearedCurrentProfileAreSafeAndSnapshotOmitsMarkdown()
            throws Exception {
        assertThat(recovery.unsafeRows()).isZero();
        assertThat(ownerSnapshot(snapshot()).path("unsafe_profiles").asLong()).isZero();
        profile(1, 3, "sensitive-profile-body");

        var snapshot = snapshot();
        assertThat(snapshot.path("schema_version").asInt()).isEqualTo(2);
        assertThat(snapshot.toString()).doesNotContain("sensitive-profile-body", "markdown");
        assertThat(ownerSnapshot(snapshot).path("unsafe_profiles").asLong()).isZero();
        assertThat(recovery.unsafeRows()).isZero();

        // Clearing a previously populated document retains its positive revision for concurrency.
        sql.update("UPDATE memory.user_profiles SET markdown=''");
        assertThat(recovery.unsafeRows()).isZero();
        assertThat(ownerSnapshot(snapshot()).path("unsafe_profiles").asLong()).isZero();
    }

    @ParameterizedTest
    @ValueSource(
            strings = {"old", "old-empty", "future", "wrong-revision", "zero-owner", "missing"})
    void repositoryAndExportedSnapshotRejectInconsistentProfiles(String corruption)
            throws Exception {
        profile(1, 2, "sensitive-profile-body");
        switch (corruption) {
            case "old" -> sql.update("UPDATE memory.owners SET epoch=2");
            case "old-empty" -> {
                sql.update("UPDATE memory.owners SET epoch=2");
                sql.update("UPDATE memory.user_profiles SET markdown=''");
            }
            case "future" -> sql.update("UPDATE memory.user_profiles SET epoch=2");
            case "wrong-revision" -> sql.update("UPDATE memory.user_profiles SET revision=3");
            case "zero-owner" -> sql.update("UPDATE memory.owners SET profile_revision=0");
            case "missing" -> sql.update("DELETE FROM memory.user_profiles");
            default -> throw new AssertionError();
        }

        assertThat(recovery.unsafeRows()).isPositive();
        var snapshot = snapshot();
        assertThat(ownerSnapshot(snapshot).path("unsafe_profiles").asLong()).isPositive();
        assertThat(snapshot.toString()).doesNotContain("sensitive-profile-body", "markdown");
    }

    @Test
    void corruptedRestoreWithOrphanProfileIsRejectedByRepositoryAndExportedSnapshot()
            throws Exception {
        tx.executeWithoutResult(
                status -> {
                    // Only this isolated test database/transaction bypasses its FK to simulate bad
                    // restore
                    // data.
                    sql.execute("SET LOCAL session_replication_role=replica");
                    sql.update(
                            "INSERT INTO memory.user_profiles(tenant,subject,epoch,revision,markdown)"
                                    + " VALUES('orphan','owner',1,1,'orphan-sensitive-profile')");
                });

        assertThat(recovery.unsafeRows()).isPositive();
        var snapshot = snapshot();
        assertThat(snapshot.path("orphan_owners").asLong()).isEqualTo(1);
        assertThat(snapshot.toString()).doesNotContain("orphan-sensitive-profile");
    }

    @ParameterizedTest
    @ValueSource(
            strings = {"src/main/resources/deployment/grants.sql", "ops/memory-worker-grants.sql"})
    void bothGrantPoliciesDenyWorkerProfileReadsAndWritesWhileAllowingMemoryJobs(String path)
            throws Exception {
        String grants = Files.readString(Path.of(path));
        // The standalone grants file also installs a pgvector index; that DDL is unrelated to
        // grants.
        grants = grants.split("CREATE INDEX IF NOT EXISTS memory_projection_cosine", 2)[0];
        grants = grants.replace("DATABASE financeclaw_java", "DATABASE " + PG.getDatabaseName());
        sql.execute(grants);
        profile(1, 1, "private-authoritative-profile");
        var worker =
                new JdbcTemplate(
                        new DriverManagerDataSource(
                                PG.getJdbcUrl(), "financeclaw_memory", "profile-fixture"));

        assertThat(worker.queryForObject("SELECT count(*) FROM memory.jobs", Long.class)).isZero();
        for (String statement :
                new String[] {
                    "SELECT markdown FROM memory.user_profiles",
                    "INSERT INTO memory.user_profiles(tenant,subject,epoch,revision,markdown)"
                            + " VALUES('profile-test','other',1,1,'automated')",
                    "UPDATE memory.user_profiles SET markdown='automated'",
                    "DELETE FROM memory.user_profiles"
                }) {
            assertThatThrownBy(() -> worker.execute(statement))
                    .hasRootCauseMessage("ERROR: permission denied for table user_profiles");
        }
        assertThat(sql.queryForObject("SELECT markdown FROM memory.user_profiles", String.class))
                .isEqualTo("private-authoritative-profile");
        if (path.startsWith("src/"))
            for (String privilege : new String[] {"SELECT", "INSERT", "UPDATE", "DELETE"})
                assertThat(
                                sql.queryForObject(
                                        "SELECT has_table_privilege('financeclaw_app','memory.user_profiles',?)",
                                        Boolean.class,
                                        privilege))
                        .isTrue();
    }

    private static void profile(long epoch, long revision, String markdown) {
        sql.update("UPDATE memory.owners SET profile_revision=?", revision);
        sql.update(
                "INSERT INTO memory.user_profiles(tenant,subject,epoch,revision,markdown)"
                        + " VALUES('profile-test','owner',?,?,?)",
                epoch,
                revision,
                markdown);
    }

    private static JsonNode ownerSnapshot(JsonNode snapshot) {
        assertThat(snapshot.path("owners").size()).isEqualTo(1);
        return snapshot.path("owners").get(0);
    }

    private static JsonNode snapshot() throws Exception {
        var result =
                PG.execInContainer(
                        "psql",
                        "-X",
                        "-qAt",
                        "-v",
                        "ON_ERROR_STOP=1",
                        "-U",
                        PG.getUsername(),
                        "-d",
                        PG.getDatabaseName(),
                        "-c",
                        Files.readString(Path.of("ops/restore-privacy-snapshot.sql")));
        assertThat(result.getExitCode()).as("read-only privacy snapshot export").isZero();
        return new ObjectMapper().readTree(result.getStdout());
    }
}
