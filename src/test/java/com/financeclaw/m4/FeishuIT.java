package com.financeclaw.m4;

import static org.assertj.core.api.Assertions.*;

import com.fasterxml.jackson.databind.*;
import com.financeclaw.app.operations.AdmissionService;
import com.financeclaw.bootstrap.FinanceClawApplication;
import com.financeclaw.channel.*;
import com.financeclaw.domain.Feishu.*;
import com.financeclaw.domain.Task.Owner;
import com.financeclaw.fixture.GovernanceFixture;
import com.financeclaw.fixture.TaskFixture;
import com.financeclaw.infrastructure.agentscope.execution.*;
import com.financeclaw.infrastructure.channel.FeishuMapper;
import com.financeclaw.infrastructure.state.RuntimeMapper;
import com.financeclaw.m2.ScriptedRuntime;
import com.financeclaw.support.PayloadJson;
import java.nio.charset.StandardCharsets;
import java.security.*;
import java.time.Instant;
import java.util.*;
import java.util.concurrent.*;
import javax.crypto.*;
import javax.crypto.spec.*;
import javax.sql.DataSource;
import org.junit.jupiter.api.*;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.boot.test.context.SpringBootTest;
import org.springframework.boot.test.web.client.TestRestTemplate;
import org.springframework.context.annotation.Import;
import org.springframework.http.*;
import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.test.context.*;

@Import({TaskFixture.class, GovernanceFixture.class})
@SpringBootTest(
    classes = FinanceClawApplication.class,
    webEnvironment = SpringBootTest.WebEnvironment.RANDOM_PORT,
    properties = {
      "financeclaw.worker.enabled=false",
      "financeclaw.integrations.enabled=false",
      "financeclaw.auth.token=m4-api-token",
      "financeclaw.feishu.enabled=true",
      "financeclaw.feishu.app-id=cli_test",
      "financeclaw.feishu.app-secret=test-secret",
      "financeclaw.feishu.verification-token=test-token",
      "financeclaw.feishu.encrypt-key=test-encrypt-key",
      "financeclaw.feishu.tenant-key=tenant_test"
    })
class FeishuIT {
  static final MockFeishu REMOTE = new MockFeishu();

  @DynamicPropertySource
  static void database(DynamicPropertyRegistry r) {
    r.add("spring.datasource.url", M4Database.PG::getJdbcUrl);
    r.add("spring.datasource.username", M4Database.PG::getUsername);
    r.add("spring.datasource.password", M4Database.PG::getPassword);
    r.add("financeclaw.feishu.base-url", REMOTE::url);
  }

  @Autowired FeishuService service;
  @Autowired FeishuMapper db;
  @Autowired FeishuDelivery delivery;
  @Autowired TaskFixture tasks;
  @Autowired org.springframework.context.ApplicationContext taskContext;
  @Autowired AdmissionService admissions;
  @Autowired GovernanceFixture governance;
  @Autowired RuntimeMapper states;
  @Autowired TaskAgentRuntime runtime;
  @Autowired TestRestTemplate http;
  @Autowired DataSource source;
  final Owner owner = new Owner("local", "developer");
  final ObjectMapper json = new ObjectMapper();
  JdbcTemplate sql;
  String conversation;

  @BeforeEach
  void reset() {
    sql = new JdbcTemplate(source);
    sql.execute("TRUNCATE business.conversations CASCADE");
    sql.execute("TRUNCATE business.admission CASCADE");
    sql.execute("TRUNCATE memory.jobs,memory.owners,memory.operations,memory.audit CASCADE");
    REMOTE.reset();
    conversation = tasks.createConversation(owner);
  }

  @AfterAll
  static void close() {
    REMOTE.close();
  }

  JsonNode message(String event, String id, String text) {
    return json.valueToTree(
        Map.of(
            "schema",
            "2.0",
            "header",
            Map.of(
                "event_id",
                event,
                "event_type",
                "im.message.receive_v1",
                "app_id",
                "cli_test",
                "tenant_key",
                "tenant_test",
                "token",
                "test-token"),
            "event",
            Map.of(
                "sender",
                Map.of("sender_id", Map.of("open_id", "ou_alice"), "sender_type", "user"),
                "message",
                Map.of(
                    "chat_id",
                    "oc_alice",
                    "chat_type",
                    "p2p",
                    "message_id",
                    id,
                    "message_type",
                    "text",
                    "content",
                    PayloadJson.canonical(Map.of("text", text))))));
  }

  Binding binding() {
    return db.getBindingByChannelIdentityForUpdate("tenant_test", "ou_alice", "oc_alice");
  }

  void bind() {
    service.receive(
        message(
            UUID.randomUUID().toString(), "om_bind", "/bind " + service.link(owner, conversation)));
  }

  void send(String text) {
    service.receive(
        message(
            UUID.randomUUID().toString(),
            "om_" + UUID.randomUUID().toString().replace("-", ""),
            text));
  }

  void run(boolean scripted) throws Exception {
    try (var worker =
        com.financeclaw.m1.TaskWorkerFixture.create(
            taskContext,
            scripted
                ? new ScriptedRuntime(
                    tasks,
                    governance,
                    states,
                    taskContext.getBean(
                        com.financeclaw.infrastructure.agentscope.capability.BusinessCapabilities
                            .class))
                : runtime,
            false,
            1,
            60000,
            2000)) {
      worker.execute(tasks.claim("feishu-test", 60000));
    }
  }

  ResponseEntity<String> callback(String body, String timestamp, boolean valid) {
    var headers = new HttpHeaders();
    headers.setContentType(MediaType.APPLICATION_JSON);
    headers.set("X-Lark-Request-Timestamp", timestamp);
    headers.set("X-Lark-Request-Nonce", "nonce");
    try {
      headers.set(
          "X-Lark-Signature",
          valid
              ? HexFormat.of()
                  .formatHex(
                      MessageDigest.getInstance("SHA-256")
                          .digest(
                              (timestamp + "nonce" + "test-encrypt-key" + body)
                                  .getBytes(StandardCharsets.UTF_8)))
              : "bad");
    } catch (Exception e) {
      throw new IllegalStateException(e);
    }
    return http.postForEntity(
        "/api/channels/feishu/financeclaw/callback", new HttpEntity<>(body, headers), String.class);
  }

  @Test
  void encryptedSignedChallengeUsesNativeCryptoAndRejectsReplay() throws Exception {
    String plaintext =
        PayloadJson.canonical(
            Map.of("type", "url_verification", "token", "test-token", "challenge", "challenge-ok"));
    byte[] iv = new byte[16];
    new SecureRandom().nextBytes(iv);
    var cipher = Cipher.getInstance("AES/CBC/PKCS5Padding");
    cipher.init(
        Cipher.ENCRYPT_MODE,
        new SecretKeySpec(
            MessageDigest.getInstance("SHA-256")
                .digest("test-encrypt-key".getBytes(StandardCharsets.UTF_8)),
            "AES"),
        new IvParameterSpec(iv));
    byte[] encrypted = cipher.doFinal(plaintext.getBytes(StandardCharsets.UTF_8));
    byte[] bytes = new byte[16 + encrypted.length];
    System.arraycopy(iv, 0, bytes, 0, 16);
    System.arraycopy(encrypted, 0, bytes, 16, encrypted.length);
    String body =
        PayloadJson.canonical(Map.of("encrypt", Base64.getEncoder().encodeToString(bytes)));
    String now = Long.toString(Instant.now().getEpochSecond());
    assertThat(callback(body, now, true).getBody()).contains("challenge-ok");
    assertThat(callback(body, now, false).getStatusCode().value()).isEqualTo(401);
    assertThat(callback(body, "1", true).getStatusCode().value()).isEqualTo(401);
  }

  @Test
  void identityNeedsOneTimeOwnerBindingAndDuplicateEventsCreateOneTurn() {
    var event = message("event-1", "om_1", "hello");
    assertThatThrownBy(() -> service.receive(event)).hasMessage("identity_not_bound");
    bind();
    service.receive(event);
    service.receive(event);
    assertThat(sql.queryForObject("SELECT count(*) FROM business.turns", Integer.class))
        .isEqualTo(1);
    assertThatThrownBy(() -> service.receive(message("event-1", "om_1", "changed")))
        .hasMessage("event_conflict");
    assertThatThrownBy(() -> service.receive(message("bind-again", "om_2", "/bind invalid")))
        .hasMessage("invalid_binding_code");
  }

  @Test
  void pauseAllowsNativeRepliesAndReplaysButDoesNotAcknowledgeUnadmittedNewMessages()
      throws Exception {
    bind();
    var original = message("first", "om_first", "city");
    service.receive(original);
    run(true);
    admissions.change(owner, "pause", true, 0);
    service.receive(original); // Original event receipt remains available while paused.
    send("上海"); // Native input interaction resumes the original Turn.
    run(true);
    assertThat(tasks.get(owner, conversation, binding().activeTurn()).status())
        .isEqualTo("completed");
    var next = message("new", "om_new", "hello");
    assertThatThrownBy(() -> service.receive(next)).hasMessage("admission_paused");
    assertThat(db.getInboxEventById("new")).isNull();
    assertThat(sql.queryForObject("SELECT count(*) FROM business.turns", Integer.class))
        .isEqualTo(1);
    admissions.change(owner, "resume", false, 1);
    service.receive(next);
    service.receive(next);
    assertThat(sql.queryForObject("SELECT count(*) FROM business.turns", Integer.class))
        .isEqualTo(2);
    assertThat(db.getInboxEventById("new")).isNotNull();
  }

  @Test
  void callbackTenantAndGroupCannotBypassBinding() {
    bind();
    var event = message("group", "om_g", "hello");
    ((com.fasterxml.jackson.databind.node.ObjectNode) event.path("event").path("message"))
        .put("chat_type", "group");
    assertThatThrownBy(() -> service.receive(event)).hasMessage("direct_user_messages_only");
    ((com.fasterxml.jackson.databind.node.ObjectNode) event.path("header"))
        .put("tenant_key", "other");
    assertThat(
            callback(event.toString(), Long.toString(Instant.now().getEpochSecond()), true)
                .getStatusCode()
                .value())
        .isEqualTo(401);
  }

  @Test
  void nativeSdkTokenAndTextSenderHandleFinalNotice() throws Exception {
    bind();
    send("hello");
    run(false);
    delivery.poll();
    assertThat(REMOTE.sends).hasValue(1);
    var body = REMOTE.writes.getFirst();
    assertThat(body.path("receive_id").asText()).isEqualTo("oc_alice");
    assertThat(body.path("msg_type").asText()).isEqualTo("text");
    assertThat(body.path("content").asText()).contains("hello");
    assertThat(body.has("card")).isFalse();
    assertThat(delivery.list(owner).getFirst().status()).isEqualTo("submitted_unverified");
    delivery.poll();
    assertThat(REMOTE.sends).hasValue(1);
  }

  @Test
  void nativeSdkNonzeroCodeIsNotMisreportedAsDeliveredOrRetried() throws Exception {
    bind();
    send("hello");
    run(false);
    REMOTE.code = 99991400;
    delivery.poll();
    var notice = delivery.list(owner).getFirst();
    assertThat(notice.status()).isEqualTo("submitted_unverified");
    assertThat(notice.reason()).isEqualTo("native_sdk_no_receipt");
    for (int n = 0; n < 3; n++) delivery.poll();
    assertThat(REMOTE.sends).hasValue(1);
    assertThat(delivery.list(new Owner("another", "person"))).isEmpty();
  }

  @Test
  void transportFailureAndCrashRemainUnknownWithoutAutomaticResend() throws Exception {
    bind();
    send("hello");
    run(false);
    REMOTE.httpStatus = 500;
    delivery.poll();
    assertThat(delivery.list(owner).getFirst().status()).isEqualTo("unknown");
    delivery.poll();
    assertThat(REMOTE.sends).hasValue(1);
    send("next");
    run(false);
    delivery.project(binding().id());
    var job = delivery.claim();
    assertThat(job).isNotNull();
    sql.execute(
        "UPDATE channel.notices SET started_at=clock_timestamp()-interval '61 seconds' WHERE status='sending'");
    delivery.poll();
    assertThat(db.getNoticeForUpdate(job.id()).status()).isEqualTo("unknown");
    assertThat(REMOTE.sends).hasValue(1);
  }

  @Test
  void textClarificationAndVersionBoundApprovalUseNativeTaskResume() throws Exception {
    bind();
    send("city");
    run(true);
    delivery.poll();
    assertThat(REMOTE.writes.getFirst().path("content").asText()).contains("请输入城市");
    send("上海");
    run(true);
    assertThat(tasks.get(owner, conversation, binding().activeTurn()).status())
        .isEqualTo("completed");
    send("write");
    run(true);
    delivery.poll();
    var b = binding();
    var turn = tasks.get(owner, conversation, b.activeTurn());
    var approval = governance.interactions(owner, conversation, turn.id()).getFirst();
    String command = "/approve " + FeishuText.token(b, turn, approval);
    assertThat(REMOTE.writes.getLast().path("content").asText())
        .contains(command)
        .doesNotContain("PRIVATE_REPORT_PARAMETER", "PRIVATE_INTERMEDIATE_TEXT");
    var event = message("approved", "om_approve", command);
    service.receive(event);
    service.receive(event);
    run(true);
    assertThat(tasks.get(owner, conversation, turn.id()).status()).isEqualTo("completed");
    assertThat(sql.queryForObject("SELECT count(*) FROM business.reports", Integer.class))
        .isEqualTo(1);
    assertThatThrownBy(() -> send(command)).hasMessage("stale_approval");
  }

  @Test
  void muteAndConversationSwitchFenceOldApprovalCommands() throws Exception {
    bind();
    send("write");
    run(true);
    var b = binding();
    var turn = tasks.get(owner, conversation, b.activeTurn());
    var i = governance.interactions(owner, conversation, turn.id()).getFirst();
    String command = "/approve " + FeishuText.token(b, turn, i);
    send("/mute");
    delivery.poll();
    assertThat(REMOTE.sends).hasValue(0);
    assertThatThrownBy(() -> send(command)).hasMessage("stale_approval");
    send("/unmute");
    delivery.poll();
    assertThat(REMOTE.sends).hasValue(1);
    send("/new");
    assertThatThrownBy(() -> send(command)).hasMessage("stale_approval");
  }

  @Test
  void staleWaitingNoticeCannotBeSentAfterCompletionOrCancellation() throws Exception {
    bind();
    send("city");
    run(true);
    delivery.project(binding().id());
    send("上海");
    run(true);
    assertThat(delivery.claim()).isNull();
    delivery.poll();
    assertThat(REMOTE.sends).hasValue(1);
    assertThat(REMOTE.writes.getFirst().path("content").asText())
        .contains("done")
        .doesNotContain("请输入城市");
    send("hello");
    send("/cancel");
    delivery.poll();
    assertThat(REMOTE.writes.getLast().path("content").asText()).contains("任务已取消");
  }

  @Test
  void concurrentDuplicateCallbacksAndNoticeClaimsHaveOneOwner() throws Exception {
    bind();
    var event = message("parallel-event", "om_parallel", "hello");
    try (var pool = Executors.newVirtualThreadPerTaskExecutor()) {
      var futures = new ArrayList<Future<?>>();
      for (int n = 0; n < 6; n++) futures.add(pool.submit(() -> service.receive(event)));
      for (var f : futures) f.get(10, TimeUnit.SECONDS);
      assertThat(sql.queryForObject("SELECT count(*) FROM business.turns", Integer.class))
          .isEqualTo(1);
      run(false);
      delivery.project(binding().id());
      var claims = new ArrayList<Future<Notice>>();
      for (int n = 0; n < 4; n++) claims.add(pool.submit(delivery::claim));
      int accepted = 0;
      for (var f : claims) if (f.get(10, TimeUnit.SECONDS) != null) accepted++;
      assertThat(accepted).isEqualTo(1);
    }
  }

  @Test
  void unsupportedCardCallbacksNeverAnswerBusinessInteraction() throws Exception {
    bind();
    send("write");
    run(true);
    var event = message("unsupported-card", "om_card", "ignored");
    ((com.fasterxml.jackson.databind.node.ObjectNode) event.path("header"))
        .put("event_type", "card.action.trigger");
    service.receive(event);
    assertThat(tasks.get(owner, conversation, binding().activeTurn()).status())
        .isEqualTo("waiting");
    assertThat(sql.queryForObject("SELECT count(*) FROM business.reports", Integer.class)).isZero();
  }
}
