package com.financeclaw.m2;

import static org.assertj.core.api.Assertions.*;
import static org.mockito.ArgumentMatchers.*;
import static org.mockito.Mockito.*;

import com.financeclaw.app.execution.ExecutionLeaseService;
import com.financeclaw.app.execution.ExecutionPolicyService;
import com.financeclaw.app.report.ReportService;
import com.financeclaw.domain.Governance.Blocked;
import com.financeclaw.domain.Task.Claim;
import com.financeclaw.domain.Task.Command;
import com.financeclaw.domain.Task.Turn;
import com.financeclaw.infrastructure.governance.GovernanceMapper;
import com.financeclaw.infrastructure.governance.MyBatisReportGateway;
import java.util.List;
import java.util.Map;
import java.util.function.Supplier;
import org.apache.ibatis.session.Configuration;
import org.apache.ibatis.session.LocalCacheScope;
import org.flywaydb.core.Flyway;
import org.junit.jupiter.api.AfterAll;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;
import org.mybatis.spring.SqlSessionFactoryBean;
import org.mybatis.spring.SqlSessionTemplate;
import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.jdbc.datasource.DriverManagerDataSource;
import org.springframework.jdbc.support.JdbcTransactionManager;
import org.springframework.transaction.support.TransactionTemplate;
import org.testcontainers.containers.PostgreSQLContainer;

/** Upgrades actual V10 report/receipt combinations; no invented historical IDs or receipts. */
class ReportPublicationMigrationIT {
  private static final PostgreSQLContainer<?> PG =
      new PostgreSQLContainer<>("postgres:16.10-alpine");
  private DriverManagerDataSource source;
  private JdbcTemplate sql;

  @BeforeAll
  static void start() {
    PG.start();
  }

  @AfterAll
  static void stop() {
    PG.stop();
  }

  @BeforeEach
  void legacyDatabase() {
    source = new DriverManagerDataSource(PG.getJdbcUrl(), PG.getUsername(), PG.getPassword());
    var legacy = Flyway.configure().dataSource(source).cleanDisabled(false).target("10").load();
    sql = new JdbcTemplate(source);
    // Flyway manages its history in public; the migrations create additional schemas themselves.
    // This container belongs only to this class, so remove every application schema between cases.
    legacy.clean();
    for (String schema : List.of("memory", "channel", "business", "agent_runtime"))
      sql.execute("DROP SCHEMA IF EXISTS " + schema + " CASCADE");
    legacy.migrate();
    sql.update(
        "INSERT INTO business.conversations(id,tenant,subject) VALUES('conversation','tenant','subject')");
    sql.update(
        "INSERT INTO business.turns(id,conversation_id,tenant,subject,idempotency_key,request_hash,message,release_hash,status) VALUES('turn','conversation','tenant','subject','key','request-hash','publish','release','running')");
  }

  private Flyway upgraded() {
    return Flyway.configure().dataSource(source).target("11").load();
  }

  private void entity(String call, String id, String title) {
    sql.update(
        "INSERT INTO business.reports(id,turn_id,call_id,title) VALUES(?,'turn',?,?)",
        id,
        call,
        title);
  }

  private void receipt(String call, String status, String result) {
    sql.update(
        "INSERT INTO business.tool_calls(turn_id,call_id,action_hash,status,result) VALUES('turn',?,'action',?,?)",
        call,
        status,
        result);
  }

  private GovernanceMapper mapper() throws Exception {
    var config = new Configuration();
    config.setMapUnderscoreToCamelCase(true);
    config.setCacheEnabled(false);
    config.setLocalCacheScope(LocalCacheScope.STATEMENT);
    config.addMapper(GovernanceMapper.class);
    var factory = new SqlSessionFactoryBean();
    factory.setDataSource(source);
    factory.setConfiguration(config);
    return new SqlSessionTemplate(factory.getObject()).getMapper(GovernanceMapper.class);
  }

  @Test
  void preservesAllHistoricalShapesAndExactReceiptBytesWithoutPromotingPending() throws Exception {
    String complete = "{ \"status\" : \"created\", \"report_id\" : \"report-original\" }";
    entity("matched", "report-original", "原始报告标题");
    receipt("matched", "completed", complete);
    entity("pending-matched", "pending-report", "已有实体但结果未知");
    receipt("pending-matched", "pending", null);
    receipt("pending-only", "pending", "unverified original bytes");
    receipt("receipt-only", "completed", "old completed receipt without entity");
    receipt("empty-completed", "completed", null);
    entity("entity-only", "orphan-report", "没有回执的原始报告");
    var entities =
        sql.queryForList("SELECT id,turn_id,call_id,title FROM business.reports ORDER BY call_id");
    var receipts =
        sql.queryForList(
            "SELECT turn_id,call_id,action_hash,status,result FROM business.tool_calls ORDER BY call_id");

    assertThat(upgraded().migrate().migrationsExecuted).isOne();

    assertThat(
            sql.queryForList(
                "SELECT id,turn_id,call_id,title FROM business.reports WHERE id IS NOT NULL ORDER BY call_id"))
        .isEqualTo(entities);
    assertThat(
            sql.queryForList(
                "SELECT turn_id,call_id,action_hash,status,result FROM business.reports WHERE action_hash IS NOT NULL ORDER BY call_id"))
        .isEqualTo(receipts);
    assertThat(sql.queryForObject("SELECT to_regclass('business.tool_calls')::text", String.class))
        .isNull();
    assertThat(
            sql.queryForMap(
                "SELECT id,title,action_hash,status,result FROM business.reports WHERE call_id='entity-only'"))
        .containsEntry("id", "orphan-report")
        .containsEntry("title", "没有回执的原始报告")
        .containsEntry("status", "legacy_untracked")
        .containsEntry("action_hash", null)
        .containsEntry("result", null);
    assertThat(
            sql.queryForList(
                "SELECT id,title FROM business.reports WHERE call_id IN ('pending-only','receipt-only','empty-completed')"))
        .hasSize(3)
        .allSatisfy(row -> assertThat(row).containsEntry("id", null).containsEntry("title", null));
    var mapper = mapper();
    assertThat(mapper.getToolCallEffect("turn", "matched").result()).isEqualTo(complete);
    assertThat(mapper.existsPendingToolCall("turn")).isTrue();
    var before = snapshot();
    assertThat(upgraded().migrate().migrationsExecuted).isZero();
    assertThat(snapshot()).isEqualTo(before);
  }

  @Test
  void migratedReceiptsReplayButUnknownHistoriesBlockWithoutAnotherPublication() throws Exception {
    String bytes = "{ \"report_id\": \"old\", \"status\": \"created\" }";
    receipt("completed", "completed", bytes);
    receipt("pending", "pending", null);
    receipt("empty-completed", "completed", null);
    entity("entity-only", "original", "preserved");
    upgraded().migrate();
    var leases = mock(ExecutionLeaseService.class);
    // Replay behavior is isolated here; GovernanceBoundariesIT verifies the real fence transaction.
    doAnswer(invocation -> ((Supplier<?>) invocation.getArgument(1)).get())
        .when(leases)
        .fenced(any(), any());
    var service =
        new ReportService(
            new MyBatisReportGateway(mapper()), leases, mock(ExecutionPolicyService.class));
    var claim =
        new Claim(
            new Command("command", "turn", "running", "attempt", 1, true, null),
            new Turn(
                "turn",
                "conversation",
                "tenant",
                "subject",
                "key",
                "request-hash",
                "publish",
                "release",
                "running",
                1,
                null,
                null));
    var before = snapshot();

    assertThat(service.report(claim, "completed", "action", "changed title")).isEqualTo(bytes);
    assertThatThrownBy(() -> service.report(claim, "completed", "changed", "title"))
        .isInstanceOf(Blocked.class)
        .hasMessage("effect_conflict");
    for (String call : List.of("pending", "empty-completed", "entity-only"))
      assertThatThrownBy(() -> service.report(claim, call, "action", "title"))
          .isInstanceOf(Blocked.class)
          .hasMessage("reconciliation_required");
    assertThat(snapshot()).isEqualTo(before);
  }

  @Test
  void unexpectedDependencyRollsBackTheEntireUpgradeBeforeOldReceiptTableIsDropped() {
    entity("matched", "original", "preserved");
    receipt("matched", "pending", null);
    var original = sql.queryForList("SELECT * FROM business.reports");
    sql.execute("CREATE VIEW business.external_effect_reader AS SELECT * FROM business.tool_calls");

    assertThatThrownBy(() -> upgraded().migrate())
        .hasStackTraceContaining("external_effect_reader");

    assertThat(sql.queryForList("SELECT * FROM business.reports")).isEqualTo(original);
    assertThat(sql.queryForObject("SELECT count(*) FROM business.tool_calls", Integer.class))
        .isOne();
    assertThat(
            sql.queryForObject(
                "SELECT count(*) FROM information_schema.columns WHERE table_schema='business' AND table_name='reports' AND column_name='action_hash'",
                Integer.class))
        .isZero();
    sql.execute("DROP VIEW business.external_effect_reader");
    assertThat(upgraded().migrate().migrationsExecuted).isOne();
  }

  @Test
  void publicationIsOneAtomicInsertAndOuterRollbackRemovesEntityAndReceiptTogether()
      throws Exception {
    upgraded().migrate();
    var mapper = mapper();
    var tx = new TransactionTemplate(new JdbcTransactionManager(source));
    String receipt = "{\"report_id\":\"new-report\",\"status\":\"created\"}";
    tx.executeWithoutResult(
        status -> {
          assertThat(
                  mapper.insertReportPublication(
                      "new-report", "turn", "call", "title", "action", receipt))
              .isOne();
          assertThat(mapper.getToolCallEffect("turn", "call").result()).isEqualTo(receipt);
          assertThat(mapper.existsPendingToolCall("turn")).isFalse();
          status.setRollbackOnly();
        });
    assertThat(snapshot()).isEmpty();
    assertThat(
            mapper.insertReportPublication(
                "new-report", "turn", "call", "title", "action", receipt))
        .isOne();
    var before = snapshot();
    assertThatThrownBy(
            () ->
                mapper.insertReportPublication(
                    "second-report", "turn", "call", "changed", "action", receipt))
        .isInstanceOf(org.springframework.dao.DuplicateKeyException.class);
    assertThat(snapshot()).isEqualTo(before);
  }

  private List<Map<String, Object>> snapshot() {
    return sql.queryForList("SELECT * FROM business.reports ORDER BY turn_id,call_id");
  }
}
