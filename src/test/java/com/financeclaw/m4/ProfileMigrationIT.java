package com.financeclaw.m4;

import static org.assertj.core.api.Assertions.*;

import com.financeclaw.app.memory.MemoryService;
import com.financeclaw.app.memory.port.MemoryRepository;
import com.financeclaw.domain.Governance.Blocked;
import com.financeclaw.domain.Task.Owner;
import com.financeclaw.domain.Task.Turn;
import com.financeclaw.domain.memory.MemoryPolicy;
import com.financeclaw.infrastructure.memory.MemoryMapper;
import com.financeclaw.infrastructure.memory.MyBatisMemoryRepository;
import java.util.List;
import java.util.Map;
import org.apache.ibatis.session.Configuration;
import org.apache.ibatis.session.LocalCacheScope;
import org.flywaydb.core.Flyway;
import org.junit.jupiter.api.AfterAll;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.Test;
import org.mybatis.spring.SqlSessionFactoryBean;
import org.mybatis.spring.SqlSessionTemplate;
import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.jdbc.datasource.DriverManagerDataSource;
import org.springframework.jdbc.support.JdbcTransactionManager;
import org.springframework.transaction.support.TransactionTemplate;
import org.testcontainers.containers.PostgreSQLContainer;

/** Upgrade an actual V8 database; a fresh V9 schema cannot expose missing legacy bindings. */
class ProfileMigrationIT {
  private static final PostgreSQLContainer<?> PG =
      new PostgreSQLContainer<>("postgres:16.10-alpine");
  private static JdbcTemplate sql;
  private static MemoryService memory;
  private static Turn queued;
  private static Turn completed;
  private static Turn forgotten;
  private static Turn pending;
  private static List<Map<String, Object>> originalSources;
  private static List<Map<String, Object>> originalJobs;

  @BeforeAll
  static void upgrade() throws Exception {
    PG.start();
    var source = new DriverManagerDataSource(PG.getJdbcUrl(), PG.getUsername(), PG.getPassword());
    Flyway.configure().dataSource(source).target("8").load().migrate();
    sql = new JdbcTemplate(source);

    var legacy = new Owner("legacy", "same-subject");
    queued = turn("legacy-queued", legacy, "queued");
    completed = turn("legacy-completed", legacy, "completed");
    var existing = new Owner("existing", "same-subject");
    owner(existing, 1, false);
    turn("existing-no-source", existing, "waiting");
    turn("existing-source", existing, "completed");
    sql.update(
        "INSERT INTO memory.sources(id,tenant,subject,epoch,content,status) VALUES('existing-source',?,?,1,'original evidence text','pending')",
        existing.tenant(),
        existing.subject());
    sql.update(
        "INSERT INTO memory.jobs(kind,key_id,tenant,subject,epoch) VALUES('extract','existing-source',?,?,1)",
        existing.tenant(),
        existing.subject());

    var erased = new Owner("forgotten", "same-subject");
    owner(erased, 2, false);
    forgotten = turn("forgotten-no-source", erased, "waiting");
    turn("forgotten-source", erased, "completed");
    sql.update(
        "INSERT INTO memory.sources(id,tenant,subject,epoch,content,status) VALUES('forgotten-source',?,?,1,NULL,'forgotten')",
        erased.tenant(),
        erased.subject());
    var purging = new Owner("pending", "same-subject");
    owner(purging, 1, true);
    pending = turn("pending-no-source", purging, "queued");
    var erasedPurging = new Owner("forgotten-pending", "same-subject");
    owner(erasedPurging, 3, true);
    turn("forgotten-pending-no-source", erasedPurging, "completed");

    originalSources =
        sql.queryForList(
            "SELECT id,tenant,subject,epoch,content,status,created_at FROM memory.sources ORDER BY id");
    originalJobs = sql.queryForList("SELECT * FROM memory.jobs ORDER BY id");
    assertThat(
            Flyway.configure().dataSource(source).target("9").load().migrate().migrationsExecuted)
        .isEqualTo(1);

    var config = new Configuration();
    config.setMapUnderscoreToCamelCase(true);
    config.setCacheEnabled(false);
    config.setLocalCacheScope(LocalCacheScope.STATEMENT);
    config.addMapper(MemoryMapper.class);
    var factory = new SqlSessionFactoryBean();
    factory.setDataSource(source);
    factory.setConfiguration(config);
    MemoryRepository db =
        new MyBatisMemoryRepository(
            new SqlSessionTemplate(factory.getObject()).getMapper(MemoryMapper.class));
    memory =
        new MemoryService(
            db,
            new TransactionTemplate(new JdbcTransactionManager(source)),
            new MemoryPolicy(false, false, "disabled", true));
  }

  @AfterAll
  static void stop() {
    PG.stop();
  }

  @Test
  void onlyNeverForgottenOwnersReceiveEmptyVersionZeroBindings() {
    var rows =
        sql.queryForList(
            "SELECT id,tenant,subject,epoch,content,status,profile_revision FROM memory.sources WHERE status='profile_only' ORDER BY id");
    assertThat(rows)
        .extracting(row -> row.get("id"))
        .containsExactly("existing-no-source", "legacy-completed", "legacy-queued");
    assertThat(rows)
        .allSatisfy(
            row ->
                assertThat(row)
                    .containsEntry("epoch", 1L)
                    .containsEntry("profile_revision", 0L)
                    .containsEntry("content", null)
                    .containsEntry("subject", "same-subject"));
    assertThat(
            sql.queryForMap(
                "SELECT epoch,purge_pending,profile_revision FROM memory.owners WHERE tenant='legacy' AND subject='same-subject'"))
        .containsEntry("epoch", 1L)
        .containsEntry("purge_pending", false)
        .containsEntry("profile_revision", 0L);
    assertThat(sql.queryForObject("SELECT count(*) FROM memory.user_profiles", Integer.class))
        .isZero();
    assertThat(sql.queryForObject("SELECT count(*) FROM memory.recalls", Integer.class)).isZero();
    assertThat(sql.queryForObject("SELECT count(*) FROM memory.facts", Integer.class)).isZero();
  }

  @Test
  void existingSourcesAndJobsRemainUnchangedIncludingForgottenEvidence() {
    assertThat(
            sql.queryForList(
                "SELECT id,tenant,subject,epoch,content,status,created_at FROM memory.sources WHERE id IN ('existing-source','forgotten-source') ORDER BY id"))
        .isEqualTo(originalSources);
    assertThat(sql.queryForList("SELECT * FROM memory.jobs ORDER BY id")).isEqualTo(originalJobs);
    assertThat(
            sql.queryForMap(
                "SELECT epoch,purge_pending FROM memory.owners WHERE tenant='forgotten' AND subject='same-subject'"))
        .containsEntry("epoch", 2L)
        .containsEntry("purge_pending", false);
    assertThat(
            sql.queryForMap(
                "SELECT epoch,purge_pending FROM memory.owners WHERE tenant='pending' AND subject='same-subject'"))
        .containsEntry("epoch", 1L)
        .containsEntry("purge_pending", true);
    assertThat(
            sql.queryForMap(
                "SELECT epoch,purge_pending FROM memory.owners WHERE tenant='forgotten-pending' AND subject='same-subject'"))
        .containsEntry("epoch", 3L)
        .containsEntry("purge_pending", true);
  }

  @Test
  void defaultProfileModeRetainsLegacyTaskAndArtifactChecksButCannotResurrectForgottenTurns() {
    assertThatCode(() -> memory.check(queued)).doesNotThrowAnyException();
    assertThat(memory.sameEpoch(completed)).isTrue();
    assertThat(memory.context(queued)).isEmpty();
    assertThat(memory.filterHistory(queued, List.of(completed))).containsExactly(completed);
    for (var turn : List.of(forgotten, pending)) {
      assertThatThrownBy(() -> memory.check(turn))
          .isInstanceOf(Blocked.class)
          .hasMessage("memory_privacy_changed");
      assertThat(memory.sameEpoch(turn)).isFalse();
    }
  }

  private static void owner(Owner owner, long epoch, boolean purgePending) {
    sql.update(
        "INSERT INTO memory.owners(tenant,subject,epoch,purge_pending) VALUES(?,?,?,?)",
        owner.tenant(),
        owner.subject(),
        epoch,
        purgePending);
  }

  private static Turn turn(String id, Owner owner, String status) {
    String conversation = "conversation-" + id;
    sql.update(
        "INSERT INTO business.conversations(id,tenant,subject) VALUES(?,?,?)",
        conversation,
        owner.tenant(),
        owner.subject());
    sql.update(
        "INSERT INTO business.turns(id,conversation_id,tenant,subject,idempotency_key,request_hash,message,release_hash,status) VALUES(?,?,?,?,?,'hash','legacy private message','release',?)",
        id,
        conversation,
        owner.tenant(),
        owner.subject(),
        "key-" + id,
        status);
    return new Turn(
        id,
        conversation,
        owner.tenant(),
        owner.subject(),
        "key-" + id,
        "hash",
        "legacy private message",
        "release",
        status,
        1,
        null,
        null);
  }
}
