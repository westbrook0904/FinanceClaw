package com.financeclaw.m0;

import static org.assertj.core.api.Assertions.*;

import com.fasterxml.jackson.databind.ObjectMapper;
import com.financeclaw.agent.ResumeInputs;
import com.financeclaw.domain.ExecutionScope;
import com.financeclaw.infrastructure.state.EffectLedger;
import com.financeclaw.infrastructure.state.MyBatisAgentStateStore;
import io.agentscope.core.message.ToolUseBlock;
import io.agentscope.core.state.AgentState;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.List;
import java.util.Map;
import java.util.UUID;
import java.util.concurrent.TimeUnit;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.params.ParameterizedTest;
import org.junit.jupiter.params.provider.ValueSource;

class RecoveryIT extends PostgresSupport {
  private record Exit(int code, String output) {}

  private Exit fork(ExecutionScope scope, String mode, String crash) throws Exception {
    Path directory = Path.of("target", "m0", "processes");
    Files.createDirectories(directory);
    String id = UUID.randomUUID().toString();
    Path input = directory.resolve(id + ".json");
    Path output = directory.resolve(id + ".log");
    var config =
        new ForkedProbe.Config(
            POSTGRES.getJdbcUrl(),
            POSTGRES.getUsername(),
            POSTGRES.getPassword(),
            scope,
            mode,
            crash);
    new ObjectMapper().writeValue(input.toFile(), config);
    Process process =
        new ProcessBuilder(
                Path.of(System.getProperty("java.home"), "bin", "java").toString(),
                "-cp",
                System.getProperty(
                    "surefire.test.class.path", System.getProperty("java.class.path")),
                ForkedProbe.class.getName(),
                input.toString())
            .redirectErrorStream(true)
            .redirectOutput(output.toFile())
            .start();
    try {
      if (!process.waitFor(45, TimeUnit.SECONDS)) {
        process.destroyForcibly();
        throw new AssertionError("Probe timed out: " + output);
      }
      new ObjectMapper()
          .writerWithDefaultPrettyPrinter()
          .writeValue(
              directory.resolve(id + ".exit.json").toFile(),
              Map.of(
                  "mode",
                  mode,
                  "crash_boundary",
                  crash,
                  "exit_code",
                  process.exitValue(),
                  "scope_key",
                  scope.key(),
                  "log",
                  output.getFileName().toString()));
      return new Exit(process.exitValue(), Files.readString(output));
    } finally {
      process.destroyForcibly();
      Files.deleteIfExists(input);
    }
  }

  private MyBatisAgentStateStore store(ExecutionScope scope) {
    return new MyBatisAgentStateStore(MAPPER, scope);
  }

  @Test
  void anotherJvmCannotExecuteAnAlreadyOwnedScope() throws Exception {
    var scope = scope();
    try (var ignored =
        com.financeclaw.infrastructure.state.ExecutionGate.acquire(
            DATABASE.getSqlSessionFactory(), scope)) {
      var competing = fork(scope, "normal", "");
      assertThat(competing.code()).isNotZero();
      assertThat(competing.output()).contains("already active");
      assertThat(ProbeFixtures.effects(DATABASE, scope)).isZero();
    }
    var accepted = fork(scope, "normal", "");
    assertThat(accepted.code()).withFailMessage(accepted.output()).isZero();
    assertThat(ProbeFixtures.effects(DATABASE, scope)).isEqualTo(1);
  }

  @Test
  void approvalSurvivesJvmExitAndExecutesExactlyOneEffect() throws Exception {
    var scope = scope();
    assertThat(fork(scope, "approval-start", "").code()).isZero();
    assertThat(ProbeFixtures.effects(DATABASE, scope)).isZero();
    var saved =
        store(scope)
            .getVersioned(scope.userId(), scope.sessionId(), "agent_state", AgentState.class);
    assertThat(saved.version()).isEqualTo(1);
    assertThat(
            ResumeInputs.pending(store(scope), scope, saved.version(), ProbeFixtures.CALL_ID)
                .getName())
        .isEqualTo("write_report");
    var resumed = fork(scope, "approval-resume", "");
    assertThat(resumed.code()).withFailMessage(resumed.output()).isZero();
    assertThat(resumed.output()).contains("done");
    assertThat(ProbeFixtures.effects(DATABASE, scope)).isEqualTo(1);
    var duplicate = fork(scope, "approval-resume", "");
    assertThat(duplicate.code()).isNotZero();
    assertThat(ProbeFixtures.effects(DATABASE, scope)).isEqualTo(1);
  }

  @Test
  void deniedApprovalDoesNotExecuteToolAfterRestart() throws Exception {
    var scope = scope();
    var begin = fork(scope, "approval-start", "");
    assertThat(begin.code()).withFailMessage(begin.output()).isZero();
    var denied = fork(scope, "approval-deny", "");
    assertThat(denied.code()).withFailMessage(denied.output()).isZero();
    assertThat(ProbeFixtures.effects(DATABASE, scope)).isZero();
  }

  @Test
  void clarificationSurvivesJvmExitAndBindsAnswerToQuestion() throws Exception {
    var scope = scope();
    var begin = fork(scope, "clarify-start", "");
    assertThat(begin.code()).withFailMessage(begin.output()).isZero();
    assertThat(begin.output()).contains("TOOL_SUSPENDED");
    var resumed = fork(scope, "clarify-resume", "");
    assertThat(resumed.code()).withFailMessage(resumed.output()).isZero();
    assertThat(resumed.output()).contains("done");
    var state =
        store(scope)
            .get(scope.userId(), scope.sessionId(), "agent_state", AgentState.class)
            .orElseThrow();
    assertThat(state.toJson()).contains("Shanghai");
    assertThat(ProbeFixtures.effects(DATABASE, scope)).isZero();
  }

  @Test
  void approvalRejectsWrongCallChangedArgumentsStaleVersionAndWrongOwner() throws Exception {
    var scope = scope();
    assertThat(fork(scope, "approval-start", "").code()).isZero();
    var store = store(scope);
    var saved =
        store.getVersioned(scope.userId(), scope.sessionId(), "agent_state", AgentState.class);
    var tool = ResumeInputs.pending(store, scope, saved.version(), ProbeFixtures.CALL_ID);
    String hash = ResumeInputs.actionHash(tool);
    assertThatThrownBy(
            () -> ResumeInputs.approval(store, scope, saved.version(), "wrong-call", hash, true))
        .isInstanceOf(IllegalArgumentException.class);
    assertThatThrownBy(
            () ->
                ResumeInputs.approval(store, scope, saved.version() + 1, tool.getId(), hash, true))
        .isInstanceOf(IllegalArgumentException.class);
    var changed =
        ToolUseBlock.builder()
            .id(tool.getId())
            .name(tool.getName())
            .input(Map.of("operation", "another-report"))
            .build();
    assertThatThrownBy(
            () ->
                ResumeInputs.approval(
                    store,
                    scope,
                    saved.version(),
                    tool.getId(),
                    ResumeInputs.actionHash(changed),
                    true))
        .isInstanceOf(IllegalArgumentException.class);
    var other =
        new ExecutionScope(
            "tenant-b", scope.subject(), scope.conversation(), scope.agent(), scope.release());
    assertThatThrownBy(
            () ->
                ResumeInputs.approval(
                    store(other), other, saved.version(), tool.getId(), hash, true))
        .isInstanceOf(IllegalArgumentException.class);
    assertThat(ProbeFixtures.effects(DATABASE, scope)).isZero();
  }

  @Test
  void clarificationRejectsWrongSchemaAndCall() throws Exception {
    var scope = scope();
    assertThat(fork(scope, "clarify-start", "").code()).isZero();
    var store = store(scope);
    var saved =
        store.getVersioned(scope.userId(), scope.sessionId(), "agent_state", AgentState.class);
    var tool = ResumeInputs.pending(store, scope, saved.version(), ProbeFixtures.CALL_ID);
    assertThatThrownBy(
            () ->
                ResumeInputs.clarification(
                    store,
                    scope,
                    saved.version(),
                    tool.getId(),
                    ResumeInputs.actionHash(tool),
                    " "))
        .isInstanceOf(IllegalArgumentException.class);
    assertThatThrownBy(
            () ->
                ResumeInputs.clarification(
                    store,
                    scope,
                    saved.version(),
                    "wrong-call",
                    ResumeInputs.actionHash(tool),
                    "Shanghai"))
        .isInstanceOf(IllegalArgumentException.class);
  }

  @ParameterizedTest
  @ValueSource(strings = {"before-model", "before-tool"})
  void hardCrashBeforeEffectHasNoSavedStateOrExternalEffect(String boundary) throws Exception {
    var scope = scope();
    assertThat(fork(scope, "normal", boundary).code()).isEqualTo(74);
    assertThat(store(scope).exists(scope.userId(), scope.sessionId())).isFalse();
    assertThat(ProbeFixtures.effects(DATABASE, scope)).isZero();
  }

  @Test
  void crashAfterIntentBlocksAutomaticReplayEvenWhenNoEffectIsObserved() throws Exception {
    var scope = scope();
    assertThat(fork(scope, "normal", "after-intent").code()).isEqualTo(74);
    assertThat(ProbeFixtures.effects(DATABASE, scope)).isZero();
    var retry = fork(scope, "normal", "");
    assertThat(retry.code()).isNotZero();
    assertThat(retry.output()).contains("blocked: external tool outcome");
    assertThat(ProbeFixtures.effects(DATABASE, scope)).isZero();
  }

  @Test
  void crashAfterExternalEffectBlocksUntilVerifiedReconciliationThenReusesResult()
      throws Exception {
    var scope = scope();
    assertThat(fork(scope, "normal", "after-effect").code()).isEqualTo(74);
    assertThat(ProbeFixtures.effects(DATABASE, scope)).isEqualTo(1);
    assertThat(store(scope).exists(scope.userId(), scope.sessionId())).isFalse();
    var retry = fork(scope, "normal", "");
    assertThat(retry.code()).isNotZero();
    assertThat(retry.output()).contains("blocked: external tool outcome");
    // The separate probe table acts as the external system of record.
    String hash =
        ExecutionScope.hash(
            List.of("write_report", ResumeInputs.canonical(ProbeFixtures.TOOL_INPUT)));
    new EffectLedger(MAPPER, scope).complete("report-1", hash, "{\"status\":\"created\"}");
    var verified = fork(scope, "normal", "");
    assertThat(verified.code()).withFailMessage(verified.output()).isZero();
    assertThat(ProbeFixtures.effects(DATABASE, scope)).isEqualTo(1);
  }

  @Test
  void crashAfterToolReceiptBeforeAgentSaveDoesNotRepeatEffect() throws Exception {
    var scope = scope();
    assertThat(fork(scope, "normal", "after-tool").code()).isEqualTo(74);
    assertThat(store(scope).exists(scope.userId(), scope.sessionId())).isFalse();
    assertThat(ProbeFixtures.effects(DATABASE, scope)).isEqualTo(1);
    var retry = fork(scope, "normal", "");
    assertThat(retry.code()).withFailMessage(retry.output()).isZero();
    assertThat(ProbeFixtures.effects(DATABASE, scope)).isEqualTo(1);
  }

  @Test
  void hardCrashAfterSavePreservesFinalState() throws Exception {
    var scope = scope();
    assertThat(fork(scope, "normal", "after-save").code()).isEqualTo(74);
    var saved =
        store(scope)
            .get(scope.userId(), scope.sessionId(), "agent_state", AgentState.class)
            .orElseThrow();
    assertThat(saved.getContext().getLast().getTextContent()).isEqualTo("done");
    assertThat(ProbeFixtures.effects(DATABASE, scope)).isEqualTo(1);
  }

  @Test
  void ordinaryModelFailurePersistsInputAndToolFailurePersistsErrorResult() throws Exception {
    var modelScope = scope();
    assertThat(fork(modelScope, "normal", "model-failure").code()).isNotZero();
    assertThat(
            store(modelScope)
                .get(modelScope.userId(), modelScope.sessionId(), "agent_state", AgentState.class)
                .orElseThrow()
                .toJson())
        .contains("Please use write_report");
    var toolScope = scope();
    var outcome = fork(toolScope, "normal", "tool-failure");
    assertThat(outcome.code()).withFailMessage(outcome.output()).isZero();
    assertThat(
            store(toolScope)
                .get(toolScope.userId(), toolScope.sessionId(), "agent_state", AgentState.class)
                .orElseThrow()
                .toJson())
        .contains("injected tool failure");
    assertThat(ProbeFixtures.effects(DATABASE, toolScope)).isZero();
  }
}
