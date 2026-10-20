package com.financeclaw.m3;

import static org.assertj.core.api.Assertions.*;

import com.financeclaw.app.release.ReleaseService;
import com.financeclaw.application.*;
import com.financeclaw.bootstrap.FinanceClawApplication;
import com.financeclaw.domain.Governance.*;
import com.financeclaw.domain.Task.*;
import com.financeclaw.domain.release.BusinessFeatures;
import com.financeclaw.domain.release.ModelProfile;
import com.financeclaw.domain.release.ReleaseDefinition;
import com.financeclaw.domain.release.RemoteEndpoint;
import com.financeclaw.infrastructure.agentscope.execution.*;
import com.financeclaw.infrastructure.agentscope.skills.PackagedSkills;
import com.financeclaw.infrastructure.governance.GovernanceMapper;
import com.financeclaw.infrastructure.release.BusinessResources;
import com.financeclaw.infrastructure.state.*;
import com.financeclaw.support.PayloadJson;
import com.financeclaw.tools.*;
import io.agentscope.core.message.*;
import io.agentscope.core.model.*;
import io.agentscope.core.tool.*;
import java.time.*;
import java.util.*;
import java.util.concurrent.*;
import javax.sql.DataSource;
import org.junit.jupiter.api.*;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.boot.test.context.SpringBootTest;
import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.test.context.*;
import org.testcontainers.containers.PostgreSQLContainer;
import reactor.core.publisher.Flux;

@SpringBootTest(
    classes = FinanceClawApplication.class,
    properties = {"financeclaw.worker.enabled=false", "financeclaw.integrations.enabled=false"})
class BusinessIT {
  static final String SKILL_NAME = "cocktail-from-what-i-have";
  static final String SKILL_TOOL = "load_skill_through_path";
  static final PostgreSQLContainer<?> PG =
      new PostgreSQLContainer<>("postgres:16.10-alpine")
          .withDatabaseName("m3")
          .withUsername("m3")
          .withPassword("test-only");

  static {
    PG.start();
  }

  @DynamicPropertySource
  static void database(DynamicPropertyRegistry r) {
    r.add("spring.datasource.url", PG::getJdbcUrl);
    r.add("spring.datasource.username", PG::getUsername);
    r.add("spring.datasource.password", PG::getPassword);
  }

  @Autowired TaskService tasks;
  @Autowired org.springframework.context.ApplicationContext taskContext;
  @Autowired GovernanceService governance;
  @Autowired GovernanceMapper db;
  @Autowired RuntimeMapper states;
  @Autowired ReleaseService releases;
  @Autowired DataSource source;
  JdbcTemplate sql;
  final Owner owner = new Owner("local", "developer");
  List<List<Msg>> requests = new CopyOnWriteArrayList<>();

  @BeforeEach
  void reset() {
    sql = new JdbcTemplate(source);
    sql.execute("TRUNCATE business.conversations CASCADE");
    publish(List.of(), 12, ReleaseDefinition.OFFLINE, snapshots(Instant.now()));
    requests.clear();
  }

  static String snapshots(Instant time) {
    return PayloadJson.canonical(
        Map.of(
            "AAPL",
            Map.of(
                "price",
                "100.00",
                "currency",
                "USD",
                "provider",
                "m3-synthetic-fixture",
                "as_of",
                time.toString()),
            "MSFT",
            Map.of(
                "price",
                "200.00",
                "currency",
                "USD",
                "provider",
                "m3-synthetic-fixture",
                "as_of",
                time.toString())));
  }

  void publish(List<RemoteEndpoint> remotes, int models, ModelProfile profile, String market) {
    releases.publish(
        new ReleaseDefinition(
            "m3-tests",
            "finance_agent",
            ReleaseDefinition.SCOPES,
            profile,
            null,
            models,
            32,
            new BusinessFeatures(remotes, PackagedSkills.hash(), market)));
  }

  Accepted submit(String message) {
    return tasks.submit(
        owner, tasks.createConversation(owner), UUID.randomUUID().toString(), message);
  }

  Claim claim() {
    return tasks.claim("m3-test", 60000);
  }

  Turn turn(Accepted t) {
    return tasks.get(owner, t.conversationId(), t.turnId());
  }

  TaskAgentRuntime runtime() {
    return new TaskAgentRuntime(
        tasks,
        governance,
        states,
        taskContext.getBean(
            com.financeclaw.infrastructure.agentscope.capability.BusinessCapabilities.class),
        25000) {
      @Override
      protected Model model(Claim claim) {
        return new Model() {
          public String getModelName() {
            return "m3-scripted";
          }

          public Flux<ChatResponse> stream(
              List<Msg> messages, List<ToolSchema> tools, GenerateOptions options) {
            requests.add(messages);
            var resolved =
                messages.stream()
                    .flatMap(m -> m.getContentBlocks(ToolResultBlock.class).stream())
                    .filter(r -> !r.isSuspended())
                    .toList();
            List<ContentBlock> content;
            String raw = claim.turn().message();
            var skillSchema =
                tools.stream().filter(t -> t.getName().equals(SKILL_TOOL)).findFirst();
            if (raw.startsWith("/skill ")
                && skillSchema.isPresent()
                && resolved.stream().noneMatch(r -> SKILL_TOOL.equals(r.getName()))) {
              var properties = (Map<?, ?>) skillSchema.get().getParameters().get("properties");
              String id =
                  (String)
                      ((List<?>) ((Map<?, ?>) properties.get("skillId")).get("enum")).getFirst();
              String path = raw.contains("traversal") ? "../application.yml" : "SKILL.md";
              var load =
                  com.financeclaw.infrastructure.agentscope.NativeToolCalls.call(
                      "skill-1", SKILL_TOOL, Map.of("skillId", id, "path", path));
              content =
                  raw.contains("parallel")
                      ? List.of(
                          load,
                          com.financeclaw.infrastructure.agentscope.NativeToolCalls.call(
                              "call-2", "search_catalog", Map.of("city", "上海")))
                      : List.of(load);
            } else if (raw.startsWith("/skill ")
                && raw.contains("resource")
                && resolved.size() == 1) {
              var properties =
                  (Map<?, ?>) skillSchema.orElseThrow().getParameters().get("properties");
              String id =
                  (String)
                      ((List<?>) ((Map<?, ?>) properties.get("skillId")).get("enum")).getFirst();
              content =
                  List.of(
                      com.financeclaw.infrastructure.agentscope.NativeToolCalls.call(
                          "skill-2", SKILL_TOOL, Map.of("skillId", id, "path", "SOURCE.json")));
            } else if (raw.startsWith("/skill ")
                && raw.contains("approval")
                && resolved.size() == 1) {
              content =
                  List.of(
                      com.financeclaw.infrastructure.agentscope.NativeToolCalls.call(
                          "call-1", "create_report", Map.of("title", "skill resume")));
            } else if (!resolved.isEmpty())
              content =
                  List.of(
                      TextBlock.builder()
                          .text(
                              "done "
                                  + resolved.getLast().getOutput().stream()
                                      .filter(b -> b instanceof TextBlock)
                                      .map(b -> ((TextBlock) b).getText())
                                      .findFirst()
                                      .orElse(""))
                          .build());
            else if (raw.startsWith("research"))
              content =
                  List.of(
                      com.financeclaw.infrastructure.agentscope.NativeToolCalls.call(
                          "call-1",
                          "market_research",
                          raw.contains("missing")
                              ? Map.of("task", "比较证券")
                              : Map.of(
                                  "task",
                                  "比较指定证券",
                                  "symbols",
                                  List.of("AAPL", "MSFT"),
                                  "analysis_period",
                                  "2026-10")));
            else content = List.of(TextBlock.builder().text("仅本次请求使用已选择的调酒方法。").build());
            return Flux.just(ChatResponse.builder().id("root").content(content).build());
          }
        };
      }
    };
  }

  void run(Claim c) {
    try (var worker =
        com.financeclaw.m1.TaskWorkerFixture.create(
            taskContext, runtime(), false, 1, 60000, 2000)) {
      worker.execute(c);
    } catch (InterruptedException e) {
      Thread.currentThread().interrupt();
      throw new AssertionError(e);
    }
  }

  ToolCallParam param(String tool, Map<String, Object> input) {
    return ToolCallParam.builder()
        .toolUseBlock(
            com.financeclaw.infrastructure.agentscope.NativeToolCalls.call("call-1", tool, input))
        .input(input)
        .build();
  }

  String text(ToolResultBlock block) {
    return block.getOutput().stream()
        .filter(b -> b instanceof TextBlock)
        .map(b -> ((TextBlock) b).getText())
        .findFirst()
        .orElseThrow();
  }

  @Test
  void hotelQueryDetailsTagsUseNativeTransportAndLargeResultsHaveBoundedAuthorizedReads()
      throws Exception {
    try (var mock = new MockRemote("hotel")) {
      publish(List.of(mock.definition("hotel", false, 2000)), 12, ReleaseDefinition.OFFLINE, "{}");
      var t = submit("hotel");
      var c = claim();
      try (var tools = new GovernedTools(tasks, governance, c)) {
        for (var name : List.of("searchHotels", "getHotelDetail", "getHotelSearchTags")) {
          var input =
              name.equals("searchHotels")
                  ? Map.<String, Object>of(
                      "originQuery", "上海酒店", "place", "上海", "placeType", "CITY")
                  : Map.<String, Object>of();
          mock.annotations = true;
          var result =
              tools
                  .toolkit
                  .getTool(name)
                  .callAsync(param(name, input))
                  .block(Duration.ofSeconds(8));
          String id = (String) PayloadJson.object(text(result)).get("artifact_id");
          var page = governance.read(owner, id, 790, 100, "/hotels");
          assertThat(page.total()).isEqualTo(800);
          assertThat(page.count()).isEqualTo(10);
          assertThatThrownBy(
                  () -> governance.read(new Owner("other", "developer"), id, 0, 1, "/hotels"))
              .isInstanceOf(Problem.class);
        }
        assertThat(mock.calls.get()).isEqualTo(3);
        var artifact =
            sql.queryForObject("SELECT id FROM business.artifacts LIMIT 1", String.class);
        governance.changeGrant(owner, t.conversationId(), "revoke", 1, List.of("artifact:read"));
        assertThatThrownBy(() -> governance.read(owner, artifact, 0, 1, "/hotels"))
            .isInstanceOf(Problem.class)
            .hasMessage("artifact_scope_denied");
      }
    }
  }

  @Test
  void discoveryUsesNativeSchemasAndTimeoutsCannotPublishSuccess() throws Exception {
    try (var mock = new MockRemote("hotel")) {
      var nativeToolkit = new Toolkit();
      try (var clients = new McpTools()) {
        clients.register(nativeToolkit, mock.definition("hotel", false, 2000), "local");
        assertThat(nativeToolkit.getTool("getHotelSearchTags"))
            .isInstanceOf(io.agentscope.core.tool.mcp.McpTool.class);
        assertThat(nativeToolkit.getToolNames())
            .containsExactlyInAnyOrder("searchHotels", "getHotelDetail", "getHotelSearchTags");
      }
      mock.drift = true;
      publish(List.of(mock.definition("hotel", false, 2000)), 12, ReleaseDefinition.OFFLINE, "{}");
      submit("changed-schema");
      try (var tools = new GovernedTools(tasks, governance, claim())) {
        assertThat(
                (List<String>)
                    tools.toolkit.getTool("getHotelSearchTags").getParameters().get("required"))
            .contains("injected_required_parameter");
        assertThatThrownBy(
                () ->
                    tools
                        .toolkit
                        .getTool("getHotelSearchTags")
                        .callAsync(param("getHotelSearchTags", Map.of()))
                        .block())
            .hasMessage("invalid_tool_input");
      }
      assertThat(mock.calls.get()).isZero();
    }
    try (var mock = new MockRemote("hotel")) {
      mock.delay = 1000;
      publish(List.of(mock.definition("hotel", false, 200)), 12, ReleaseDefinition.OFFLINE, "{}");
      submit("timeout");
      try (var tools = new GovernedTools(tasks, governance, claim())) {
        assertThatThrownBy(
                () ->
                    tools
                        .toolkit
                        .getTool("getHotelSearchTags")
                        .callAsync(param("getHotelSearchTags", Map.of()))
                        .block())
            .hasMessage("mcp_service_error");
      }
      assertThat(sql.queryForObject("SELECT count(*) FROM business.artifacts", Integer.class))
          .isZero();
    }
  }

  @Test
  void bearerCredentialIsResolvedAtTransportBoundaryAndNeverFrozenInRelease() throws Exception {
    try (var mock = new MockRemote("hotel")) {
      var base = mock.definition("hotel", false, 2000);
      var configured =
          new RemoteEndpoint(
              base.service(),
              base.url(),
              base.allowedHosts(),
              "M3_TEST_TOKEN",
              false,
              base.tenants(),
              base.tools(),
              base.timeoutMillis());
      publish(List.of(configured), 12, ReleaseDefinition.OFFLINE, "{}");
      submit("auth");
      var c = claim();
      try (var clients = new McpTools()) {
        var toolkit = new Toolkit();
        clients.register(
            toolkit,
            configured,
            "local",
            ref -> {
              assertThat(ref).isEqualTo("M3_TEST_TOKEN");
              return "loopback-test-only";
            });
        toolkit
            .getTool("getHotelSearchTags")
            .callAsync(param("getHotelSearchTags", Map.of()))
            .block(Duration.ofSeconds(8));
        assertThat(mock.authorization).isEqualTo("Bearer loopback-test-only");
      }
      assertThat(PayloadJson.canonical(releases.require(c.turn().releaseHash())))
          .contains("M3_TEST_TOKEN")
          .doesNotContain("loopback-test-only");
    }
  }

  @Test
  void structuredContentOnlyCannotBecomeSuccessfulBusinessEvidence() throws Exception {
    try (var mock = new MockRemote("hotel")) {
      mock.structuredOnly = true;
      publish(List.of(mock.definition("hotel", false, 2000)), 12, ReleaseDefinition.OFFLINE, "{}");
      submit("structured-only");
      try (var tools = new GovernedTools(tasks, governance, claim())) {
        assertThatThrownBy(
                () ->
                    tools
                        .toolkit
                        .getTool("getHotelSearchTags")
                        .callAsync(param("getHotelSearchTags", Map.of()))
                        .block())
            .hasMessage("mcp_invalid_result");
      }
      assertThat(sql.queryForObject("SELECT count(*) FROM business.artifacts", Integer.class))
          .isZero();
    }
  }

  @Test
  void taibuUsesNativeInputsAndClassifiesServiceErrors() throws Exception {
    try (var mock = new MockRemote("taibu")) {
      var samples = PayloadJson.object(BusinessResources.read("classpath:m3/taibu-samples.json"));
      mock.payload = ((Map<?, ?>) samples.get("almanac")).get("structuredContent");
      publish(List.of(mock.definition("taibu", false, 2000)), 12, ReleaseDefinition.OFFLINE, "{}");
      submit("taibu");
      var c = claim();
      try (var tools = new GovernedTools(tasks, governance, c)) {
        assertThat(
                text(
                    tools
                        .toolkit
                        .getTool("almanac")
                        .callAsync(param("almanac", Map.of("date", "2026-09-12")))
                        .block()))
            .contains("artifact_id");
        mock.payload = ((Map<?, ?>) samples.get("bazi")).get("structuredContent");
        assertThat(
                text(
                    tools
                        .toolkit
                        .getTool("bazi")
                        .callAsync(param("bazi", BusinessContractsTest.birth()))
                        .block()))
            .contains("artifact_id");
        mock.error = true;
        assertThatThrownBy(
                () ->
                    tools
                        .toolkit
                        .getTool("almanac")
                        .callAsync(param("almanac", Map.of("date", "2026-09-12")))
                        .block())
            .hasMessage("mcp_service_error");
      }
    }
  }

  @Test
  void nativeSkillsLoadProgressivelyAndPersistToolResultsInAgentState() {
    var t = submit("/skill " + SKILL_NAME + " resource");
    var c = claim();
    run(c);
    assertThat(turn(t).status()).isEqualTo("completed");
    String first = PayloadJson.canonical(requests.getFirst());
    assertThat(first)
        .contains(SKILL_NAME, SKILL_TOOL)
        .doesNotContain("## Framework: Build From The Shelf");
    String later = PayloadJson.canonical(requests.getLast());
    assertThat(later)
        .contains("## Framework: Build From The Shelf", "5a0326ce34b44c015fc26b5c28f6118092c806e4");
    assertThat(saved(c)).contains("## Framework: Build From The Shelf", "SOURCE.json");
    assertThat(db.policy(t.turnId()).toolCalls()).isEqualTo(2);
    assertThat(
            sql.queryForObject(
                "SELECT count(*) FROM business.artifacts WHERE turn_id=?",
                Integer.class,
                t.turnId()))
        .isZero();
    assertThat(PayloadJson.canonical(tasks.events(owner, t.conversationId(), t.turnId(), 0)))
        .contains(SKILL_TOOL)
        .doesNotContain("## Framework: Build From The Shelf");
  }

  String saved(Claim c) {
    return sql.queryForObject(
        "SELECT string_agg(payload::text,' ') FROM agent_runtime.states WHERE namespace=?",
        String.class,
        c.scope().namespace());
  }

  @Test
  void nativeSkillLoadingCanShareAToolBatchAndRejectsTraversal() {
    var parallel = submit("/skill " + SKILL_NAME + " parallel");
    run(claim());
    assertThat(turn(parallel).status()).isEqualTo("completed");
    assertThat(db.policy(parallel.turnId()).toolCalls()).isEqualTo(2);
    var traversal = submit("/skill " + SKILL_NAME + " traversal");
    var c = claim();
    run(c);
    assertThat(turn(traversal).status()).isEqualTo("failed");
    assertThat(saved(c)).doesNotContain("spring:", "datasource:");
  }

  @Test
  void skillToolResultsSurviveApprovalAndFreshAttemptResumeWithoutReloading() {
    var t = submit("/skill " + SKILL_NAME + " approval");
    var first = claim();
    run(first);
    assertThat(turn(t).status()).isEqualTo("waiting");
    assertThat(saved(first)).contains("## Framework: Build From The Shelf");
    var i = db.interactions(t.turnId()).getFirst();
    governance.answer(
        owner,
        i.id(),
        "approve",
        new Answer("approval", i.revision(), "approve", i.actionHash(), null));
    var resumed = claim();
    run(resumed);
    assertThat(turn(t).status()).isEqualTo("completed");
    assertThat(saved(resumed)).contains("## Framework: Build From The Shelf");
    assertThat(db.policy(t.turnId()).toolCalls()).isEqualTo(2);
    assertThat(resumed.scope().key()).isNotEqualTo(first.scope().key());
  }

  @Test
  void revokedSkillScopeRemovesNativeRepositoryAndToolsFromNewTurn() {
    String conversation = tasks.createConversation(owner);
    governance.changeGrant(owner, conversation, "narrow", 1, List.of("catalog:read"));
    var t = tasks.submit(owner, conversation, "without-skill", "hello");
    run(claim());
    assertThat(turn(t).status()).isEqualTo("completed");
    assertThat(PayloadJson.canonical(requests)).doesNotContain(SKILL_NAME, SKILL_TOOL);
  }

  @Test
  void researchHasIndependentChildStateNarrowedToolsAndSharedRootBudget() {
    var t = submit("research");
    var c = claim();
    run(c);
    assertThat(turn(t).status()).isEqualTo("completed");
    assertThat(turn(t).result()).contains("m3-synthetic-fixture", "as_of", "analysis_period");
    var scope = com.financeclaw.domain.research.ResearchRun.scope(c.scope(), "call-1");
    assertThat(scope.key()).isNotEqualTo(c.scope().key());
    var state =
        new MyBatisAgentStateStore(states, scope)
            .getVersioned(
                scope.userId(),
                scope.sessionId(),
                "agent_state",
                io.agentscope.core.state.AgentState.class);
    assertThat(state.isPresent()).isTrue();
    assertThat(db.policy(t.turnId()).modelCalls()).isEqualTo(4);
    assertThat(db.step(t.turnId(), "child:call-1", "tool:call-1")).isNotNull();
    assertThat(
            tasks.events(owner, t.conversationId(), t.turnId(), 0).stream()
                .filter(e -> e.kind().equals("tool_started"))
                .map(Event::tool))
        .contains("market_research")
        .doesNotContain("market_snapshot");
  }

  @Test
  void researchClarifiesAtRootThenResumesWithOriginalTask() {
    var t = submit("research missing");
    run(claim());
    assertThat(turn(t).status()).isEqualTo("waiting");
    var i = db.interactions(t.turnId()).getFirst();
    assertThat(i.kind()).isEqualTo("input");
    governance.answer(
        owner,
        i.id(),
        "scope",
        new Answer(
            "input",
            i.revision(),
            null,
            null,
            Map.of("symbols", List.of("AAPL"), "analysis_period", "2026-10")));
    run(claim());
    assertThat(turn(t).status()).isEqualTo("completed");
    assertThat(turn(t).result()).contains("AAPL", "2026-10");
  }

  @Test
  void researchFailureAndBudgetExhaustionCannotBecomeSuccess() {
    publish(List.of(), 2, ReleaseDefinition.OFFLINE, snapshots(Instant.now()));
    var t = submit("research");
    run(claim());
    assertThat(turn(t).status()).isNotEqualTo("completed");
    assertThat(db.policy(t.turnId()).modelCalls()).isEqualTo(2);
    publish(List.of(), 12, ReleaseDefinition.OFFLINE, "{}");
    var unavailable = submit("research");
    run(claim());
    assertThat(turn(unavailable).status()).isNotEqualTo("completed");
  }

  static String portfolio() {
    return com.financeclaw.app.portfolio.PortfolioReviewService.COMMAND
        + PayloadJson.canonical(
            Map.of(
                "portfolio_name",
                "M3组合",
                "positions",
                List.of(
                    Map.of("symbol", "AAPL", "quantity", "1", "cost_basis", "90"),
                    Map.of("symbol", "MSFT", "quantity", "3", "cost_basis", "150"))));
  }

  @Test
  void workflowRecoversInASeparateJvmUsingPersistedSteps() throws Exception {
    String market = snapshots(Instant.now());
    publish(List.of(), 12, ReleaseDefinition.OFFLINE, market);
    var t = submit(portfolio());
    var config = new Properties();
    config.setProperty("url", PG.getJdbcUrl());
    config.setProperty("user", PG.getUsername());
    config.setProperty("password", PG.getPassword());
    config.setProperty("market", market);
    var file = java.nio.file.Files.createTempFile("m3-database-", ".properties");
    var evidence = java.nio.file.Path.of("target/m3");
    java.nio.file.Files.createDirectories(evidence);
    List<Map<String, Object>> exits = new ArrayList<>();
    try {
      try (var out = java.nio.file.Files.newOutputStream(file)) {
        config.store(out, "isolated test database");
      }
      for (String expected : List.of("waiting", "completed")) {
        if (expected.equals("completed")) {
          var i = db.interactions(t.turnId()).getFirst();
          governance.answer(
              owner,
              i.id(),
              "fork-approve",
              new Answer("approval", i.revision(), "approve", i.actionHash(), null));
        }
        var process =
            new ProcessBuilder(
                    java.nio.file.Path.of(System.getProperty("java.home"), "bin/java").toString(),
                    "-cp",
                    System.getProperty("java.class.path"),
                    ForkedBusiness.class.getName(),
                    file.toString(),
                    expected)
                .redirectErrorStream(true)
                .redirectOutput(evidence.resolve("workflow-" + expected + ".log").toFile())
                .start();
        boolean exited = process.waitFor(30, TimeUnit.SECONDS);
        if (!exited) process.destroyForcibly();
        assertThat(exited).isTrue();
        assertThat(process.exitValue()).isZero();
        exits.add(
            Map.of("phase", expected, "pid", process.pid(), "exit_code", process.exitValue()));
        assertThat(turn(t).status()).isEqualTo(expected);
      }
      java.nio.file.Files.writeString(
          evidence.resolve("process-exits.json"), PayloadJson.canonical(exits) + "\n");
    } finally {
      java.nio.file.Files.deleteIfExists(file);
    }
    assertThat(
            sql.queryForObject(
                "SELECT count(*) FROM business.artifacts WHERE turn_id=?",
                Integer.class,
                t.turnId()))
        .isEqualTo(1);
    assertThat(db.policy(t.turnId()).modelCalls()).isZero();
  }

  @Test
  void parallelResearchWithTheSameCallIdsCannotShareStateOrEvidence() throws Exception {
    var first = submit("research");
    var one = claim();
    var other = new Owner("other-tenant", "developer");
    var second = tasks.submit(other, tasks.createConversation(other), "second", "research");
    var two = claim();
    try (var pool = Executors.newFixedThreadPool(2)) {
      var a = pool.submit(() -> run(one));
      var b = pool.submit(() -> run(two));
      a.get(25, TimeUnit.SECONDS);
      b.get(25, TimeUnit.SECONDS);
    }
    assertThat(turn(first).status()).isEqualTo("completed");
    assertThat(tasks.get(other, second.conversationId(), second.turnId()).status())
        .isEqualTo("completed");
    assertThat(com.financeclaw.domain.research.ResearchRun.scope(one.scope(), "call-1").key())
        .isNotEqualTo(
            com.financeclaw.domain.research.ResearchRun.scope(two.scope(), "call-1").key());
    assertThat(db.step(first.turnId(), "child:call-1", "result")).isNotNull();
    assertThat(db.step(second.turnId(), "child:call-1", "result")).isNotNull();
  }

  @Test
  void skillRevocationAfterAnswerBlocksResume() {
    var t = submit("/skill " + SKILL_NAME + " approval");
    var c = claim();
    run(c);
    var i = db.interactions(t.turnId()).getFirst();
    governance.answer(
        owner,
        i.id(),
        "approve",
        new Answer("approval", i.revision(), "approve", i.actionHash(), null));
    governance.changeGrant(
        owner, t.conversationId(), "revoke", 1, List.of("report:write", "artifact:read"));
    run(claim());
    assertThat(turn(t).status()).isEqualTo("blocked");
    assertThat(sql.queryForObject("SELECT count(*) FROM business.reports", Integer.class)).isZero();
  }

  @Test
  void taibuConfidentialEgressAndTenantMismatchAreRejectedBeforeNetwork() {
    var contract = List.of("bazi");
    var remote =
        new RemoteEndpoint(
            "taibu",
            "https://example.invalid/mcp",
            List.of("example.invalid"),
            "",
            true,
            List.of("local"),
            contract,
            1000);
    var t = submit("taibu-denied");
    var c = claim();
    try (var transport = new McpTools()) {
      assertThatThrownBy(() -> transport.register(new Toolkit(), remote, c.turn().tenant()))
          .hasMessage("confidential_egress_denied");
      var denied =
          new RemoteEndpoint(
              "taibu",
              remote.url(),
              remote.allowedHosts(),
              "",
              true,
              List.of("someone-else"),
              contract,
              1000);
      assertThatThrownBy(() -> transport.register(new Toolkit(), denied, c.turn().tenant()))
          .hasMessage("mcp_tenant_denied");
    }
    assertThat(db.policy(t.turnId()).toolCalls()).isZero();
  }

  @Test
  void deterministicWorkflowPausesBeforePublicationAndDuplicateAnswersPublishOnce() {
    var t = submit(portfolio());
    var first = claim();
    run(first);
    assertThat(turn(t).status()).isEqualTo("waiting");
    var i = db.interactions(t.turnId()).getFirst();
    assertThat(i.tool()).isEqualTo("publish_portfolio_report");
    assertThat(
            sql.queryForObject(
                "SELECT count(*) FROM business.artifacts WHERE turn_id=?",
                Integer.class,
                t.turnId()))
        .isZero();
    assertThat(db.step(t.turnId(), "workflow:portfolio_review:1.1.0", "analyze_exposure").result())
        .contains("700.00", "0.8571");
    var answer = new Answer("approval", i.revision(), "approve", i.actionHash(), null);
    governance.answer(owner, i.id(), "one", answer);
    assertThat(governance.answer(owner, i.id(), "two", answer).idempotentReplay()).isTrue();
    run(claim());
    assertThat(turn(t).status()).isEqualTo("completed");
    assertThat(db.policy(t.turnId()).modelCalls()).isZero();
    assertThat(
            sql.queryForObject(
                "SELECT count(*) FROM business.artifacts WHERE turn_id=?",
                Integer.class,
                t.turnId()))
        .isEqualTo(1);
  }

  @Test
  void staleSnapshotsAndDeniedApprovalCannotPublish() {
    publish(
        List.of(),
        12,
        ReleaseDefinition.OFFLINE,
        snapshots(Instant.now().minus(Duration.ofDays(10))));
    var stale = submit(portfolio());
    run(claim());
    assertThat(turn(stale).status()).isNotEqualTo("completed");
    assertThat(db.interactions(stale.turnId())).isEmpty();
    publish(List.of(), 12, ReleaseDefinition.OFFLINE, snapshots(Instant.now()));
    var denied = submit(portfolio());
    run(claim());
    var i = db.interactions(denied.turnId()).getFirst();
    governance.answer(
        owner, i.id(), "no", new Answer("approval", i.revision(), "reject", i.actionHash(), null));
    run(claim());
    assertThat(turn(denied).status()).isEqualTo("failed");
    assertThat(sql.queryForObject("SELECT count(*) FROM business.artifacts", Integer.class))
        .isZero();
  }

  com.financeclaw.app.portfolio.PortfolioReviewService portfolioService() {
    return taskContext.getBean(com.financeclaw.app.portfolio.PortfolioReviewService.class);
  }

  com.financeclaw.app.research.MarketResearchService researchService() {
    return taskContext.getBean(com.financeclaw.app.research.MarketResearchService.class);
  }

  com.financeclaw.infrastructure.agentscope.capability.MarketResearchAgent child(
      Claim claim, Model model) {
    return new com.financeclaw.infrastructure.agentscope.capability.MarketResearchAgent(
        researchService(),
        taskContext.getBean(com.financeclaw.app.execution.ExecutionLeaseService.class),
        states,
        claim,
        model);
  }

  static Map<String, Object> researchInput() {
    return Map.of(
        "task", "bounded research", "symbols", List.of("AAPL"), "analysis_period", "2026-10");
  }

  @Test
  void portfolioArchiveAndStepReceiptRollbackTogetherAndReplayOnlyOnce() {
    var t = submit(portfolio());
    var c = claim();
    var request =
        com.financeclaw.infrastructure.agentscope.capability.PortfolioInputs.parse(
            PayloadJson.object(
                c.turn()
                    .message()
                    .substring(
                        com.financeclaw.app.portfolio.PortfolioReviewService.COMMAND.length())));
    var prepared = portfolioService().prepare(c, request);
    String hash = (String) prepared.get("draft_hash");
    var fault =
        new com.financeclaw.app.portfolio.PortfolioReviewService(
            taskContext.getBean(com.financeclaw.app.execution.CapabilityStepService.class),
            taskContext.getBean(com.financeclaw.app.execution.ExecutionPolicyService.class),
            taskContext.getBean(com.financeclaw.app.market.MarketDataService.class),
            (claim, draft) -> {
              assertThat(
                      org.springframework.transaction.support.TransactionSynchronizationManager
                          .isActualTransactionActive())
                  .isTrue();
              governance.archive(claim, draft, "workflow:run");
              throw new IllegalStateException("after_archive");
            });
    assertThatThrownBy(() -> fault.publish(c, hash)).hasMessage("after_archive");
    assertThat(
            sql.queryForObject(
                "SELECT count(*) FROM business.artifacts WHERE turn_id=?",
                Integer.class,
                t.turnId()))
        .isZero();
    assertThat(
            db.step(
                t.turnId(),
                com.financeclaw.app.portfolio.PortfolioReviewService.NAMESPACE,
                "publish_report"))
        .isNull();
    var result = portfolioService().publish(c, hash);
    assertThat(portfolioService().publish(c, hash)).isEqualTo(result);
    assertThatThrownBy(() -> portfolioService().publish(c, "0".repeat(64)))
        .hasMessage("workflow_draft_mismatch");
    assertThat(
            sql.queryForObject(
                "SELECT count(*) FROM business.artifacts WHERE turn_id=?",
                Integer.class,
                t.turnId()))
        .isEqualTo(1);
  }

  @Test
  void frozenLegacyApprovalStepCanPublishWithoutRecomputingOldMarketData() throws Exception {
    var t = submit(portfolio());
    var c = claim();
    Map<String, Object> old;
    try (var in =
        getClass().getResourceAsStream("/architecture/portfolio-research-baseline.json")) {
      old =
          PayloadJson.object(
              new String(
                  Objects.requireNonNull(in).readAllBytes(),
                  java.nio.charset.StandardCharsets.UTF_8));
    }
    var prepared = (Map<?, ?>) old.get("prepared");
    var steps = (Map<?, ?>) old.get("steps");
    var approval = (Map<?, ?>) steps.get("publication_approval");
    db.saveStep(
        t.turnId(),
        com.financeclaw.app.portfolio.PortfolioReviewService.NAMESPACE,
        "publication_approval",
        (String) approval.get("input_hash"),
        PayloadJson.canonical(prepared));
    String hash = (String) prepared.get("draft_hash");
    assertThat(portfolioService().preparedHash(c)).isEqualTo(hash);
    var published = (Map<?, ?>) portfolioService().publish(c, hash);
    assertThat(portfolioService().publish(c, hash)).isEqualTo(published);
    assertThat(db.policy(t.turnId()).toolCalls()).isZero();
    assertThat(
            sql.queryForObject(
                "SELECT payload::text FROM business.artifacts WHERE turn_id=?",
                String.class,
                t.turnId()))
        .contains("r5-frozen-fixture");
  }

  @Test
  void cachedChildResultChecksInputAndCurrentAuthorizationBeforeReturning() {
    var t = submit("cached child");
    var c = claim();
    var input = researchInput();
    var steps = taskContext.getBean(com.financeclaw.app.execution.CapabilityStepService.class);
    steps.run(
        c,
        "child:call-1",
        "result",
        input,
        () -> Map.of("outcome", "success", "summary", "frozen child result"));
    var model = org.mockito.Mockito.mock(Model.class);
    assertThat(
            child(c, model)
                .run(param("market_research", input))
                .block(Duration.ofSeconds(5))
                .getOutput())
        .isNotEmpty();
    org.mockito.Mockito.verifyNoInteractions(model);
    assertThat(db.policy(t.turnId()).modelCalls()).isZero();
    var changed = new TreeMap<>(input);
    changed.put("analysis_period", "other");
    assertThatThrownBy(() -> child(c, model).run(param("market_research", changed)).block())
        .hasMessage("child_input_conflict");
    governance.changeGrant(owner, t.conversationId(), "revoke-child", 1, List.of("market:read"));
    assertThatThrownBy(() -> child(c, model).run(param("market_research", input)).block())
        .hasMessage("authorization_changed");
    org.mockito.Mockito.verifyNoInteractions(model);
  }

  @Test
  void nativeChildModelLimitIsEnforcedAgainstTheSameRootCounters() {
    var t = submit("bounded model");
    var c = claim();
    var calls = new java.util.concurrent.atomic.AtomicInteger();
    Model endless =
        new Model() {
          public String getModelName() {
            return "child-model-budget-probe";
          }

          public Flux<ChatResponse> stream(
              List<Msg> messages, List<ToolSchema> schemas, GenerateOptions options) {
            assertThat(
                    org.springframework.transaction.support.TransactionSynchronizationManager
                        .isActualTransactionActive())
                .isFalse();
            int n = calls.incrementAndGet();
            return Flux.just(
                ChatResponse.builder()
                    .id("child-" + n)
                    .content(
                        List.of(
                            com.financeclaw.infrastructure.agentscope.NativeToolCalls.call(
                                "snapshot-" + n, "market_snapshot", Map.of("symbol", "AAPL"))))
                    .build());
          }
        };
    assertThatThrownBy(
            () ->
                child(c, endless)
                    .run(param("market_research", researchInput()))
                    .block(Duration.ofSeconds(20)))
        .isInstanceOf(Blocked.class);
    assertThat(calls.get()).isEqualTo(4);
    assertThat(db.policy(t.turnId()).modelCalls()).isEqualTo(4);
    assertThat(db.step(t.turnId(), "child:call-1", "result")).isNull();
  }

  @Test
  void childCannotExpandSymbolsOrSpendMoreThanTenSnapshotCalls() {
    var t = submit("bounded snapshots");
    var c = claim();
    var execution = researchService().begin(c, "call-1", researchInput());
    assertThatThrownBy(
            () -> researchService().snapshot(execution, "outside", Map.of("symbol", "NVDA")))
        .hasMessage("child_scope_expansion");
    assertThat(db.policy(t.turnId()).toolCalls()).isZero();
    var bounded = researchService().begin(c, "call-2", researchInput());
    for (int n = 0; n < 10; n++)
      researchService().snapshot(bounded, "snapshot-" + n, Map.of("symbol", "aapl"));
    var exhausted = bounded;
    assertThatThrownBy(
            () -> researchService().snapshot(exhausted, "overflow", Map.of("symbol", "AAPL")))
        .hasMessage("child_tool_budget_exceeded");
    assertThat(db.policy(t.turnId()).toolCalls()).isEqualTo(10);
    assertThatThrownBy(() -> researchService().complete(exhausted, false, "not complete"))
        .hasMessage("child_incomplete");
    assertThat(db.step(t.turnId(), "child:call-2", "result")).isNull();
  }

  @Test
  void cancellingTheRootStopsAnInFlightChildAndCannotSaveSuccess() throws Exception {
    var t = submit("cancel child");
    var c = claim();
    var started = new CountDownLatch(1);
    var cancelled = new CountDownLatch(1);
    var terminated = new CountDownLatch(1);
    Model slow =
        new Model() {
          public String getModelName() {
            return "child-cancellation-probe";
          }

          public Flux<ChatResponse> stream(
              List<Msg> messages, List<ToolSchema> schemas, GenerateOptions options) {
            assertThat(
                    org.springframework.transaction.support.TransactionSynchronizationManager
                        .isActualTransactionActive())
                .isFalse();
            return Flux.<ChatResponse>never()
                .doOnSubscribe(s -> started.countDown())
                .doOnCancel(cancelled::countDown);
          }
        };
    var execution =
        child(c, slow)
            .run(param("market_research", researchInput()))
            .subscribeOn(reactor.core.scheduler.Schedulers.boundedElastic())
            .doFinally(signal -> terminated.countDown())
            .subscribe(result -> {}, error -> {});
    try {
      assertThat(started.await(10, TimeUnit.SECONDS)).isTrue();
      tasks.cancel(owner, t.conversationId(), t.turnId(), "cancel-child");
      execution.dispose();
      assertThat(cancelled.await(10, TimeUnit.SECONDS)).isTrue();
      assertThat(terminated.await(10, TimeUnit.SECONDS)).isTrue();
      assertThat(db.step(t.turnId(), "child:call-1", "result")).isNull();
      assertThat(turn(t).status()).isEqualTo("cancelling");
    } finally {
      execution.dispose();
    }
  }
}
