package com.financeclaw.p4;

import static org.assertj.core.api.Assertions.*;

import java.util.List;
import org.flywaydb.core.Flyway;
import org.junit.jupiter.api.Test;
import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.jdbc.datasource.DriverManagerDataSource;
import org.testcontainers.containers.PostgreSQLContainer;

class NonRagMigrationIT {
    @Test
    void advancesDeliveredVersionTenWithoutChangingExistingChecksumsOrCommandIdentity() {
        try (var pg = new PostgreSQLContainer<>("postgres:16.10-alpine")) {
            pg.start();
            var source =
                    new DriverManagerDataSource(
                            pg.getJdbcUrl(), pg.getUsername(), pg.getPassword());
            var sql = new JdbcTemplate(source);
            sql.execute("CREATE ROLE financeclaw_app");
            Flyway.configure().dataSource(source).target("10").load().migrate();
            var before =
                    sql.queryForList(
                            "SELECT version,checksum FROM flyway_schema_history WHERE success ORDER BY installed_rank");
            sql.update(
                    "INSERT INTO business.conversations(id,tenant,subject,release_hash) VALUES('old-conversation','old-tenant','old-subject','old-release')");
            sql.update(
                    "INSERT INTO business.turns(id,conversation_id,tenant,subject,idempotency_key,request_hash,message,release_hash,status) VALUES('old-turn','old-conversation','old-tenant','old-subject','old-key','old-hash','keep-input','old-release','running')");
            sql.update(
                    "INSERT INTO business.commands(id,turn_id,status,attempt_id,fence,worker_id,lease_until) VALUES('old-command','old-turn','running','old-attempt',7,'old-worker',clock_timestamp()+interval '1 hour')");
            sql.update(
                    "INSERT INTO business.attempts(id,command_id,fence,outcome,result) VALUES('old-attempt','old-command',7,'completed','keep-receipt')");
            var command = sql.queryForMap("SELECT * FROM business.commands WHERE id='old-command'");
            var attempt = sql.queryForMap("SELECT * FROM business.attempts WHERE id='old-attempt'");
            var current = Flyway.configure().dataSource(source).load();
            assertThat(current.migrate().migrationsExecuted).isEqualTo(5);
            assertThat(current.info().current().getVersion().toString()).isEqualTo("15");
            assertThat(current.validateWithResult().validationSuccessful).isTrue();
            assertThat(
                            sql.queryForList(
                                    "SELECT version,checksum FROM flyway_schema_history WHERE success AND version::int<=10 ORDER BY installed_rank"))
                    .isEqualTo(before);
            assertThat(sql.queryForMap("SELECT * FROM business.commands WHERE id='old-command'"))
                    .isEqualTo(command);
            assertThat(sql.queryForMap("SELECT * FROM business.attempts WHERE id='old-attempt'"))
                    .isEqualTo(attempt);
            assertThat(
                            sql.queryForObject(
                                    "SELECT message FROM business.turns WHERE id='old-turn'",
                                    String.class))
                    .isEqualTo("keep-input");
            for (String table :
                    List.of(
                            "control_plane.resource_grants",
                            "control_plane.resource_grant_audit",
                            "control_plane.trigger_rules",
                            "control_plane.trigger_rule_revisions",
                            "control_plane.trigger_firings",
                            "business.dispatch_policies",
                            "business.dispatch_metadata",
                            "business.dispatch_tenant_limits",
                            "business.dispatch_agent_limits",
                            "business.runtime_workers",
                            "business.runtime_artifacts")) {
                assertThat(sql.queryForObject("SELECT count(*) FROM " + table, Integer.class))
                        .as(table)
                        .isZero();
                assertThat(
                                sql.queryForObject(
                                        "SELECT has_table_privilege('financeclaw_app',?,'SELECT')",
                                        Boolean.class,
                                        table))
                        .as(table)
                        .isTrue();
            }
            assertThat(current.migrate().migrationsExecuted).isZero();
        }
    }
}
