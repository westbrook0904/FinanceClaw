package com.financeclaw.m2;

import static org.assertj.core.api.Assertions.*;
import static org.mockito.ArgumentMatchers.*;
import static org.mockito.Mockito.*;

import com.financeclaw.app.artifact.ArtifactService;
import com.financeclaw.app.execution.ExecutionLeaseService;
import com.financeclaw.app.execution.ExecutionPolicyService;
import com.financeclaw.app.governance.GrantService;
import com.financeclaw.app.release.ReleaseService;
import com.financeclaw.app.report.ReportService;
import com.financeclaw.app.task.SubmitTaskService;
import com.financeclaw.app.task.TaskQueryService;
import com.financeclaw.bootstrap.FinanceClawApplication;
import com.financeclaw.domain.Governance.Grant;
import com.financeclaw.domain.Task.*;
import com.financeclaw.infrastructure.artifact.MyBatisArtifactGateway;
import com.financeclaw.infrastructure.governance.*;
import java.util.*;
import javax.sql.DataSource;
import org.junit.jupiter.api.*;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.boot.test.context.SpringBootTest;
import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.test.context.DynamicPropertyRegistry;
import org.springframework.test.context.DynamicPropertySource;
import org.springframework.test.util.AopTestUtils;
import org.springframework.transaction.support.TransactionTemplate;
import org.testcontainers.containers.PostgreSQLContainer;

/** Real PostgreSQL rollback and persisted-receipt checks for the split governance use cases. */
@SpringBootTest(
    classes = FinanceClawApplication.class,
    webEnvironment = SpringBootTest.WebEnvironment.NONE,
    properties = {
      "financeclaw.worker.enabled=false",
      "financeclaw.integrations.enabled=false",
      "financeclaw.memory.enabled=false"
    })
class GovernanceBoundariesIT {
  static final PostgreSQLContainer<?> PG =
      new PostgreSQLContainer<>("postgres:16.10-alpine")
          .withDatabaseName("governance_boundaries")
          .withUsername("test")
          .withPassword("test-only");

  static {
    PG.start();
  }

  @DynamicPropertySource
  static void database(DynamicPropertyRegistry registry) {
    registry.add("spring.datasource.url", PG::getJdbcUrl);
    registry.add("spring.datasource.username", PG::getUsername);
    registry.add("spring.datasource.password", PG::getPassword);
  }

  @Autowired SubmitTaskService submissions;
  @Autowired TaskQueryService queries;
  @Autowired ExecutionLeaseService leases;
  @Autowired ExecutionPolicyService policy;
  @Autowired ReleaseService releases;
  @Autowired GrantService grants;
  @Autowired ArtifactService artifacts;
  @Autowired ReportService reports;
  @Autowired MyBatisGrantGateway grantStore;
  @Autowired MyBatisArtifactGateway artifactStore;
  @Autowired MyBatisReportGateway reportStore;
  @Autowired GovernanceMapper mapper;
  @Autowired TransactionTemplate tx;
  @Autowired DataSource source;
  private JdbcTemplate sql;
  private final Owner owner = new Owner("local", "developer");

  @BeforeEach
  void reset() {
    sql = new JdbcTemplate(source);
    sql.execute("TRUNCATE business.conversations CASCADE");
  }

  private Claim claim() {
    String conversation = submissions.createConversation(owner);
    submissions.submit(owner, conversation, "request", "governance boundary");
    return Objects.requireNonNull(leases.claim("governance-boundaries", 60000));
  }

  private Map<String, List<String>> rows() {
    var rows = new TreeMap<String, List<String>>();
    for (String table : List.of("grants", "grant_changes", "artifacts", "reports", "turn_policies"))
      rows.put(
          table,
          sql.queryForList(
              "SELECT row_to_json(t)::text FROM business."
                  + table
                  + " t ORDER BY row_to_json(t)::text",
              String.class));
    return rows;
  }

  @Test
  void failureAfterGrantReceiptWriteRollsBackGrantAndReceipt() {
    String conversation = submissions.createConversation(owner);
    // Spy the target, not the repository proxy. Its Mapper still joins the real Spring transaction.
    var failing = spy(AopTestUtils.<MyBatisGrantGateway>getUltimateTargetObject(grantStore));
    doAnswer(
            invocation -> {
              invocation.callRealMethod();
              throw new IllegalStateException("grant-receipt-failed");
            })
        .when(failing)
        .grantChange(anyString(), anyString(), anyString());
    var service =
        new GrantService(queries, failing, tx, new com.financeclaw.app.task.TaskEventHub());
    var before = rows();
    assertThatThrownBy(
            () -> service.changeGrant(owner, conversation, "change", 1, List.of("artifact:read")))
        .hasMessage("grant-receipt-failed");
    assertThat(rows()).isEqualTo(before);
  }

  @Test
  void failureAfterScopeWriteRollsBackNestedArtifactInsert() {
    var claim = claim();
    var failing = spy(AopTestUtils.<MyBatisArtifactGateway>getUltimateTargetObject(artifactStore));
    doAnswer(
            invocation -> {
              invocation.callRealMethod();
              throw new IllegalStateException("artifact-scope-failed");
            })
        .when(failing)
        .setArtifactScope(anyString(), anyString());
    var service = new ArtifactService(failing, leases, policy, releases, tx);
    var before = rows();
    assertThatThrownBy(() -> service.archive(claim, Map.of("result", "合成样本"), "catalog:read"))
        .hasMessage("artifact-scope-failed");
    assertThat(rows()).isEqualTo(before);
  }

  @Test
  void failureAfterPublicationRollsBackReportAndReceipt() {
    var claim = claim();
    var failing = spy(AopTestUtils.<MyBatisReportGateway>getUltimateTargetObject(reportStore));
    doAnswer(
            invocation -> {
              invocation.callRealMethod();
              throw new IllegalStateException("report-publication-failed");
            })
        .when(failing)
        .publishReport(
            anyString(), anyString(), anyString(), anyString(), anyString(), anyString());
    var service = new ReportService(failing, leases, policy);
    var before = rows();
    assertThatThrownBy(() -> service.report(claim, "call", "action", "合成报告"))
        .hasMessage("report-publication-failed");
    assertThat(rows()).isEqualTo(before);
  }

  @Test
  void allThreeUseCasesJoinTheCallersSpringTransaction() {
    var claim = claim();
    var before = rows();
    tx.executeWithoutResult(
        status -> {
          artifacts.archive(claim, Map.of("value", "test"), "catalog:read");
          reports.report(claim, "call", "action", "test");
          grants.changeGrant(
              owner, claim.turn().conversationId(), "change", 1, List.of("artifact:read"));
          assertThat(rows()).isNotEqualTo(before);
          status.setRollbackOnly();
        });
    assertThat(rows()).isEqualTo(before);
  }

  @Test
  void oldGrantReceiptReplaysAfterTheCurrentGrantHasChangedAgain() {
    String conversation = submissions.createConversation(owner);
    // Frozen pre-split digest: length-prefixed revision "1" and canonical ["artifact:read"].
    String oldHash = "1206dcff62b49927c8af8c3741c940a2d94cde6673adafc7c141e7ee50780c7a";
    tx.executeWithoutResult(
        status -> {
          mapper.getGrantForUpdate(conversation);
          mapper.updateGrant(conversation, "[\"artifact:read\"]");
          mapper.insertGrantChange(conversation, "old-request", oldHash);
          mapper.updateGrant(conversation, "[]");
        });
    var before = rows();
    assertThat(
            grants.changeGrant(
                owner, conversation, "old-request", 1, List.of("artifact:read", "artifact:read")))
        .isEqualTo(new Grant(conversation, 2, "[\"artifact:read\"]"));
    assertThat(rows()).isEqualTo(before);
    assertThatThrownBy(() -> grants.changeGrant(owner, conversation, "old-request", 1, List.of()))
        .hasMessage("idempotency_conflict");
    assertThat(rows()).isEqualTo(before);
  }

  @Test
  void oldCompletedEffectReplaysWithoutPublishingAnotherReport() {
    var claim = claim();
    String oldResult = "{\"report_id\":\"previous-report\",\"status\":\"created\"}";
    sql.update(
        "INSERT INTO business.reports(turn_id,call_id,action_hash,status,result) VALUES(?,?,?,'completed',?)",
        claim.turn().id(),
        "old-call",
        "old-action",
        oldResult);
    var before = rows();
    assertThat(reports.report(claim, "old-call", "old-action", "ignored")).isEqualTo(oldResult);
    assertThatThrownBy(() -> reports.report(claim, "old-call", "new-action", "ignored"))
        .hasMessage("effect_conflict");
    assertThat(rows()).isEqualTo(before);
  }

  @Test
  void uncertainPersistedEffectBlocksReportAndArtifactWrites() {
    var claim = claim();
    sql.update(
        "INSERT INTO business.reports(turn_id,call_id,action_hash,status) VALUES(?,?,?,'pending')",
        claim.turn().id(),
        "uncertain-call",
        "action");
    var before = rows();
    assertThatThrownBy(() -> reports.report(claim, "uncertain-call", "action", "ignored"))
        .hasMessage("reconciliation_required");
    assertThatThrownBy(() -> artifacts.archive(claim, Map.of("value", "test")))
        .hasMessage("reconciliation_required");
    assertThat(rows()).isEqualTo(before);
  }
}
