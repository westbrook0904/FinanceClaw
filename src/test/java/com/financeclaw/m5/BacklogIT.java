package com.financeclaw.m5;

import static org.assertj.core.api.Assertions.*;
import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.*;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.*;

import com.fasterxml.jackson.databind.ObjectMapper;
import com.financeclaw.app.operations.AdmissionService;
import com.financeclaw.app.operations.OperationsQueryService;
import com.financeclaw.application.*;
import com.financeclaw.bootstrap.FinanceClawApplication;
import com.financeclaw.domain.Operations.*;
import com.financeclaw.domain.Task.*;
import java.util.*;
import javax.sql.DataSource;
import org.junit.jupiter.api.*;
import org.junit.jupiter.params.ParameterizedTest;
import org.junit.jupiter.params.provider.ValueSource;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.boot.test.autoconfigure.web.servlet.AutoConfigureMockMvc;
import org.springframework.boot.test.context.SpringBootTest;
import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.test.context.*;
import org.springframework.test.web.servlet.MockMvc;
import org.testcontainers.containers.PostgreSQLContainer;

@SpringBootTest(
    classes = FinanceClawApplication.class,
    properties = {
      "financeclaw.worker.enabled=false",
      "financeclaw.integrations.enabled=false",
      "financeclaw.auth.token=m5c-test-only"
    })
@AutoConfigureMockMvc
class BacklogIT {
  static final PostgreSQLContainer<?> PG = new PostgreSQLContainer<>("postgres:16.10-alpine");

  static {
    PG.start();
  }

  @DynamicPropertySource
  static void database(DynamicPropertyRegistry r) {
    r.add("spring.datasource.url", PG::getJdbcUrl);
    r.add("spring.datasource.username", PG::getUsername);
    r.add("spring.datasource.password", PG::getPassword);
  }

  @Autowired OperationsQueryService operations;
  @Autowired AdmissionService admissions;
  @Autowired TaskService tasks;
  @Autowired DataSource source;
  @Autowired MockMvc http;
  @Autowired ObjectMapper json;
  JdbcTemplate sql;
  final Owner owner = new Owner("local", "developer");
  final List<Owner> others = List.of(new Owner("other", "developer"), new Owner("local", "other"));

  @BeforeEach
  void reset() {
    sql = new JdbcTemplate(source);
    sql.execute("TRUNCATE business.conversations,business.admission CASCADE");
    sql.execute("TRUNCATE memory.owners,memory.jobs,memory.operations,memory.audit CASCADE");
  }

  Accepted submit(Owner o) {
    return tasks.submit(
        o, tasks.createConversation(o), UUID.randomUUID().toString(), "secret-user-message");
  }

  void project() {
    while (tasks.projectNext()) {}
  }

  List<BacklogItem> all(Owner o, String kind, int size) {
    var result = new ArrayList<BacklogItem>();
    String after = "";
    do {
      var page = operations.backlog(o, kind, after, size);
      assertThat(page.scope()).isEqualTo("current_owner");
      assertThat(page.items()).hasSizeLessThanOrEqualTo(size);
      result.addAll(page.items());
      if (page.nextAfter() != null) assertThat(page.nextAfter()).isNotEqualTo(after);
      after = page.nextAfter();
      assertThat(result.size()).isLessThan(100);
    } while (after != null);
    assertThat(result).extracting(BacklogItem::id).doesNotHaveDuplicates();
    return result;
  }

  @Test
  void httpRequiresAuthenticationAndRejectsInvalidPagingAndKinds() throws Exception {
    http.perform(get("/v1/operations/drain/items")).andExpect(status().isUnauthorized());
    for (String query :
        List.of(
            "limit=0",
            "limit=101",
            "limit=-1",
            "limit=1.5",
            "kind=all",
            "after=contains space",
            "after=" + "x".repeat(129),
            "kind=memory_jobs&after=-1",
            "kind=memory_jobs&after=01",
            "kind=memory_jobs&after=9223372036854775808")) {
      http.perform(
              get("/v1/operations/drain/items?" + query)
                  .header("Authorization", "Bearer m5c-test-only"))
          .andExpect(status().isUnprocessableEntity());
    }
    http.perform(get("/v1/operations/drain/items").header("Authorization", "Bearer m5c-test-only"))
        .andExpect(status().isOk())
        .andExpect(jsonPath("$.items").isEmpty())
        .andExpect(jsonPath("$.scope").value("current_owner"))
        .andExpect(jsonPath("$.next_after").doesNotExist());
  }

  @Test
  void taskPagesStayOwnerScopedAndResolvingAnEarlierRowDoesNotSkipLaterRows() throws Exception {
    var mine = new ArrayList<Accepted>();
    for (int i = 0; i < 5; i++) mine.add(submit(owner));
    for (Owner other : others) submit(other);
    mine.sort(Comparator.comparing(Accepted::turnId));
    var first = operations.backlog(owner, "turns", "", 2);
    assertThat(first.items())
        .extracting(BacklogItem::id)
        .containsExactly(mine.get(0).turnId(), mine.get(1).turnId());
    tasks.cancel(owner, mine.getFirst().conversationId(), mine.getFirst().turnId(), "cancel");
    project();
    var second = operations.backlog(owner, "turns", first.nextAfter(), 2);
    assertThat(second.items())
        .extracting(BacklogItem::id)
        .containsExactly(mine.get(2).turnId(), mine.get(3).turnId());
    var last = operations.backlog(owner, "turns", second.nextAfter(), 2);
    assertThat(last.items()).extracting(BacklogItem::id).containsExactly(mine.get(4).turnId());
    assertThat(last.nextAfter()).isNull();
    assertThat(all(owner, "turns", 2)).hasSize(4);
    String body =
        http.perform(
                get("/v1/operations/drain/items?tenant=other&subject=other")
                    .header("Authorization", "Bearer m5c-test-only"))
            .andExpect(status().isOk())
            .andReturn()
            .getResponse()
            .getContentAsString();
    assertThat(body).doesNotContain("secret-user-message", "release_hash", "request_hash");
    assertThat(json.readTree(body).get("items")).hasSize(4);
  }

  @ParameterizedTest
  @ValueSource(strings = {"queued", "resuming", "running", "waiting", "cancelling", "blocked"})
  void everyNonterminalTurnIsLocatableEvenWithNoActiveCommand(String state) {
    var t = submit(owner);
    sql.update("UPDATE business.turns SET status=? WHERE id=?", state, t.turnId());
    sql.update("UPDATE business.commands SET status='done' WHERE turn_id=?", t.turnId());
    var items = all(owner, "turns", 1);
    assertThat(items).hasSize(1);
    assertThat(((TurnBacklog) items.getFirst()).status()).isEqualTo(state);
    assertThat(operations.drain(owner).counts().get(state.equals("resuming") ? "queued" : state))
        .isEqualTo(1);
  }

  @ParameterizedTest
  @ValueSource(strings = {"command", "interaction", "effect", "outbox"})
  void terminalTurnsWithRemainingBusinessFactsAreNotHidden(String kind) {
    var t = submit(owner);
    var claim = tasks.claim("fixture", 60000);
    tasks.cancel(owner, t.conversationId(), t.turnId(), "cancel");
    tasks.acknowledgeCancel(claim);
    project();
    assertThat(all(owner, "turns", 1)).isEmpty();
    switch (kind) {
      case "command" ->
          sql.update("UPDATE business.commands SET status='queued' WHERE turn_id=?", t.turnId());
      case "interaction" ->
          sql.update(
              """
          INSERT INTO business.interactions(id,turn_id,command_id,attempt_id,state_version,grant_revision,
            release_hash,kind,call_id,tool,action_hash,schema_json,question)
          VALUES('question',?,?,?,1,1,'release','input','call','tool','hash','{}','secret-question')
          """,
              t.turnId(),
              claim.command().id(),
              claim.command().attemptId());
      case "effect" ->
          sql.update(
              "INSERT INTO business.tool_calls VALUES(?,'call','hash','pending',NULL)", t.turnId());
      case "outbox" ->
          sql.update("UPDATE business.outbox SET delivered=false WHERE turn_id=?", t.turnId());
    }
    assertThat(all(owner, "turns", 1)).hasSize(1);
    String counter =
        switch (kind) {
          case "command" -> "active_commands";
          case "interaction" -> "pending_interactions";
          case "effect" -> "uncertain_effects";
          default -> "pending_outbox";
        };
    assertThat(operations.drain(owner).counts()).containsEntry(counter, 1L);
  }

  String binding(Owner o, Accepted t) {
    String id = UUID.randomUUID().toString();
    sql.update(
        """
        INSERT INTO channel.bindings(id,tenant_key,open_id,chat_id,tenant,subject,conversation_id,active_turn)
        VALUES(?,'secret-tenant',?,'secret-chat',?,?,?,?)
        """,
        id,
        id,
        o.tenant(),
        o.subject(),
        t.conversationId(),
        t.turnId());
    return id;
  }

  void notice(String id, String binding, Accepted t, long revision, String status) {
    sql.update(
        """
        INSERT INTO channel.notices(id,turn_id,binding_id,binding_revision,revision,payload,status)
        VALUES(?,?,?,1,?,'secret-notification-payload',?)
        """,
        id,
        t.turnId(),
        binding,
        revision,
        status);
  }

  @Test
  void notificationsUseTheOriginalTurnConversationAndExcludePayloadAndOtherOwners()
      throws Exception {
    var t = submit(owner);
    String b = binding(owner, t);
    long rev = 0;
    for (String status :
        List.of("pending", "sending", "unknown", "submitted_unverified", "suppressed"))
      notice("notice-" + rev, b, t, ++rev, status);
    // A binding can move to a new conversation while an older notice remains unresolved.
    sql.update(
        "UPDATE channel.bindings SET conversation_id=? WHERE id=?",
        tasks.createConversation(owner),
        b);
    for (Owner o : others) {
      var foreign = submit(o);
      notice(UUID.randomUUID().toString(), binding(o, foreign), foreign, 1, "unknown");
    }
    var list = all(owner, "notifications", 2);
    assertThat(list)
        .hasSize(4)
        .allMatch(row -> ((NotificationBacklog) row).conversationId().equals(t.conversationId()));
    assertThat(operations.drain(owner).counts())
        .containsEntry("pending_notifications", 2L)
        .containsEntry("unverified_notifications", 2L);
    var response = json.writeValueAsString(list);
    assertThat(response).doesNotContain("secret-", "payload", "chat_id", "open_id", "binding_id");
  }

  @Test
  void unprojectedBindingRowsTrackCurrentRevisionMuteAndSuppression() {
    var t = submit(owner);
    tasks.cancel(owner, t.conversationId(), t.turnId(), "cancel");
    project();
    String b = binding(owner, t);
    long rev = tasks.get(owner, t.conversationId(), t.turnId()).revision();
    assertThat(all(owner, "notification_bindings", 1))
        .extracting(BacklogItem::id)
        .containsExactly(b);
    notice("stale", b, t, rev - 1, "submitted_unverified");
    assertThat(all(owner, "notification_bindings", 1)).hasSize(1);
    notice("current", b, t, rev, "pending");
    assertThat(all(owner, "notification_bindings", 1)).isEmpty();
    sql.update("UPDATE channel.notices SET status='suppressed' WHERE id='current'");
    assertThat(all(owner, "notification_bindings", 1)).hasSize(1);
    sql.update("UPDATE channel.bindings SET muted=true WHERE id=?", b);
    assertThat(all(owner, "notification_bindings", 1)).isEmpty();
    assertThat(operations.drain(owner).counts()).containsEntry("unprojected_notifications", 0L);
  }

  @Test
  void aForeignTurnCannotBeExposedThroughAnOwnedBinding() {
    var mine = submit(owner);
    String b = binding(owner, mine);
    var foreign = submit(others.getFirst());
    tasks.cancel(others.getFirst(), foreign.conversationId(), foreign.turnId(), "cancel");
    sql.update("UPDATE channel.bindings SET active_turn=? WHERE id=?", foreign.turnId(), b);
    notice("foreign", b, foreign, 1, "unknown");
    assertThat(all(owner, "notification_bindings", 1)).isEmpty();
    assertThat(all(owner, "notifications", 1)).isEmpty();
    assertThat(operations.drain(owner).counts())
        .containsEntry("unverified_notifications", 0L)
        .containsEntry("unprojected_notifications", 0L);
  }

  @Test
  void memoryPaginationIsNumericAndRetainsRetryingJobsWithoutSourceOrLeaseData() throws Exception {
    for (long id : List.of(9L, 10L, 11L, 100L, 101L)) {
      sql.update(
          """
          INSERT INTO memory.jobs(id,kind,key_id,tenant,subject,epoch,status,lease_token,error_code)
          OVERRIDING SYSTEM VALUE VALUES(?,'extract',?,'local','developer',1,?,'secret-lease','extraction_failed')
          """,
          id,
          "secret-source-" + id,
          id == 11 ? "done" : id == 100 ? "running" : "pending");
    }
    for (Owner o : others)
      sql.update(
          "INSERT INTO memory.jobs(kind,key_id,tenant,subject,epoch) VALUES('index',?,?,?,1)",
          UUID.randomUUID().toString(),
          o.tenant(),
          o.subject());
    var list = all(owner, "memory_jobs", 2);
    assertThat(list).extracting(BacklogItem::id).containsExactly("9", "10", "100", "101");
    assertThat(operations.drain(owner).counts()).containsEntry("pending_memory_jobs", 4L);
    assertThat(json.writeValueAsString(list)).doesNotContain("secret-", "key_id", "lease_token");
    sql.update("UPDATE memory.jobs SET status='forgotten' WHERE id=100");
    assertThat(all(owner, "memory_jobs", 2))
        .extracting(BacklogItem::id)
        .containsExactly("9", "10", "101");
  }

  @Test
  void inspectionWritesNothingAndRetainsPrivacyBacklog() {
    submit(owner);
    admissions.change(owner, "pause", true, 0);
    sql.update(
        "INSERT INTO memory.owners(tenant,subject,purge_pending) VALUES('local','developer',true)");
    Map<String, String> before = fingerprints();
    for (String kind : List.of("turns", "notifications", "notification_bindings", "memory_jobs"))
      all(owner, kind, 1);
    assertThat(operations.drain(owner).counts()).containsEntry("pending_forget", 1L);
    assertThat(operations.drain(owner).drained()).isFalse();
    assertThat(fingerprints()).isEqualTo(before);
  }

  Map<String, String> fingerprints() {
    var result = new TreeMap<String, String>();
    for (String table :
        sql.queryForList(
            "SELECT schemaname||'.'||tablename FROM pg_tables WHERE schemaname IN ('business','memory','channel','agent_runtime')",
            String.class))
      result.put(
          table,
          sql.queryForObject(
              "SELECT md5(COALESCE(string_agg(to_jsonb(t)::text,'' ORDER BY to_jsonb(t)::text),'')) FROM "
                  + table
                  + " t",
              String.class));
    return result;
  }
}
