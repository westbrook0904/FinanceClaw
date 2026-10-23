package com.financeclaw.m4;

import static org.assertj.core.api.Assertions.*;

import com.financeclaw.app.memory.*;
import com.financeclaw.app.memory.port.MemoryRepository;
import com.financeclaw.app.task.TaskEventHub;
import com.financeclaw.domain.Governance.Blocked;
import com.financeclaw.domain.Memory.*;
import com.financeclaw.domain.Task.*;
import com.financeclaw.domain.memory.*;
import com.financeclaw.infrastructure.memory.*;
import java.util.List;
import java.util.concurrent.*;
import org.apache.ibatis.session.Configuration;
import org.apache.ibatis.session.LocalCacheScope;
import org.flywaydb.core.Flyway;
import org.junit.jupiter.api.*;
import org.junit.jupiter.params.ParameterizedTest;
import org.junit.jupiter.params.provider.ValueSource;
import org.mybatis.spring.SqlSessionFactoryBean;
import org.mybatis.spring.SqlSessionTemplate;
import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.jdbc.datasource.DriverManagerDataSource;
import org.springframework.jdbc.support.JdbcTransactionManager;
import org.springframework.transaction.support.TransactionTemplate;
import org.testcontainers.containers.PostgreSQLContainer;

/** Actual PostgreSQL owner locks, constructor mappings, rollback and document/source isolation. */
class UserProfileIT {
  private static final PostgreSQLContainer<?> PG =
      new PostgreSQLContainer<>("postgres:16.10-alpine");
  private static final Owner ALICE = new Owner("tenant", "alice");
  private static final Owner BOB = new Owner("tenant", "bob");
  private static JdbcTemplate sql;
  private static TransactionTemplate tx;
  private static MemoryRepository db;
  private TaskEventHub events;
  private UserProfileService profiles;
  private MemoryService memory;

  @BeforeAll
  static void database() throws Exception {
    PG.start();
    var source = new DriverManagerDataSource(PG.getJdbcUrl(), PG.getUsername(), PG.getPassword());
    Flyway.configure().dataSource(source).load().migrate();
    sql = new JdbcTemplate(source);
    tx = new TransactionTemplate(new JdbcTransactionManager(source));
    var config = new Configuration();
    config.setMapUnderscoreToCamelCase(true);
    config.setCacheEnabled(false);
    config.setLocalCacheScope(LocalCacheScope.STATEMENT);
    config.addMapper(MemoryMapper.class);
    var factory = new SqlSessionFactoryBean();
    factory.setDataSource(source);
    factory.setConfiguration(config);
    db =
        new MyBatisMemoryRepository(
            new SqlSessionTemplate(factory.getObject()).getMapper(MemoryMapper.class));
  }

  @AfterAll
  static void stopDatabase() {
    PG.stop();
  }

  @BeforeEach
  void reset() {
    sql.execute("TRUNCATE business.conversations CASCADE");
    sql.execute("TRUNCATE memory.owners,memory.jobs,memory.operations,memory.audit CASCADE");
    events = new TaskEventHub();
    var settings = new MemoryPolicy(false, false, "disabled", true);
    profiles = new UserProfileService(db, tx, settings, events);
    memory = new MemoryService(db, tx, settings);
  }

  @Test
  void exactMarkdownRoundTripsWithoutOwnerLeakageOrCrossOwnerReads() {
    assertThat(profiles.read(ALICE)).isEqualTo(new UserProfile(1, 0, ""));
    String markdown = "# 用户画像\r\n\r\n- 偏好：简洁 😀\n";
    assertThat(profiles.update(ALICE, 1, 0, markdown)).isEqualTo(new UserProfile(1, 1, markdown));
    assertThat(profiles.read(ALICE)).isEqualTo(new UserProfile(1, 1, markdown));
    assertThat(profiles.read(BOB)).isEqualTo(new UserProfile(1, 0, ""));
    assertThat(tx.execute(s -> db.lock(ALICE)).profileRevision()).isEqualTo(1);
    assertThat(sql.queryForObject("SELECT count(*) FROM memory.user_profiles", Integer.class))
        .isEqualTo(1);
  }

  @Test
  void unchangedAndClearedDocumentsHavePredictableRevisions() {
    assertThat(profiles.update(ALICE, 1, 0, "").revision()).isZero();
    assertThat(sql.queryForObject("SELECT count(*) FROM memory.user_profiles", Integer.class))
        .isZero();
    profiles.update(ALICE, 1, 0, "text");
    assertThat(profiles.update(ALICE, 1, 1, "text").revision()).isEqualTo(1);
    assertThat(profiles.update(ALICE, 1, 1, "")).isEqualTo(new UserProfile(1, 2, ""));
    assertThat(sql.queryForObject("SELECT count(*) FROM memory.user_profiles", Integer.class))
        .isEqualTo(1);
    assertThat(
            sql.queryForObject(
                "SELECT count(*) FROM memory.audit WHERE action='profile_update'", Integer.class))
        .isEqualTo(2);
  }

  @Test
  void onlyOneConcurrentEditorCanCommitTheSameExpectedRevision() throws Exception {
    profiles.read(ALICE);
    var ready = new CountDownLatch(2);
    var start = new CountDownLatch(1);
    try (var pool = Executors.newFixedThreadPool(2)) {
      var first = pool.submit(() -> updateTogether(ready, start, "first"));
      var second = pool.submit(() -> updateTogether(ready, start, "second"));
      assertThat(ready.await(5, TimeUnit.SECONDS)).isTrue();
      start.countDown();
      assertThat(List.of(first.get(10, TimeUnit.SECONDS), second.get(10, TimeUnit.SECONDS)))
          .containsExactlyInAnyOrder("updated", "profile_revision_conflict");
    }
    assertThat(profiles.read(ALICE).revision()).isEqualTo(1);
    assertThat(
            sql.queryForObject(
                "SELECT count(*) FROM memory.audit WHERE action='profile_update'", Integer.class))
        .isEqualTo(1);
  }

  private String updateTogether(CountDownLatch ready, CountDownLatch start, String markdown)
      throws InterruptedException {
    ready.countDown();
    if (!start.await(5, TimeUnit.SECONDS)) throw new IllegalStateException("start timed out");
    try {
      profiles.update(ALICE, 1, 0, markdown);
      return "updated";
    } catch (Problem conflict) {
      return conflict.getMessage();
    }
  }

  @ParameterizedTest
  @ValueSource(booleans = {false, true})
  void profileEditsAndForgettingWaitForAnAdmittedTurnsSharedOwnerLock(boolean forgetting)
      throws Exception {
    // The unique-key insert on first use is outside this steady-state shared-lock scenario.
    profiles.read(ALICE);
    var admitted = new CountDownLatch(1);
    var release = new CountDownLatch(1);
    var mutationStarted = new CountDownLatch(1);
    try (var pool = Executors.newVirtualThreadPerTaskExecutor()) {
      var reader =
          pool.submit(
              () ->
                  tx.execute(
                      status -> {
                        var turn = admit("held-admission", ALICE);
                        admitted.countDown();
                        try {
                          if (!release.await(10, TimeUnit.SECONDS))
                            throw new AssertionError("admission release timeout");
                        } catch (InterruptedException interrupted) {
                          Thread.currentThread().interrupt();
                          throw new AssertionError(interrupted);
                        }
                        return turn;
                      }));
      Future<Object> mutation;
      try {
        assertThat(admitted.await(10, TimeUnit.SECONDS)).isTrue();
        mutation =
            pool.submit(
                () -> {
                  mutationStarted.countDown();
                  if (forgetting) return memory.forget(ALICE, 1, "forget-after-admission");
                  return profiles.update(ALICE, 1, 0, "updated after admission");
                });
        assertThat(mutationStarted.await(10, TimeUnit.SECONDS)).isTrue();
        long deadline = System.nanoTime() + TimeUnit.SECONDS.toNanos(5);
        boolean waiting;
        do {
          waiting =
              sql.queryForObject(
                      "SELECT count(*) FROM pg_stat_activity WHERE pid<>pg_backend_pid()"
                          + " AND wait_event_type='Lock' AND query LIKE '%memory.owners%'"
                          + " AND query LIKE '%FOR UPDATE%'",
                      Integer.class)
                  > 0;
          if (!waiting) Thread.sleep(10);
        } while (!waiting && System.nanoTime() < deadline);
        assertThat(waiting).as("profile mutation waits for the shared owner lock").isTrue();
        assertThat(mutation.isDone()).isFalse();
        assertThat(
                sql.queryForMap(
                    "SELECT epoch,profile_revision,purge_pending FROM memory.owners"
                        + " WHERE tenant=? AND subject=?",
                    ALICE.tenant(),
                    ALICE.subject()))
            .containsEntry("epoch", 1L)
            .containsEntry("profile_revision", 0L)
            .containsEntry("purge_pending", false);
      } finally {
        release.countDown();
      }
      var turn = reader.get(10, TimeUnit.SECONDS);
      Object changed = mutation.get(10, TimeUnit.SECONDS);
      assertThat(db.sourceById(turn.id()))
          .isEqualTo(
              new Source(
                  turn.id(),
                  "tenant",
                  "alice",
                  1,
                  null,
                  forgetting ? "forgotten" : "profile_only",
                  0));
      if (forgetting) {
        assertThat(changed).isEqualTo(2L);
        assertThatThrownBy(() -> memory.check(turn)).hasMessage("memory_privacy_changed");
      } else {
        assertThat(changed).isEqualTo(new UserProfile(1, 1, "updated after admission"));
        assertThatThrownBy(() -> memory.check(turn)).hasMessage("profile_changed");
      }
    }
  }

  @Test
  void admissionBindsTheProfileVersionWithoutStoringTheBodyOrEvidenceSource() {
    profiles.update(ALICE, 1, 0, "private profile marker");
    var turn = admit("turn", ALICE);
    var source = db.sourceById(turn.id());
    assertThat(source).isEqualTo(new Source("turn", "tenant", "alice", 1, null, "profile_only", 1));
    assertThat(memory.context(turn)).contains("private profile marker");
    tx.executeWithoutResult(s -> memory.completed(turn.id()));
    assertThat(sql.queryForObject("SELECT count(*) FROM memory.jobs", Integer.class)).isZero();
    assertThat(sql.queryForObject("SELECT count(*) FROM memory.recalls", Integer.class)).isZero();
    assertThat(
            sql.queryForObject("SELECT content FROM memory.sources WHERE id='turn'", String.class))
        .isNull();
  }

  @Test
  void profileChangesBlockOldExecutionAndFilterOldHistoryWithoutChangingPrivacyEpoch() {
    var old = admit("old", ALICE);
    profiles.update(ALICE, 1, 0, "new profile");
    var current = admit("current", ALICE);
    var foreign = admit("foreign", BOB);

    assertThatThrownBy(() -> memory.check(old))
        .isInstanceOf(Blocked.class)
        .hasMessage("profile_changed");
    assertThatThrownBy(() -> memory.context(old))
        .isInstanceOf(Blocked.class)
        .hasMessage("profile_changed");
    assertThat(memory.sameEpoch(old)).isTrue();
    assertThat(memory.filterHistory(current, List.of(old, foreign, current)))
        .containsExactly(current);
    assertThat(memory.context(current)).contains("new profile");
  }

  @Test
  void disabledProfilesRejectOldProfileExecutionsButBindNewTurnsWithoutProfileContent() {
    profiles.update(ALICE, 1, 0, "profile marker");
    var old = admit("old", ALICE);
    memory = new MemoryService(db, tx, new MemoryPolicy(false, false, "disabled", false));

    assertThatThrownBy(() -> memory.check(old))
        .isInstanceOf(Blocked.class)
        .hasMessage("profile_changed");
    var current = admit("current", ALICE);
    assertThat(db.sourceById(current.id()))
        .isEqualTo(new Source("current", "tenant", "alice", 1, null, "profile_only", 0));
    assertThatCode(() -> memory.check(current)).doesNotThrowAnyException();
    assertThat(memory.context(current)).isEmpty();
    assertThat(memory.filterHistory(current, List.of(old, current))).containsExactly(current);
    tx.executeWithoutResult(s -> memory.completed(current.id()));
    assertThat(sql.queryForObject("SELECT count(*) FROM memory.jobs", Integer.class)).isZero();
  }

  @Test
  void disablingBothLayersCannotRestoreAccessAfterPrivacyEpochChanges() {
    var old = admit("old", ALICE);
    tx.executeWithoutResult(
        s -> {
          db.lock(ALICE);
          db.forget(ALICE);
          db.eraseSources(ALICE);
          db.purged(ALICE);
        });
    memory = new MemoryService(db, tx, new MemoryPolicy(false, false, "disabled", false));

    assertThatThrownBy(() -> memory.check(old))
        .isInstanceOf(Blocked.class)
        .hasMessage("memory_privacy_changed");
    assertThat(memory.sameEpoch(old)).isFalse();
    var current = admit("current", ALICE);
    assertThatCode(() -> memory.check(current)).doesNotThrowAnyException();
    assertThat(memory.filterHistory(current, List.of(old, current))).containsExactly(current);
  }

  @Test
  void rollbackRestoresTheDocumentVersionAndKeepsTheExistingAttemptLive() {
    profiles.update(ALICE, 1, 0, "before");
    var turn = admit("turn", ALICE);
    var claim =
        new Claim(new Command("command", "turn", "running", "attempt", 1, true, null), turn);
    try (var handle = events.begin(claim)) {
      assertThatThrownBy(
              () ->
                  tx.executeWithoutResult(
                      s -> {
                        profiles.update(ALICE, 1, 1, "rolled back");
                        throw new IllegalStateException("rollback");
                      }))
          .hasMessage("rollback");
      assertThat(profiles.read(ALICE)).isEqualTo(new UserProfile(1, 1, "before"));
      assertThatCode(() -> events.progress(claim, "text_delta", "still live", null))
          .doesNotThrowAnyException();

      profiles.update(ALICE, 1, 1, "committed");
      assertThatThrownBy(() -> events.progress(claim, "text_delta", "obsolete", null))
          .isInstanceOf(Fenced.class);
    }
  }

  @Test
  void confirmedEvidenceKeepsItsSourceAndCannotModifyTheManualProfile() {
    profiles.update(ALICE, 1, 0, "Prefer concise answers.");
    memory = new MemoryService(db, tx, new MemoryPolicy(true, false, "model", true));
    var previous = admit("previous", ALICE);
    var fact =
        new Fact(
            "fact",
            previous.id(),
            "tenant",
            "alice",
            1,
            "response_style",
            "详细",
            "我喜欢详细",
            1,
            "active");
    tx.executeWithoutResult(s -> db.insertFact(fact));
    var current = admit("current", ALICE);

    assertThat(memory.context(current))
        .contains(
            "Manually maintained user profile",
            "Confirmed cross-session preference evidence",
            "Prefer concise answers.",
            "\"sourceId\":\"previous\"",
            "\"evidence\":\"我喜欢详细\"");
    assertThat(profiles.read(ALICE)).isEqualTo(new UserProfile(1, 1, "Prefer concise answers."));
  }

  @Test
  void pendingForgetPreventsProfileReadsAndWritesAndRepositoryEraseResetsTheVersion() {
    profiles.update(ALICE, 1, 0, "private");
    tx.executeWithoutResult(
        s -> {
          db.lock(ALICE);
          db.forget(ALICE);
          db.eraseProfile(ALICE);
        });
    var privacy = tx.execute(s -> db.lock(ALICE));
    assertThat(privacy).isEqualTo(new Privacy("tenant", "alice", 2, true, 0));
    assertThat(db.userProfile(ALICE)).isNull();
    assertThatThrownBy(() -> profiles.read(ALICE))
        .isInstanceOf(Problem.class)
        .hasMessage("purge_pending");
    assertThatThrownBy(() -> profiles.update(ALICE, 2, 0, "new"))
        .isInstanceOf(Problem.class)
        .hasMessage("purge_pending");
  }

  private Turn admit(String id, Owner owner) {
    String conversation = "conversation-" + id;
    sql.update(
        "INSERT INTO business.conversations(id,tenant,subject) VALUES(?,?,?)",
        conversation,
        owner.tenant(),
        owner.subject());
    sql.update(
        "INSERT INTO"
            + " business.turns(id,conversation_id,tenant,subject,idempotency_key,request_hash,message,release_hash,status)"
            + " VALUES(?,?,?,?,?,'hash','private request','release','running')",
        id,
        conversation,
        owner.tenant(),
        owner.subject(),
        "key-" + id);
    var turn =
        new Turn(
            id,
            conversation,
            owner.tenant(),
            owner.subject(),
            "key-" + id,
            "hash",
            "private request",
            "release",
            "running",
            1,
            null,
            null);
    tx.executeWithoutResult(s -> memory.admit(turn));
    return turn;
  }
}
