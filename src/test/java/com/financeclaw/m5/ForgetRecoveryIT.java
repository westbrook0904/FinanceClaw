package com.financeclaw.m5;

import static org.assertj.core.api.Assertions.*;
import static org.mockito.Mockito.*;

import com.financeclaw.app.memory.*;
import com.financeclaw.app.memory.port.*;
import com.financeclaw.bootstrap.FinanceClawApplication;
import com.financeclaw.domain.Memory.*;
import com.financeclaw.domain.Task.*;
import com.financeclaw.domain.memory.*;
import com.financeclaw.fixture.TaskFixture;
import com.financeclaw.infrastructure.memory.*;
import java.nio.file.*;
import java.util.*;
import java.util.concurrent.*;
import javax.sql.DataSource;
import org.junit.jupiter.api.*;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.boot.test.context.SpringBootTest;
import org.springframework.context.annotation.Import;
import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.jdbc.datasource.DriverManagerDataSource;
import org.springframework.test.context.*;
import org.springframework.transaction.support.TransactionTemplate;
import org.testcontainers.containers.PostgreSQLContainer;

@Import(TaskFixture.class)
@SpringBootTest(
    classes = FinanceClawApplication.class,
    properties = {
      "financeclaw.worker.enabled=false", "financeclaw.integrations.enabled=false",
      "financeclaw.memory.enabled=true", "financeclaw.memory.forget-ledger.enabled=true",
      "financeclaw.auth.token=privacy-test-only"
    })
class ForgetRecoveryIT {
  static final PostgreSQLContainer<?> PG = new PostgreSQLContainer<>("postgres:16.10-alpine");
  static final PostgreSQLContainer<?> LEDGER = new PostgreSQLContainer<>("postgres:16.10-alpine");

  static {
    PG.start();
    LEDGER.start();
    try {
      ledgerSql()
          .execute(
              Files.readString(Path.of("src/main/resources/privacy-ledger/V1__forget_ledger.sql")));
    } catch (Exception failure) {
      throw new ExceptionInInitializerError(failure);
    }
  }

  static JdbcTemplate ledgerSql() {
    return new JdbcTemplate(
        new DriverManagerDataSource(
            LEDGER.getJdbcUrl(), LEDGER.getUsername(), LEDGER.getPassword()));
  }

  @DynamicPropertySource
  static void properties(DynamicPropertyRegistry registry) {
    registry.add("spring.datasource.url", PG::getJdbcUrl);
    registry.add("spring.datasource.username", PG::getUsername);
    registry.add("spring.datasource.password", PG::getPassword);
    registry.add("financeclaw.memory.forget-ledger.url", LEDGER::getJdbcUrl);
    registry.add("financeclaw.memory.forget-ledger.username", LEDGER::getUsername);
    registry.add("financeclaw.memory.forget-ledger.password", LEDGER::getPassword);
  }

  @Autowired MemoryService memory;
  @Autowired MemoryRepository repository;
  @Autowired ForgetRecoveryRepository recovery;
  @Autowired ForgetLedger ledger;
  @Autowired TransactionTemplate tx;
  @Autowired DataSource source;
  @Autowired TaskFixture tasks;
  JdbcTemplate sql;
  final Owner alice = new Owner("local", "alice"), bob = new Owner("local", "bob");

  @BeforeEach
  void reset() {
    sql = new JdbcTemplate(source);
    sql.execute("TRUNCATE business.conversations CASCADE");
    sql.execute("TRUNCATE memory.owners,memory.jobs,memory.audit,memory.operations CASCADE");
    sql.update(
        "UPDATE memory.privacy_ledger_binding SET lineage=?,active=false",
        UUID.randomUUID().toString());
  }

  PrivacyRecoveryService offline() {
    return new PrivacyRecoveryService(repository, recovery, ledger, tx);
  }

  MemoryForgetService service(ForgetRecoveryRepository local, ForgetLedger external) {
    return new MemoryForgetService(repository, tx, local, external);
  }

  MemoryProcessingService worker() {
    return new MemoryProcessingService(
        repository,
        tx,
        text -> new Extraction(List.of()),
        text -> {
          var values = new double[1024];
          values[0] = 1;
          return EmbeddingVector.of(values);
        },
        "test");
  }

  String seed(Owner owner, String text) {
    var accepted =
        tasks.submit(owner, tasks.createConversation(owner), UUID.randomUUID().toString(), text);
    var fact =
        new Fact(
            UUID.randomUUID().toString(),
            accepted.turnId(),
            owner.tenant(),
            owner.subject(),
            memory.privacy(owner).epoch(),
            "response_style",
            text,
            text,
            1,
            "active");
    tx.executeWithoutResult(
        status -> {
          repository.insertFact(fact);
          repository.freeze(accepted.turnId(), owner, fact.epoch());
          repository.indexJob(fact.id(), "test");
          var vector = new double[1024];
          vector[0] = 1;
          repository.project(fact, "test", EmbeddingVector.of(vector));
        });
    return fact.id();
  }

  long count(String table) {
    return sql.queryForObject("SELECT count(*) FROM memory." + table, Long.class);
  }

  @Test
  void independentFailureKeepsBarrierAndRetryUsesTheSameFact() {
    offline().bootstrap();
    String id = seed(alice, "private-old-value");
    var unavailable = mock(ForgetLedger.class);
    when(unavailable.enabled()).thenReturn(true);
    doThrow(new IllegalStateException("transport-with-sensitive-detail"))
        .when(unavailable)
        .append(any());
    assertThatThrownBy(() -> service(recovery, unavailable).forget(alice, 1, "forget-one"))
        .hasMessage("forget_ledger_unavailable");
    var pending = recovery.pending(alice);
    assertThat(pending).isNotNull();
    assertThat(memory.privacy(alice)).isEqualTo(new Privacy("local", "alice", 1, true));
    assertThat(memory.list(alice)).isEmpty();
    assertThat(repository.fact(id).content()).isEqualTo("private-old-value");
    assertThatThrownBy(() -> tasks.submit(alice, tasks.createConversation(alice), "blocked", "new"))
        .hasMessage("purge_pending");
    assertThatThrownBy(() -> memory.rebuild(alice, "rebuild")).hasMessage("purge_pending");
    worker().poll();
    assertThat(memory.privacy(alice).purgePending()).isTrue();
    assertThat(count("audit")).isZero();
    assertThatThrownBy(() -> memory.forget(alice, 1, "different-key")).hasMessage("forget_pending");
    assertThat(memory.forget(alice, 1, "forget-one")).isEqualTo(2);
    assertThat(ledger.snapshot(recovery.binding().lineage())).containsExactly(pending);
    assertThat(repository.fact(id).content()).isNull();
    assertThat(recovery.pending(alice)).isNull();
    worker().poll();
    assertThat(memory.privacy(alice).purgePending()).isFalse();
  }

  @Test
  void externalCommitThenTimeoutAndLocalReceiptFailureRemainRecoverable() {
    offline().bootstrap();
    seed(alice, "private-old-value");
    var uncertain = spy(ledger);
    doAnswer(
            call -> {
              ledger.append(call.getArgument(0));
              throw new IllegalStateException("timeout");
            })
        .when(uncertain)
        .append(any());
    assertThatThrownBy(() -> service(recovery, uncertain).forget(alice, 1, "unknown"))
        .hasMessage("forget_ledger_unavailable");
    assertThat(ledger.snapshot(recovery.binding().lineage())).hasSize(1);
    var failing = spy(recovery);
    doAnswer(
            call -> {
              recovery.apply(call.getArgument(0));
              throw new IllegalStateException("after-local-write");
            })
        .when(failing)
        .apply(any());
    assertThatThrownBy(() -> service(failing, ledger).forget(alice, 1, "unknown"))
        .hasMessage("after-local-write");
    assertThat(memory.privacy(alice).epoch()).isEqualTo(1);
    assertThat(recovery.pending(alice)).isNotNull();
    assertThat(count("forget_applied")).isZero();
    assertThat(count("operations")).isZero();
    assertThat(offline().replay().result()).isEqualTo("privacy_replayed");
    assertThat(memory.forget(alice, 1, "unknown")).isEqualTo(2);
    assertThat(count("forget_applied")).isEqualTo(1);
    assertThat(count("audit")).isEqualTo(2);
    assertThatThrownBy(() -> memory.forget(alice, 2, "unknown")).hasMessage("idempotency_conflict");
  }

  @Test
  void sameKeyConcurrentRequestsConvergeAndDisabledConfigurationCannotBypassBinding()
      throws Exception {
    offline().bootstrap();
    seed(alice, "old");
    try (var threads = Executors.newFixedThreadPool(2)) {
      var start = new CountDownLatch(1);
      var first =
          threads.submit(
              () -> {
                start.await();
                return memory.forget(alice, 1, "same");
              });
      var second =
          threads.submit(
              () -> {
                start.await();
                return memory.forget(alice, 1, "same");
              });
      start.countDown();
      assertThat(first.get(15, TimeUnit.SECONDS)).isEqualTo(2);
      assertThat(second.get(15, TimeUnit.SECONDS)).isEqualTo(2);
    }
    assertThat(ledger.snapshot(recovery.binding().lineage())).hasSize(1);
    assertThat(count("forget_applied")).isEqualTo(1);
    var disabled = mock(ForgetLedger.class);
    assertThatThrownBy(() -> service(recovery, disabled).forget(alice, 2, "bypass"))
        .hasMessage("forget_ledger_required");
    assertThatThrownBy(() -> tx.executeWithoutResult(status -> memory.forget(alice, 2, "ambient")))
        .hasMessage("Forget orchestration requires no ambient transaction");
  }

  @Test
  void restoresActualOldDumpAfterSourceLossAndReplaysWithoutErasingNewEpoch() throws Exception {
    offline().bootstrap();
    String old = seed(alice, "private-before-backup");
    String other = seed(bob, "other-owner-memory");
    command(
        "pg_dump",
        "-U",
        PG.getUsername(),
        "-d",
        PG.getDatabaseName(),
        "-Fc",
        "-f",
        "/tmp/old.dump");
    memory.forget(alice, 1, "after-backup-one");
    worker().poll();
    memory.forget(alice, 2, "after-backup-two");
    worker().poll();
    var authority = ledger.snapshot(recovery.binding().lineage());
    assertThat(authority).hasSize(2);
    // The source schemas really disappear; the ledger lives in a different PostgreSQL container.
    sql.execute("DROP SCHEMA memory,channel,agent_runtime,business CASCADE");
    command(
        "pg_restore",
        "-U",
        PG.getUsername(),
        "-d",
        PG.getDatabaseName(),
        "--clean",
        "--if-exists",
        "/tmp/old.dump");
    assertThat(repository.fact(old).content()).isEqualTo("private-before-backup");
    assertThat(memory.privacy(alice).epoch()).isEqualTo(1);
    assertThat(ledger.snapshot(recovery.binding().lineage())).isEqualTo(authority);
    var report = offline().replay();
    assertThat(report.result()).isEqualTo("privacy_replayed");
    assertThat(report.facts()).isEqualTo(2);
    assertThat(repository.fact(old).content()).isNull();
    assertThat(repository.fact(other).content()).isEqualTo("other-owner-memory");
    assertThat(memory.privacy(alice)).isEqualTo(new Privacy("local", "alice", 3, false));
    assertThat(recovery.unsafeRows()).isZero();
    assertThat(memory.forget(alice, 1, "after-backup-one")).isEqualTo(2);
    String newer = seed(alice, "new-epoch-memory");
    long auditCount = count("audit");
    long oldRevision = repository.fact(old).revision();
    assertThat(offline().replay()).isEqualTo(report);
    assertThat(repository.fact(newer).content()).isEqualTo("new-epoch-memory");
    assertThat(repository.fact(old).revision()).isEqualTo(oldRevision);
    assertThat(count("audit")).isEqualTo(auditCount);
  }

  @Test
  void historyBootstrapPreservesLegacyRequestKeysAndRejectsStaleAuthority() {
    var legacy = new MemoryForgetService(repository, tx);
    memory.privacy(alice);
    legacy.forget(alice, 1, "legacy-key");
    worker().poll();
    assertThat(offline().bootstrap().facts()).isEqualTo(1);
    assertThat(ledger.snapshot(recovery.binding().lineage()).getFirst().key())
        .isEqualTo("legacy-key");
    assertThat(offline().bootstrap().facts()).isEqualTo(1);
    sql.update("UPDATE memory.owners SET epoch=3 WHERE tenant='local' AND subject='alice'");
    assertThatThrownBy(() -> offline().replay()).hasMessage("privacy_owner_epoch_conflict");
  }

  @Test
  void foreignBindingAndFutureEpochRowsKeepRestorationBlocked() {
    offline().bootstrap();
    String id = seed(alice, "old");
    memory.forget(alice, 1, "forgot");
    worker().poll();
    sql.update("UPDATE memory.facts SET epoch=99 WHERE id=?", id);
    assertThatThrownBy(() -> offline().replay()).hasMessage("privacy_replay_unsafe");
    sql.update("UPDATE memory.privacy_ledger_binding SET lineage=?", UUID.randomUUID().toString());
    assertThatThrownBy(() -> offline().replay()).hasMessage("forget_ledger_uninitialized");
  }

  @Test
  void bootstrapActivationFailureCanRetryWithoutCreatingNewEvents() {
    memory.privacy(alice);
    new MemoryForgetService(repository, tx).forget(alice, 1, "historical");
    worker().poll();
    var failing = spy(recovery);
    doThrow(new IllegalStateException("activation-failed")).when(failing).activate();
    assertThatThrownBy(
            () -> new PrivacyRecoveryService(repository, failing, ledger, tx).bootstrap())
        .hasMessage("activation-failed");
    assertThat(recovery.binding().active()).isFalse();
    var external = ledger.snapshot(recovery.binding().lineage());
    assertThat(external).hasSize(1);
    assertThat(offline().bootstrap().facts()).isEqualTo(1);
    assertThat(recovery.binding().active()).isTrue();
    assertThat(ledger.snapshot(recovery.binding().lineage())).isEqualTo(external);
  }

  @Test
  void foreignPendingCannotWriteAnotherLineageBeforeRecoveryFails() {
    offline().bootstrap();
    memory.privacy(alice);
    String foreign = UUID.randomUUID().toString();
    ledger.initialize(foreign, List.of());
    var fact =
        new ForgetFact(
            UUID.randomUUID().toString(),
            recovery.binding().lineage(),
            alice,
            1,
            "pending",
            ForgetFact.hash(1));
    tx.executeWithoutResult(status -> recovery.prepare(fact));
    sql.update("UPDATE memory.forget_requests SET lineage=?", foreign);
    assertThatThrownBy(() -> offline().replay()).hasMessage("privacy_pending_conflict");
    assertThat(ledger.snapshot(foreign)).isEmpty();
    assertThat(recovery.pending(alice)).isNotNull();
  }

  @Test
  void reversedOriginalForgetAuditOrderCannotBeRepairedBySortingEpochs() {
    memory.privacy(alice);
    var legacy = new MemoryForgetService(repository, tx);
    legacy.forget(alice, 1, "first");
    legacy.forget(alice, 2, "second");
    worker().poll();
    // Both epochs and receipts remain present, but the source audit order is impossible.
    sql.update("UPDATE memory.audit SET epoch=5-epoch WHERE action='forget'");
    assertThatThrownBy(() -> offline().bootstrap()).hasMessage("forget_history_invalid");
    assertThat(recovery.binding().active()).isFalse();
  }

  private static void command(String... args) throws Exception {
    var result = PG.execInContainer(args);
    assertThat(result.getExitCode()).as("PostgreSQL backup/restore command").isZero();
  }
}
