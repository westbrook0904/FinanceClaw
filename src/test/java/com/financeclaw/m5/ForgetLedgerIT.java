package com.financeclaw.m5;

import static org.assertj.core.api.Assertions.*;

import com.financeclaw.domain.Task.Owner;
import com.financeclaw.domain.memory.ForgetFact;
import com.financeclaw.infrastructure.memory.PostgresForgetLedger;
import java.lang.reflect.InvocationTargetException;
import java.lang.reflect.Proxy;
import java.sql.Connection;
import java.sql.PreparedStatement;
import java.sql.SQLException;
import java.util.Collections;
import java.util.List;
import java.util.UUID;
import java.util.concurrent.Callable;
import java.util.concurrent.CountDownLatch;
import java.util.concurrent.Executors;
import java.util.concurrent.TimeUnit;
import javax.sql.DataSource;
import org.junit.jupiter.api.AfterAll;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;
import org.springframework.core.io.ClassPathResource;
import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.jdbc.datasource.DelegatingDataSource;
import org.springframework.jdbc.datasource.DriverManagerDataSource;
import org.springframework.jdbc.datasource.init.ResourceDatabasePopulator;
import org.springframework.jdbc.support.JdbcTransactionManager;
import org.springframework.transaction.support.TransactionTemplate;
import org.testcontainers.containers.PostgreSQLContainer;

/** Real, separately provisioned PostgreSQL authority; no business Flyway migrations or mocks. */
class ForgetLedgerIT {
  private static final PostgreSQLContainer<?> PG =
      new PostgreSQLContainer<>("postgres:16.10-alpine").withDatabaseName("privacy_ledger_test");
  private static DataSource source;
  private static DataSource business;
  private static JdbcTemplate sql;
  private static JdbcTemplate businessSql;
  private PostgresForgetLedger ledger;
  private String lineage;
  private final Owner owner = new Owner("tenant-a", "alice");

  @BeforeAll
  static void prepare() {
    PG.start();
    source = new DriverManagerDataSource(PG.getJdbcUrl(), PG.getUsername(), PG.getPassword());
    sql = new JdbcTemplate(source);
    new ResourceDatabasePopulator(new ClassPathResource("privacy-ledger/V1__forget_ledger.sql"))
        .execute(source);
    sql.execute("CREATE DATABASE privacy_ledger_business_test");
    business =
        new DriverManagerDataSource(
            PG.getJdbcUrl().replace("/privacy_ledger_test", "/privacy_ledger_business_test"),
            PG.getUsername(),
            PG.getPassword());
    businessSql = new JdbcTemplate(business);
    businessSql.execute("CREATE SCHEMA memory");
    businessSql.execute("CREATE TABLE memory.privacy_ledger_binding(id integer PRIMARY KEY)");
    businessSql.execute("CREATE TABLE transaction_probe(id integer PRIMARY KEY)");
    sql.execute("CREATE ROLE privacy_runtime LOGIN PASSWORD 'ledger-test-only'");
    sql.execute("ALTER ROLE privacy_runtime SET synchronous_commit=off");
    sql.execute("GRANT USAGE ON SCHEMA privacy_ledger TO privacy_runtime");
    sql.execute("GRANT SELECT,INSERT,UPDATE ON privacy_ledger.lineages TO privacy_runtime");
    sql.execute("GRANT SELECT,INSERT ON privacy_ledger.forget_facts TO privacy_runtime");
  }

  @AfterAll
  static void stop() {
    PG.stop();
  }

  @BeforeEach
  void reset() {
    sql.execute("TRUNCATE privacy_ledger.forget_facts,privacy_ledger.lineages");
    businessSql.execute("TRUNCATE transaction_probe");
    ledger = new PostgresForgetLedger(source);
    lineage = UUID.randomUUID().toString();
  }

  private ForgetFact fact(Owner selectedOwner, long epoch, String key) {
    return new ForgetFact(
        UUID.randomUUID().toString(), lineage, selectedOwner, epoch, key, ForgetFact.hash(epoch));
  }

  private ForgetFact fact(long epoch) {
    return fact(owner, epoch, "forget-" + epoch);
  }

  @Test
  void initializeAtomicallyImportsAndSortsHistoryAndAllowsAnExactRetry() {
    var first = fact(1);
    var second = fact(2);
    var bob = fact(new Owner("tenant-a", "bob"), 1, "forget-bob");
    ledger.initialize(lineage, List.of(bob, second, first));
    assertThat(ledger.enabled()).isTrue();
    assertThat(ledger.snapshot(lineage)).containsExactly(first, second, bob);
    ledger.initialize(lineage, List.of(first, second, bob));
    ledger.append(first);
    ledger.append(second);
    assertThat(ledger.snapshot(lineage)).hasSize(3);
    assertThatThrownBy(() -> ledger.initialize(lineage, List.of()))
        .hasMessage("forget_ledger_conflict");
    assertThatThrownBy(() -> ledger.initialize(lineage, List.of(fact(1))))
        .hasMessage("forget_ledger_conflict");
  }

  @Test
  void existingInitializationMayHaveAdditionalAcknowledgedFactsButCannotBeExtendedByBootstrap() {
    var first = fact(1);
    var second = fact(2);
    ledger.initialize(lineage, List.of(first));
    ledger.append(second);
    ledger.initialize(lineage, List.of(first));
    assertThatThrownBy(() -> ledger.initialize(lineage, List.of(first, second, fact(3))))
        .hasMessage("forget_ledger_conflict");
    assertThat(ledger.snapshot(lineage)).containsExactly(first, second);
  }

  @Test
  void allIdentitiesMustMatchForAppendReplayAndEpochsMustRemainContinuous() {
    var first = fact(1);
    ledger.initialize(lineage, List.of());
    ledger.append(first);
    ledger.append(first);
    assertThatThrownBy(() -> ledger.append(fact(1))).hasMessage("forget_ledger_conflict");
    assertThatThrownBy(() -> ledger.append(fact(3))).hasMessage("forget_ledger_conflict");
    assertThatThrownBy(() -> ledger.append(fact(owner, 2, first.key())))
        .hasMessage("forget_ledger_conflict");
    var differentOwner =
        new ForgetFact(
            first.eventId(),
            lineage,
            new Owner("tenant-b", "alice"),
            1,
            first.key(),
            first.requestHash());
    assertThatThrownBy(() -> ledger.append(differentOwner)).hasMessage("forget_ledger_conflict");
    ledger.append(fact(2));
    assertThat(ledger.snapshot(lineage)).hasSize(2);
  }

  @Test
  void missingRegistryAndInvalidOrMixedHistoryCannotCreatePartialState() {
    assertThatThrownBy(() -> ledger.append(fact(1))).hasMessage("forget_ledger_uninitialized");
    assertThatThrownBy(() -> ledger.snapshot(lineage)).hasMessage("forget_ledger_uninitialized");
    assertThatThrownBy(() -> ledger.initialize(lineage, List.of(fact(2))))
        .hasMessage("forget_ledger_invalid_history");
    var wrongLineage =
        new ForgetFact(
            UUID.randomUUID().toString(),
            UUID.randomUUID().toString(),
            owner,
            1,
            "foreign",
            ForgetFact.hash(1));
    assertThatThrownBy(() -> ledger.initialize(lineage, List.of(fact(1), wrongLineage)))
        .hasMessage("forget_ledger_invalid_history");
    var duplicate = fact(1);
    assertThatThrownBy(() -> ledger.initialize(lineage, List.of(duplicate, duplicate)))
        .hasMessage("forget_ledger_invalid_history");
    assertThatThrownBy(() -> ledger.snapshot("not-a-uuid"))
        .hasMessage("forget_ledger_invalid_history");
    assertThat(sql.queryForObject("SELECT count(*) FROM privacy_ledger.lineages", Long.class))
        .isZero();
  }

  @Test
  void lineagesAreIsolatedAndGlobalEventConflictRollsBackWholeInitialization() {
    var first = fact(1);
    ledger.initialize(lineage, List.of(first));
    String anotherLineage = UUID.randomUUID().toString();
    var beforeConflict =
        new ForgetFact(
            UUID.randomUUID().toString(), anotherLineage, owner, 1, "first", ForgetFact.hash(1));
    var reusedId =
        new ForgetFact(first.eventId(), anotherLineage, owner, 2, "second", ForgetFact.hash(2));
    assertThatThrownBy(() -> ledger.initialize(anotherLineage, List.of(beforeConflict, reusedId)))
        .hasMessage("forget_ledger_conflict");
    assertThatThrownBy(() -> ledger.snapshot(anotherLineage))
        .hasMessage("forget_ledger_uninitialized");
    var independent =
        new ForgetFact(
            UUID.randomUUID().toString(),
            anotherLineage,
            owner,
            1,
            first.key(),
            first.requestHash());
    ledger.initialize(anotherLineage, List.of(independent));
    assertThat(ledger.snapshot(anotherLineage)).containsExactly(independent);
    assertThat(ledger.snapshot(lineage)).containsExactly(first);
  }

  @Test
  void simultaneousIdenticalAppendsCommitExactlyOnceAndRivalIdentitiesCannotWinTogether()
      throws Exception {
    var first = fact(1);
    ledger.initialize(lineage, List.of());
    var gate = new CountDownLatch(1);
    try (var executor = Executors.newFixedThreadPool(4)) {
      Callable<Void> duplicate =
          () -> {
            assertThat(gate.await(10, TimeUnit.SECONDS)).isTrue();
            ledger.append(first);
            return null;
          };
      var futures = List.of(executor.submit(duplicate), executor.submit(duplicate));
      gate.countDown();
      for (var future : futures) future.get(15, TimeUnit.SECONDS);
      var nextGate = new CountDownLatch(1);
      Callable<Boolean> rival =
          () -> {
            assertThat(nextGate.await(10, TimeUnit.SECONDS)).isTrue();
            try {
              ledger.append(fact(2));
              return true;
            } catch (IllegalStateException failure) {
              assertThat(failure).hasMessage("forget_ledger_conflict");
              return false;
            }
          };
      var a = executor.submit(rival);
      var b = executor.submit(rival);
      nextGate.countDown();
      assertThat(List.of(a.get(15, TimeUnit.SECONDS), b.get(15, TimeUnit.SECONDS)))
          .containsExactlyInAnyOrder(true, false);
    }
    assertThat(ledger.snapshot(lineage)).hasSize(2);
  }

  @Test
  void independentCommitSurvivesBusinessRollback() {
    var first = fact(1);
    ledger.initialize(lineage, List.of());
    var transaction = new TransactionTemplate(new JdbcTransactionManager(business));
    assertThatThrownBy(
            () ->
                transaction.execute(
                    status -> {
                      businessSql.update("INSERT INTO transaction_probe(id) VALUES (1)");
                      ledger.append(first);
                      throw new IllegalStateException("business_failed_after_ledger_commit");
                    }))
        .hasMessage("business_failed_after_ledger_commit");
    assertThat(businessSql.queryForObject("SELECT count(*) FROM transaction_probe", Long.class))
        .isZero();
    assertThat(ledger.snapshot(lineage)).containsExactly(first);
  }

  @Test
  void snapshotRetainsItsTransactionViewWhenAnotherConnectionCommitsBetweenReads()
      throws Exception {
    var first = fact(1);
    var second = fact(2);
    ledger.initialize(lineage, List.of(first));
    var registryRead = new CountDownLatch(1);
    var writerCommitted = new CountDownLatch(1);
    var gatedSource =
        new DelegatingDataSource(source) {
          @Override
          public Connection getConnection() throws SQLException {
            Connection connection = super.getConnection();
            return (Connection)
                Proxy.newProxyInstance(
                    Connection.class.getClassLoader(),
                    new Class<?>[] {Connection.class},
                    (proxy, method, args) -> {
                      try {
                        Object result = method.invoke(connection, args);
                        if (method.getName().equals("prepareStatement")
                            && args[0] instanceof String query
                            && query.startsWith("SELECT lineage FROM privacy_ledger.lineages")) {
                          var statement = (PreparedStatement) result;
                          return Proxy.newProxyInstance(
                              PreparedStatement.class.getClassLoader(),
                              new Class<?>[] {PreparedStatement.class},
                              (statementProxy, statementMethod, statementArgs) -> {
                                try {
                                  Object statementResult =
                                      statementMethod.invoke(statement, statementArgs);
                                  if (statementMethod.getName().equals("executeQuery")) {
                                    registryRead.countDown();
                                    assertThat(writerCommitted.await(10, TimeUnit.SECONDS))
                                        .isTrue();
                                  }
                                  return statementResult;
                                } catch (InvocationTargetException invoked) {
                                  throw invoked.getCause();
                                }
                              });
                        }
                        return result;
                      } catch (InvocationTargetException invoked) {
                        throw invoked.getCause();
                      }
                    });
          }
        };
    try (var executor = Executors.newSingleThreadExecutor()) {
      var read = executor.submit(() -> new PostgresForgetLedger(gatedSource).snapshot(lineage));
      try {
        assertThat(registryRead.await(10, TimeUnit.SECONDS)).isTrue();
        ledger.append(second);
      } finally {
        writerCommitted.countDown();
      }
      assertThat(read.get(15, TimeUnit.SECONDS)).containsExactly(first);
    }
    assertThat(ledger.snapshot(lineage)).containsExactly(first, second);
  }

  @Test
  void businessDatabaseIsRejectedEvenBeforeAnyLedgerSchemaIsAvailable() {
    var main = new PostgresForgetLedger(business);
    assertThatThrownBy(() -> main.initialize(lineage, List.of()))
        .hasMessage("forget_ledger_same_database");
    assertThatThrownBy(() -> main.append(fact(1))).hasMessage("forget_ledger_same_database");
    assertThatThrownBy(() -> main.snapshot(lineage)).hasMessage("forget_ledger_same_database");
  }

  @Test
  void runtimeRoleCanAppendAndSnapshotButCannotMutateOrEraseFacts() {
    var runtimeSource =
        new DriverManagerDataSource(PG.getJdbcUrl(), "privacy_runtime", "ledger-test-only");
    var runtime = new PostgresForgetLedger(runtimeSource);
    var first = fact(1);
    runtime.initialize(lineage, List.of(first));
    runtime.append(fact(2));
    assertThat(runtime.snapshot(lineage)).hasSize(2);
    var runtimeSql = new JdbcTemplate(runtimeSource);
    assertThatThrownBy(
            () -> runtimeSql.update("UPDATE privacy_ledger.forget_facts SET from_epoch=3"))
        .isInstanceOf(org.springframework.dao.DataAccessException.class);
    assertThatThrownBy(() -> runtimeSql.update("DELETE FROM privacy_ledger.forget_facts"))
        .isInstanceOf(org.springframework.dao.DataAccessException.class);
    assertThatThrownBy(() -> runtimeSql.execute("TRUNCATE privacy_ledger.forget_facts"))
        .isInstanceOf(org.springframework.dao.DataAccessException.class);
  }

  @Test
  void writesOverrideAnUnsafeConnectionDefaultBeforeInsertingFacts() {
    sql.execute(
        "CREATE FUNCTION privacy_ledger.require_sync_commit() RETURNS trigger LANGUAGE plpgsql AS $$"
            + " BEGIN IF current_setting('synchronous_commit') <> 'on' THEN"
            + " RAISE EXCEPTION 'synchronous_commit required'; END IF; RETURN NEW; END $$");
    sql.execute(
        "CREATE TRIGGER require_sync_commit BEFORE INSERT ON privacy_ledger.forget_facts"
            + " FOR EACH ROW EXECUTE FUNCTION privacy_ledger.require_sync_commit()");
    try {
      var runtimeSource =
          new DriverManagerDataSource(PG.getJdbcUrl(), "privacy_runtime", "ledger-test-only");
      assertThat(
              new JdbcTemplate(runtimeSource)
                  .queryForObject("SHOW synchronous_commit", String.class))
          .isEqualTo("off");
      var runtime = new PostgresForgetLedger(runtimeSource);
      runtime.initialize(lineage, List.of(fact(1)));
      runtime.append(fact(2));
      assertThat(runtime.snapshot(lineage)).hasSize(2);
    } finally {
      sql.execute("DROP TRIGGER require_sync_commit ON privacy_ledger.forget_facts");
      sql.execute("DROP FUNCTION privacy_ledger.require_sync_commit()");
    }
  }

  @Test
  void snapshotRejectsAdminCorruptionAndDoesNotReturnPartiallyValidHistory() {
    var first = fact(1);
    ledger.initialize(lineage, List.of(first, fact(2)));
    sql.update(
        "DELETE FROM privacy_ledger.forget_facts WHERE event_id=?",
        UUID.fromString(first.eventId()));
    assertThatThrownBy(() -> ledger.snapshot(lineage)).hasMessage("forget_ledger_invalid_history");
    assertThatThrownBy(() -> ledger.append(fact(3))).hasMessage("forget_ledger_invalid_history");
  }

  @Test
  void limitsAreEnforcedOnBootstrapAppendAndDatabaseSnapshots() {
    var first = fact(1);
    assertThatThrownBy(
            () ->
                ledger.initialize(
                    lineage, Collections.nCopies(PostgresForgetLedger.MAX_FACTS + 1, first)))
        .hasMessage("forget_ledger_limit");
    ledger.initialize(lineage, List.of());
    sql.update(
        "INSERT INTO privacy_ledger.forget_facts"
            + " (event_id,lineage,tenant,subject,from_epoch,idempotency_key,request_hash)"
            + " SELECT md5(i::text)::uuid,?,'tenant',i::text,1,'first',?"
            + " FROM generate_series(1,?) i",
        UUID.fromString(lineage),
        ForgetFact.hash(1),
        PostgresForgetLedger.MAX_FACTS);
    assertThat(ledger.snapshot(lineage)).hasSize(PostgresForgetLedger.MAX_FACTS);
    assertThatThrownBy(() -> ledger.append(first)).hasMessage("forget_ledger_limit");
    sql.update(
        "INSERT INTO privacy_ledger.forget_facts"
            + " (event_id,lineage,tenant,subject,from_epoch,idempotency_key,request_hash)"
            + " VALUES (?,?,'tenant','overflow',1,'first',?)",
        UUID.randomUUID(),
        UUID.fromString(lineage),
        ForgetFact.hash(1));
    assertThatThrownBy(() -> ledger.snapshot(lineage)).hasMessage("forget_ledger_limit");
  }

  @Test
  void databaseFailureDoesNotExposeConnectionDetailsOrSqlCauses() {
    var unauthorized =
        new PostgresForgetLedger(
            new DriverManagerDataSource(
                PG.getJdbcUrl(), "privacy_runtime", "deliberately-wrong-secret"));
    assertThatThrownBy(() -> unauthorized.snapshot(lineage))
        .isInstanceOf(IllegalStateException.class)
        .hasMessage("forget_ledger_unavailable")
        .hasNoCause();
  }
}
