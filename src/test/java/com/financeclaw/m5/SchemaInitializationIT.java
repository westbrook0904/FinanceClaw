package com.financeclaw.m5;

import static org.assertj.core.api.Assertions.*;

import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import org.flywaydb.core.Flyway;
import org.junit.jupiter.api.AfterAll;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.Test;
import org.springframework.dao.DataIntegrityViolationException;
import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.jdbc.datasource.DriverManagerDataSource;
import org.testcontainers.containers.PostgreSQLContainer;

/** 新库契约：从空库建立当前模型，并验证当前 Java 基线之后的增量迁移。 */
class SchemaInitializationIT {
    private static final PostgreSQLContainer<?> PG =
            new PostgreSQLContainer<>("postgres:16.10-alpine");
    private static Flyway initialization;
    private static JdbcTemplate sql;
    private static Map<String, Long> initialRows;

    @BeforeAll
    static void initializeEmptyDatabase() {
        PG.start();
        var source =
                new DriverManagerDataSource(PG.getJdbcUrl(), PG.getUsername(), PG.getPassword());
        sql = new JdbcTemplate(source);
        // 增量迁移必须给已存在的应用角色授予新表权限，不能依赖旧 GRANT ON ALL TABLES。
        sql.execute("CREATE ROLE financeclaw_app");
        initialization = Flyway.configure().dataSource(source).load();
        assertThat(initialization.migrate().migrationsExecuted).isEqualTo(7);
        initialRows = new LinkedHashMap<>();
        for (String table : tables()) {
            initialRows.put(table, sql.queryForObject("SELECT count(*) FROM " + table, Long.class));
        }
    }

    @AfterAll
    static void stop() {
        PG.stop();
    }

    @Test
    void createsCurrentTablesThroughVersionedMigrations() {
        assertThat(tables())
                .containsExactly(
                        "agent_runtime.states",
                        "agent_runtime.work_contexts",
                        "business.admission",
                        "business.admission_changes",
                        "business.artifacts",
                        "business.attempts",
                        "business.cancel_requests",
                        "business.capability_steps",
                        "business.child_invocations",
                        "business.commands",
                        "business.conversations",
                        "business.events",
                        "business.grant_changes",
                        "business.grants",
                        "business.interactions",
                        "business.journal",
                        "business.reports",
                        "business.turn_policies",
                        "business.turns",
                        "channel.bindings",
                        "channel.inbox",
                        "channel.link_codes",
                        "channel.notices",
                        "control_plane.agent_draft_revisions",
                        "control_plane.agents",
                        "control_plane.deployment_changes",
                        "control_plane.deployments",
                        "control_plane.release_manifests",
                        "memory.audit",
                        "memory.embedding_profiles",
                        "memory.facts",
                        "memory.forget_applied",
                        "memory.forget_requests",
                        "memory.jobs",
                        "memory.operations",
                        "memory.owners",
                        "memory.privacy_ledger_binding",
                        "memory.projections",
                        "memory.sources",
                        "memory.turn_snapshots",
                        "memory.user_profiles");
        assertThat(initialization.info().applied()).hasSize(7);
        assertThat(initialization.info().current().getVersion().toString()).isEqualTo("7");
        assertThat(initialization.validateWithResult().validationSuccessful).isTrue();
        assertThat(initialization.migrate().migrationsExecuted).isZero();
    }

    @Test
    void startsWithoutUserDataAndLeavesTheIndependentLedgerOutsideTheBusinessDatabase() {
        assertThat(initialRows).hasSize(41);
        initialRows.forEach(
                (table, count) ->
                        assertThat(count)
                                .as(table)
                                .isEqualTo(
                                        table.equals("memory.privacy_ledger_binding") ? 1L : 0L));
        assertThat(
                        sql.queryForObject(
                                "SELECT to_regnamespace('privacy_ledger') IS NULL", Boolean.class))
                .isTrue();
        assertThat(sql.queryForMap("SELECT singleton,active FROM memory.privacy_ledger_binding"))
                .containsEntry("singleton", true)
                .containsEntry("active", false);
    }

    @Test
    void initialSchemaIncludesBothResumeForeignKeysDespiteTheInteractionCycle() {
        sql.update(
                "INSERT INTO business.conversations(id,tenant,subject) VALUES('conversation','tenant','owner')");
        sql.update(
                "INSERT INTO business.turns(id,conversation_id,tenant,subject,idempotency_key,request_hash,message,release_hash,status) VALUES('turn','conversation','tenant','owner','key','hash','message','release','running')");
        sql.update(
                "INSERT INTO business.commands(id,turn_id,status) VALUES('command','turn','running')");
        sql.update(
                "INSERT INTO business.attempts(id,command_id,fence) VALUES('attempt','command',1)");
        assertThatThrownBy(
                        () ->
                                sql.update(
                                        "UPDATE business.commands SET resume_id='missing' WHERE id='command'"))
                .isInstanceOf(DataIntegrityViolationException.class);
        assertThatThrownBy(
                        () ->
                                sql.update(
                                        "UPDATE business.attempts SET resume_id='missing' WHERE id='attempt'"))
                .isInstanceOf(DataIntegrityViolationException.class);
        sql.update(
                "INSERT INTO business.interactions(id,turn_id,command_id,attempt_id,state_version,grant_revision,release_hash,kind,call_id,tool,action_hash,schema_json,question) VALUES('interaction','turn','command','attempt',1,1,'release','input','call','tool','action','{}','question')");
        sql.update("UPDATE business.commands SET resume_id='interaction' WHERE id='command'");
        sql.update("UPDATE business.attempts SET resume_id='interaction' WHERE id='attempt'");
        assertThat(
                        sql.queryForObject(
                                "SELECT resume_id FROM business.commands WHERE id='command'",
                                String.class))
                .isEqualTo("interaction");
        assertThat(
                        sql.queryForObject(
                                "SELECT resume_id FROM business.attempts WHERE id='attempt'",
                                String.class))
                .isEqualTo("interaction");
    }

    @Test
    void childInvocationStoresOnlyStableBindingAndBoundedCounters() {
        assertThat(
                        sql.queryForList(
                                "SELECT column_name FROM information_schema.columns WHERE"
                                        + " table_schema='business' AND table_name='child_invocations'"
                                        + " ORDER BY ordinal_position",
                                String.class))
                .containsExactly(
                        "root_turn_id",
                        "call_id",
                        "target",
                        "input_hash",
                        "scopes",
                        "status",
                        "model_calls",
                        "tool_calls");
        sql.update(
                "INSERT INTO business.conversations(id,tenant,subject)"
                        + " VALUES('child-conversation','tenant','owner')");
        sql.update(
                "INSERT INTO business.turns(id,conversation_id,tenant,subject,idempotency_key,request_hash,message,release_hash,status)"
                        + " VALUES('child-turn','child-conversation','tenant','owner','key','hash','message','release','running')");
        sql.update(
                "INSERT INTO business.child_invocations(root_turn_id,call_id,input_hash)"
                        + " VALUES('child-turn','call',?)",
                "a".repeat(64));
        assertThat(
                        sql.queryForMap(
                                "SELECT target,scopes::text,status,model_calls,tool_calls FROM"
                                        + " business.child_invocations WHERE root_turn_id='child-turn'"))
                .containsEntry("target", "market_research")
                .containsEntry("scopes", "[\"market:read\"]")
                .containsEntry("status", "pending")
                .containsEntry("model_calls", 0)
                .containsEntry("tool_calls", 0);
        for (String assignment :
                List.of(
                        "target='other_agent'",
                        "scopes='[\"report:write\"]'::jsonb",
                        "scopes='[\"market:read\",\"report:write\"]'::jsonb",
                        "status='queued'",
                        "model_calls=-1",
                        "model_calls=5",
                        "tool_calls=-1",
                        "tool_calls=11",
                        "input_hash='not-a-sha256'",
                        "call_id=' '")) {
            assertThatThrownBy(
                            () ->
                                    sql.update(
                                            "UPDATE business.child_invocations SET "
                                                    + assignment
                                                    + " WHERE root_turn_id='child-turn'"))
                    .as(assignment)
                    .isInstanceOf(DataIntegrityViolationException.class);
        }
        assertThatThrownBy(
                        () ->
                                sql.update(
                                        "INSERT INTO business.child_invocations(root_turn_id,call_id,input_hash)"
                                                + " VALUES('child-turn','call',?)",
                                        "a".repeat(64)))
                .isInstanceOf(DataIntegrityViolationException.class);
        sql.update("DELETE FROM business.turns WHERE id='child-turn'");
        assertThat(
                        sql.queryForObject(
                                "SELECT count(*) FROM business.child_invocations WHERE"
                                        + " root_turn_id='child-turn'",
                                Long.class))
                .isZero();
    }

    @Test
    void existingApplicationRoleReceivesNewChildTablePrivileges() {
        for (String privilege : List.of("SELECT", "INSERT", "UPDATE", "DELETE")) {
            assertThat(
                            sql.queryForObject(
                                    "SELECT has_table_privilege('financeclaw_app',"
                                            + " 'business.child_invocations',?)",
                                    Boolean.class,
                                    privilege))
                    .as(privilege)
                    .isTrue();
        }
        assertThat(
                        sql.queryForObject(
                                "SELECT has_schema_privilege('financeclaw_app','business','CREATE')",
                                Boolean.class))
                .isFalse();
    }

    private static List<String> tables() {
        return sql.queryForList(
                "SELECT schemaname||'.'||tablename FROM pg_tables WHERE schemaname IN ('business','agent_runtime','memory','channel','control_plane') ORDER BY schemaname,tablename",
                String.class);
    }
}
