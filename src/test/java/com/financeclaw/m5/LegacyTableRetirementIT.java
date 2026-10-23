package com.financeclaw.m5;

import static org.assertj.core.api.Assertions.*;

import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import org.flywaydb.core.Flyway;
import org.flywaydb.core.api.FlywayException;
import org.junit.jupiter.api.AfterAll;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.Test;
import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.jdbc.datasource.DriverManagerDataSource;
import org.testcontainers.containers.PostgreSQLContainer;

/** Exercises a populated V9 upgrade, including an unexpected dependency and migration rollback. */
class LegacyTableRetirementIT {
  private static final PostgreSQLContainer<?> PG =
      new PostgreSQLContainer<>("postgres:16.10-alpine");
  private static DriverManagerDataSource source;
  private static JdbcTemplate sql;
  private static final List<String> RETIRED =
      List.of(
          "business.projections",
          "business.outbox",
          "business.skill_refs",
          "agent_runtime.tool_effects");

  @BeforeAll
  static void prepare() {
    PG.start();
    source = new DriverManagerDataSource(PG.getJdbcUrl(), PG.getUsername(), PG.getPassword());
    Flyway.configure().dataSource(source).target("9").load().migrate();
    sql = new JdbcTemplate(source);
    sql.update(
        "INSERT INTO business.conversations(id,tenant,subject) VALUES('conversation','tenant','owner')");
    sql.update(
        "INSERT INTO business.turns(id,conversation_id,tenant,subject,idempotency_key,request_hash,message,release_hash,status) VALUES('turn','conversation','tenant','owner','key','request','private message','release','completed')");
    sql.update("INSERT INTO business.commands(id,turn_id,status) VALUES('command','turn','done')");
    sql.update(
        "INSERT INTO business.attempts(id,command_id,fence,state_version,outcome,result) VALUES('attempt','command',1,1,'completed','answer')");
    sql.update(
        "INSERT INTO business.journal(conversation_id,turn_id,role,content) VALUES('conversation','turn','assistant','answer')");
    sql.update("INSERT INTO business.events(turn_id,revision,status) VALUES('turn',1,'completed')");
    sql.update(
        "INSERT INTO business.grants(conversation_id,scopes) VALUES('conversation','[\"artifact:read\"]')");
    sql.update(
        "INSERT INTO business.turn_policies(turn_id,grant_revision,scopes) VALUES('turn',1,'[]')");
    sql.update(
        "INSERT INTO agent_runtime.states(namespace,user_id,session_id,state_key,version,payload) VALUES('namespace','owner','session','agent_state',1,'{\"saved\":true}')");
    sql.update(
        "INSERT INTO memory.owners(tenant,subject,profile_revision) VALUES('tenant','owner',1)");
    sql.update(
        "INSERT INTO memory.user_profiles(tenant,subject,epoch,revision,markdown) VALUES('tenant','owner',1,1,'# Human profile')");
    sql.update(
        "INSERT INTO memory.sources(id,tenant,subject,epoch,content,status,profile_revision) VALUES('turn','tenant','owner',1,'evidence','admitted',1)");
    sql.update("INSERT INTO business.outbox(turn_id,revision,status) VALUES('turn',1,'completed')");
    sql.update(
        "INSERT INTO business.projections(turn_id,revision,status) VALUES('turn',1,'completed')");
    sql.update(
        "INSERT INTO business.skill_refs(turn_id,package_hash,version) VALUES('turn','legacy-package','v1')");
    sql.update(
        "INSERT INTO agent_runtime.tool_effects(scope_key,operation_id,request_hash,status) VALUES('legacy-scope','operation','hash','pending')");
  }

  @AfterAll
  static void stop() {
    PG.stop();
  }

  @Test
  void upgradeRetiresOnlyFourTablesAndUnexpectedDependenciesRollBackWithoutCascade() {
    var original = snapshot();
    assertThat(original).hasSize(39);
    sql.execute(
        "CREATE VIEW public.legacy_skill_consumer AS SELECT turn_id FROM business.skill_refs");
    assertThatThrownBy(() -> Flyway.configure().dataSource(source).target("10").load().migrate())
        .isInstanceOf(FlywayException.class);
    assertThat(snapshot()).isEqualTo(original);
    for (String table : RETIRED) assertThat(exists(table)).isTrue();
    sql.execute("DROP VIEW public.legacy_skill_consumer");

    var migration = Flyway.configure().dataSource(source).target("10").load();
    assertThat(migration.migrate().migrationsExecuted).isEqualTo(1);
    var remaining = new LinkedHashMap<>(original);
    RETIRED.forEach(remaining::remove);
    assertThat(snapshot()).isEqualTo(remaining).hasSize(35);
    for (String table : RETIRED) assertThat(exists(table)).isFalse();
    assertThat(exists("business.outbox_pending")).isFalse();
    assertThat(exists("business.outbox_id_seq")).isFalse();
    assertThat(migration.validateWithResult().validationSuccessful).isTrue();
    assertThat(migration.migrate().migrationsExecuted).isZero();
  }

  private static boolean exists(String relation) {
    return Boolean.TRUE.equals(
        sql.queryForObject("SELECT to_regclass(?) IS NOT NULL", Boolean.class, relation));
  }

  private static Map<String, String> snapshot() {
    var rows = new LinkedHashMap<String, String>();
    for (String table :
        sql.queryForList(
            "SELECT schemaname||'.'||tablename FROM pg_tables WHERE schemaname IN ('business','agent_runtime','memory','channel') ORDER BY schemaname,tablename",
            String.class)) {
      // Names come only from the fresh schema's catalog, never from request input.
      rows.put(
          table,
          sql.queryForObject(
              "SELECT COALESCE(jsonb_agg(to_jsonb(t) ORDER BY to_jsonb(t)::text),'[]'::jsonb)::text FROM "
                  + table
                  + " t",
              String.class));
    }
    return rows;
  }
}
