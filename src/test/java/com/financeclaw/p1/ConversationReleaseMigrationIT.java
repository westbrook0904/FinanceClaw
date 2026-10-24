package com.financeclaw.p1;

import static org.assertj.core.api.Assertions.*;

import java.util.Map;
import org.flywaydb.core.Flyway;
import org.junit.jupiter.api.AfterAll;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.Test;
import org.springframework.dao.DataIntegrityViolationException;
import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.jdbc.datasource.DriverManagerDataSource;
import org.testcontainers.containers.PostgreSQLContainer;

/** Upgrade a real V1 database without inventing manifests for historical release hashes. */
class ConversationReleaseMigrationIT {
    private static final PostgreSQLContainer<?> PG =
            new PostgreSQLContainer<>("postgres:16.10-alpine");

    @BeforeAll
    static void startDatabase() {
        PG.start();
    }

    @AfterAll
    static void stopDatabase() {
        PG.stop();
    }

    @Test
    void existingConversationsKeepTheirLatestAdmittedReleaseAndEmptyOnesBindOnlyOnce() {
        var source =
                new DriverManagerDataSource(PG.getJdbcUrl(), PG.getUsername(), PG.getPassword());
        var sql = new JdbcTemplate(source);
        assertThat(
                        Flyway.configure()
                                .dataSource(source)
                                .target("1")
                                .load()
                                .migrate()
                                .migrationsExecuted)
                .isOne();
        sql.update(
                """
                INSERT INTO business.conversations(id,tenant,subject) VALUES
                  ('history','tenant','owner'),
                  ('same-time','tenant','owner'),
                  ('empty','tenant','owner')
                """);
        sql.update(
                """
                INSERT INTO business.turns(
                  id,conversation_id,tenant,subject,idempotency_key,request_hash,message,
                  release_hash,status,created_at) VALUES
                  ('older','history','tenant','owner','older','h1','first',
                   'older-release','completed','2026-01-01T00:00:00Z'),
                  ('latest','history','tenant','owner','latest','h2','last',
                   'missing-latest-release','failed','2026-01-02T00:00:00Z'),
                  ('turn-a','same-time','tenant','owner','a','h3','a',
                   'tie-earlier-release','completed','2026-01-03T00:00:00Z'),
                  ('turn-z','same-time','tenant','owner','z','h4','z',
                   'tie-latest-release','waiting','2026-01-03T00:00:00Z')
                """);
        var originalTurns =
                sql.queryForList(
                        "SELECT id,conversation_id,release_hash,status FROM business.turns ORDER BY id");

        var migrations = Flyway.configure().dataSource(source).load();
        assertThat(migrations.migrate().migrationsExecuted).isEqualTo(8);
        assertThat(migrations.info().current().getVersion().toString()).isEqualTo("9");
        assertThat(
                        sql.queryForMap(
                                "SELECT id,release_hash FROM business.conversations WHERE id='history'"))
                .isEqualTo(Map.of("id", "history", "release_hash", "missing-latest-release"));
        assertThat(binding(sql, "same-time")).isEqualTo("tie-latest-release");
        assertThat(binding(sql, "empty")).isNull();
        assertThat(sql.queryForList("SELECT DISTINCT agent_id FROM business.turns", String.class))
                .containsExactly("finance_agent");
        assertThat(
                        sql.queryForList(
                                "SELECT DISTINCT agent_id FROM business.conversations",
                                String.class))
                .containsExactly("finance_agent");
        assertThat(
                        sql.queryForList(
                                "SELECT id,conversation_id,release_hash,status FROM business.turns ORDER BY id"))
                .isEqualTo(originalTurns);

        // A historical hash remains valid binding evidence even when its manifest is unavailable.
        assertThat(
                        sql.update(
                                "UPDATE business.conversations SET release_hash=? WHERE id='history'",
                                "missing-latest-release"))
                .isOne();
        assertThatThrownBy(
                        () ->
                                sql.update(
                                        "UPDATE business.conversations SET release_hash='new-release' WHERE id='history'"))
                .isInstanceOf(DataIntegrityViolationException.class);
        assertThatThrownBy(
                        () ->
                                sql.update(
                                        "UPDATE business.conversations SET release_hash=NULL WHERE id='history'"))
                .isInstanceOf(DataIntegrityViolationException.class);
        assertThat(binding(sql, "history")).isEqualTo("missing-latest-release");

        assertThat(
                        sql.update(
                                "UPDATE business.conversations SET release_hash='first-use-release' WHERE id='empty'"))
                .isOne();
        assertThatThrownBy(
                        () ->
                                sql.update(
                                        "UPDATE business.conversations SET release_hash='later-release' WHERE id='empty'"))
                .isInstanceOf(DataIntegrityViolationException.class);
        assertThat(binding(sql, "empty")).isEqualTo("first-use-release");
        assertThat(migrations.migrate().migrationsExecuted).isZero();
        assertThat(binding(sql, "history")).isEqualTo("missing-latest-release");
        assertThat(binding(sql, "empty")).isEqualTo("first-use-release");
    }

    private static String binding(JdbcTemplate sql, String conversation) {
        return sql.queryForObject(
                "SELECT release_hash FROM business.conversations WHERE id=?",
                String.class,
                conversation);
    }
}
