package com.financeclaw.architecture;

import static org.assertj.core.api.Assertions.*;
import static org.mockito.ArgumentMatchers.*;
import static org.mockito.Mockito.*;

import com.financeclaw.app.execution.*;
import com.financeclaw.app.execution.port.ExecutionContextGateway;
import com.financeclaw.app.ziwei.*;
import com.financeclaw.app.ziwei.port.*;
import com.financeclaw.domain.Governance.*;
import com.financeclaw.domain.Task.*;
import com.financeclaw.domain.release.ZiweiPolicy;
import com.financeclaw.infrastructure.agentscope.capability.AgentScopeZiweiInputs;
import com.financeclaw.infrastructure.ziwei.ZiweiSchemas;
import com.financeclaw.support.*;
import com.financeclaw.ziwei.*;
import java.nio.charset.StandardCharsets;
import java.time.Instant;
import java.util.*;
import java.util.function.Supplier;
import org.junit.jupiter.api.*;
import org.junit.jupiter.params.ParameterizedTest;
import org.junit.jupiter.params.provider.ValueSource;

class ZiweiBoundariesTest {
  static final String KEY = "synthetic-r5b-key-only-01234567890123456789";
  static final ZiweiPolicy POLICY =
      ZiweiPolicy.development("ZIWEI_HMAC_KEY", "r5b-v1", "Asia/Shanghai");
  static final ZiweiDates.QueryClock CLOCK =
      new ZiweiDates.QueryClock(Instant.parse("2025-12-31T16:00:00Z"), "Asia/Shanghai");
  static final AgentScopeZiweiInputs INPUTS = new AgentScopeZiweiInputs();

  static Map<String, Object> baseline() throws Exception {
    try (var in =
        ZiweiBoundariesTest.class.getResourceAsStream("/architecture/ziwei-io-baseline.json")) {
      return PayloadJson.object(
          new String(Objects.requireNonNull(in).readAllBytes(), StandardCharsets.UTF_8));
    }
  }

  static Map<String, Object> input() {
    return Map.of(
        "birth",
        Map.of(
            "calendar",
            "solar",
            "date",
            Map.of("year", 1990, "month", 6, "day", 15),
            "time",
            Map.of("kind", "shichen", "shichen", "yin"),
            "place",
            Map.of("name", "上海"),
            "sex_for_chart",
            "male"));
  }

  static Claim claim() {
    return new Claim(
        new Command("command", "r5b-turn", "running", "attempt", 1, true, null),
        new Turn(
            "r5b-turn",
            "conversation",
            "r5b-tenant",
            "r5b-owner",
            "key",
            "hash",
            "hello",
            "release",
            "running",
            2,
            null,
            null));
  }

  @TestFactory
  Collection<DynamicTest> allOriginalSchemaIdentityStepAndChartBytesRemainCompatible()
      throws Exception {
    var tests = new ArrayList<DynamicTest>();
    for (Object value : (List<?>) baseline().get("cases")) {
      var row = ZiweiValues.map(value);
      String tool = (String) row.get("tool");
      var input = ZiweiValues.map(row.get("input"));
      tests.add(
          DynamicTest.dynamicTest(
              tool + ":" + input.get("focus"),
              () -> {
                var review = INPUTS.inspect(tool, input, CLOCK);
                assertThat(review.complete()).isTrue();
                assertThat(review.value().toString()).isEqualTo(row.get("normalized_summary"));
                String fingerprint =
                    ZiweiIdentity.fingerprint(
                        review.value(),
                        new Owner("r5b-tenant", "r5b-owner"),
                        POLICY,
                        ZiweiPolicy.key(KEY));
                assertThat(fingerprint).isEqualTo(row.get("fingerprint"));
                assertThat(PayloadJson.canonical(ZiweiSchemas.schema(tool)))
                    .isEqualTo(row.get("schema_json"));
                assertThat(PayloadJson.canonical(INPUTS.answerSchema(tool)))
                    .isEqualTo(row.get("answer_schema_json"));
                assertThat(ZiweiSchemas.description(tool)).isEqualTo(row.get("description"));
                var calculated =
                    ZiweiCalculation.calculate(
                        review.value(), ZiweiDates.Level.forTool(tool), fingerprint, POLICY);
                assertThat(PayloadDigest.sha256(PayloadJson.canonical(calculated.artifact())))
                    .isEqualTo(row.get("artifact_sha256"));
                assertThat(PayloadDigest.sha256(PayloadJson.canonical(calculated.projection())))
                    .isEqualTo(row.get("projection_sha256"));
                assertThat(
                        PayloadDigest.sha256(
                            PayloadJson.canonical(List.of(tool, input, fingerprint))))
                    .isEqualTo(row.get("result_step_hash"));
              }));
    }
    return tests;
  }

  @Test
  void fixedResourceAndReleaseIdentityStillMatchTheOriginalBytes() throws Exception {
    var old = baseline();
    assertThat(ZiweiSchemas.schemaHash())
        .isEqualTo(old.get("schema_hash"))
        .isEqualTo(POLICY.schemaHash());
    assertThat(PayloadJson.canonical(POLICY)).isEqualTo(old.get("policy_json"));
    assertThat(ZiweiIdentity.binding(ZiweiPolicy.key(KEY), POLICY, "r5b-turn"))
        .isEqualTo(old.get("key_binding"));
  }

  @ParameterizedTest
  @ValueSource(
      strings = {
        "success",
        "suspend",
        "rejected",
        "archive_failure",
        "binding_conflict",
        "clock_failure"
      })
  void executionKeyIsClearedForEveryExitAfterAcquisition(String path) {
    var configuration = mock(ZiweiConfiguration.class);
    var context = mock(ExecutionContextGateway.class);
    var policies = mock(ExecutionPolicyService.class);
    var steps = mock(CapabilityStepService.class);
    var publication = mock(ZiweiPublication.class);
    var claim = claim();
    byte[] key = ZiweiPolicy.key(KEY);
    when(configuration.policy("release")).thenReturn(POLICY);
    when(configuration.key(POLICY.keyRef())).thenReturn(key);
    when(context.requestClock("r5b-turn")).thenReturn(CLOCK.requestClock());
    when(policies.scopes(claim)).thenReturn(List.of("ziwei:read"));
    when(steps.run(eq(claim), anyString(), anyString(), any(), any()))
        .thenAnswer(
            invocation -> {
              if (path.equals("binding_conflict")) throw new Blocked("step_input_conflict");
              return invocation.<Supplier<Object>>getArgument(4).get();
            });
    when(publication.archive(eq(claim), anyMap()))
        .thenReturn(
            new Artifact(
                "id",
                "r5b-turn",
                "conversation",
                "r5b-tenant",
                "r5b-owner",
                "release",
                "hash",
                "{}",
                2));
    if (path.equals("archive_failure"))
      when(publication.archive(eq(claim), anyMap()))
          .thenThrow(new IllegalStateException("archive_failure"));
    if (path.equals("clock_failure"))
      when(context.requestClock("r5b-turn")).thenThrow(new IllegalStateException("clock_failure"));
    var service =
        new ZiweiService(
            policies,
            steps,
            new ZiweiInputService(configuration, context, INPUTS),
            configuration,
            publication);
    Map<String, Object> request = path.equals("suspend") ? Map.of() : new TreeMap<>(input());
    String tool = "ziwei_natal_chart";
    if (path.equals("rejected")) {
      tool = "ziwei_daily_chart";
      request.put("date_range", Map.of("start", "2026-01-01", "end", "2026-02-02"));
    }
    String selectedTool = tool;
    if (Set.of("archive_failure", "binding_conflict", "clock_failure").contains(path)) {
      assertThatThrownBy(() -> service.run(claim, selectedTool, "call", request))
          .hasMessage(path.equals("binding_conflict") ? "step_input_conflict" : path);
    } else {
      var result = service.run(claim, tool, "call", request);
      assertThat(result.suspended()).isEqualTo(path.equals("suspend"));
      if (!result.suspended())
        assertThat(PayloadJson.object(result.text()))
            .containsEntry("status", path.equals("rejected") ? "rejected" : "success");
    }
    assertThat(key).containsOnly((byte) 0);
    if (!Set.of("success", "archive_failure").contains(path)) verifyNoInteractions(publication);
    if (path.equals("suspend"))
      verify(steps, never()).run(eq(claim), eq("ziwei:call"), anyString(), any(), any());
  }

  @Test
  void deniedExecutionCannotReadKeysOrPublish() {
    var policies = mock(ExecutionPolicyService.class);
    var configuration = mock(ZiweiConfiguration.class);
    var steps = mock(CapabilityStepService.class);
    var publication = mock(ZiweiPublication.class);
    var inputs = mock(ZiweiInputService.class);
    when(policies.scopes(claim())).thenReturn(List.of("artifact:read"));
    var service = new ZiweiService(policies, steps, inputs, configuration, publication);
    assertThatThrownBy(() -> service.run(claim(), "ziwei_natal_chart", "call", input()))
        .hasMessage("ziwei_denied");
    verifyNoInteractions(configuration, steps, publication, inputs);
  }

  @Test
  void clarificationValidationReadsNoKeyAndPreservesSafeHttpErrors() {
    var configuration = mock(ZiweiConfiguration.class);
    var context = mock(ExecutionContextGateway.class);
    when(configuration.policy("release")).thenReturn(POLICY);
    when(context.requestClock("r5b-turn")).thenReturn(CLOCK.requestClock());
    var inputs = new ZiweiInputService(configuration, context, INPUTS);
    inputs.validateAnswer(claim(), "ziwei_natal_chart", Map.of(), input());
    assertThatThrownBy(
            () ->
                inputs.validateAnswer(
                    claim(),
                    "ziwei_natal_chart",
                    Map.of(),
                    Map.of("birth", Map.of("time", Map.of("kind", "unknown")))))
        .isInstanceOfSatisfying(
            Problem.class,
            e -> {
              assertThat(e.status()).isEqualTo(422);
              assertThat(e).hasMessage("ziwei_input_incomplete");
            });
    assertThatThrownBy(
            () ->
                inputs.validateAnswer(
                    claim(), "ziwei_natal_chart", input(), Map.of("mode", "chart_only")))
        .hasMessage("invalid_answer");
    when(configuration.policy("release")).thenReturn(null);
    assertThatThrownBy(() -> inputs.validateAnswer(claim(), "ziwei_natal_chart", Map.of(), input()))
        .isInstanceOfSatisfying(
            Problem.class,
            e -> {
              assertThat(e.status()).isEqualTo(409);
              assertThat(e).hasMessage("ziwei_disabled");
            });
    verify(configuration, never()).key(anyString());
  }

  @Test
  void fullArtifactAndProjectionRemainDeeplyImmutableWithNullableNatalTarget() {
    var normalized = INPUTS.inspect("ziwei_natal_chart", input(), CLOCK).value();
    var result =
        ZiweiCalculation.calculate(normalized, ZiweiDates.Level.NATAL, "fingerprint", POLICY);
    assertThat(result.artifact()).containsEntry("target", null);
    assertThatThrownBy(() -> result.artifact().clear())
        .isInstanceOf(UnsupportedOperationException.class);
    assertThatThrownBy(
            () ->
                ZiweiValues.map(((List<?>) result.artifact().get("facts")).getFirst())
                    .put("value", "changed"))
        .isInstanceOf(UnsupportedOperationException.class);
    assertThatThrownBy(() -> ((List<?>) result.projection().get("facts")).clear())
        .isInstanceOf(UnsupportedOperationException.class);
  }
}
