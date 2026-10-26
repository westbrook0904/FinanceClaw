package com.financeclaw.p3;

import static org.assertj.core.api.Assertions.*;

import org.flywaydb.core.Flyway;
import org.junit.jupiter.api.Test;
import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.jdbc.datasource.DriverManagerDataSource;
import org.testcontainers.containers.PostgreSQLContainer;

/** V10 必须能升级当前已发布的 V9，不通过重写 V1 或清空通知来获得新索引。 */
class OutboxIndexMigrationIT {
    @Test
    void upgradesVersionNineWithoutChangingAppliedChecksumsOrExistingNotices() {
        try (var postgres = new PostgreSQLContainer<>("postgres:16.10-alpine")) {
            postgres.start();
            var source =
                    new DriverManagerDataSource(
                            postgres.getJdbcUrl(), postgres.getUsername(), postgres.getPassword());
            var sql = new JdbcTemplate(source);
            var previous = Flyway.configure().dataSource(source).target("9").load();
            assertThat(previous.migrate().migrationsExecuted).isEqualTo(9);
            var checksums =
                    sql.queryForList(
                            "SELECT version,checksum FROM flyway_schema_history WHERE success ORDER BY installed_rank");
            assertThat(
                            sql.queryForObject(
                                    "SELECT to_regclass('channel.channel_pending_notice_binding') IS NULL",
                                    Boolean.class))
                    .isTrue();
            sql.update(
                    "INSERT INTO business.conversations(id,tenant,subject) VALUES('upgrade-conversation','tenant','owner')");
            sql.update(
                    "INSERT INTO business.turns(id,conversation_id,tenant,subject,idempotency_key,request_hash,message,release_hash,status)"
                            + " VALUES('upgrade-turn','upgrade-conversation','tenant','owner','key','hash','message','release','completed')");
            sql.update(
                    "INSERT INTO channel.bindings(id,tenant_key,open_id,chat_id,tenant,subject,conversation_id,active_turn)"
                            + " VALUES('upgrade-binding','channel','open','chat','tenant','owner','upgrade-conversation','upgrade-turn')");
            long revision = 1;
            for (String status :
                    java.util.List.of(
                            "pending",
                            "sending",
                            "unknown",
                            "submitted_unverified",
                            "suppressed")) {
                sql.update(
                        "INSERT INTO channel.notices(id,turn_id,binding_id,binding_revision,revision,payload,status,reason)"
                                + " VALUES(?,'upgrade-turn','upgrade-binding',3,?,?,?,'preserve-reason')",
                        "upgrade-" + status,
                        revision++,
                        "preserve-" + status,
                        status);
            }
            var notices = sql.queryForList("SELECT * FROM channel.notices ORDER BY id");

            var current = Flyway.configure().dataSource(source).target("10").load();
            assertThat(current.migrate().migrationsExecuted).isOne();
            assertThat(current.info().current().getVersion().toString()).isEqualTo("10");
            assertThat(
                            sql.queryForList(
                                    "SELECT version,checksum FROM flyway_schema_history WHERE success AND version <> '10' ORDER BY installed_rank"))
                    .isEqualTo(checksums);
            assertThat(current.validateWithResult().validationSuccessful).isTrue();
            assertThat(
                            sql.queryForObject(
                                    "SELECT pg_get_indexdef('channel.channel_pending_notice_binding'::regclass)",
                                    String.class))
                    .contains(
                            "ON channel.notices USING btree (binding_id, created_at)",
                            "WHERE (status = 'pending'::text)");
            assertThat(sql.queryForList("SELECT * FROM channel.notices ORDER BY id"))
                    .isEqualTo(notices);
            assertThat(current.migrate().migrationsExecuted).isZero();
        }
    }
}
