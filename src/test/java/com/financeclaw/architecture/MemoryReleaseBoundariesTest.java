package com.financeclaw.architecture;

import static org.assertj.core.api.Assertions.*;

import com.fasterxml.jackson.databind.*;
import com.financeclaw.app.release.*;
import com.financeclaw.domain.Memory.*;
import com.financeclaw.domain.memory.EmbeddingVector;
import com.financeclaw.domain.release.*;
import com.financeclaw.infrastructure.memory.MemoryVectorCodec;
import com.financeclaw.infrastructure.release.*;
import com.financeclaw.support.PayloadJson;
import com.financeclaw.ziwei.*;
import java.util.*;
import java.util.stream.*;
import org.junit.jupiter.api.*;
import org.springframework.mock.env.MockEnvironment;

class MemoryReleaseBoundariesTest {
  private static JsonNode baseline() throws Exception {
    try (var in =
        MemoryReleaseBoundariesTest.class.getResourceAsStream(
            "/architecture/memory-release-baseline.json")) {
      return new ObjectMapper().readTree(Objects.requireNonNull(in));
    }
  }

  @TestFactory
  Stream<DynamicTest> publishedReleaseBytesAndHashesMatchOriginalCompiledImplementation()
      throws Exception {
    return StreamSupport.stream(baseline().path("releases").spliterator(), false)
        .map(
            sample ->
                DynamicTest.dynamicTest(
                    sample.path("release_hash").asText(),
                    () -> {
                      var env =
                          new MockEnvironment()
                              .withProperty("financeclaw.model.api-key", "synthetic-model-secret");
                      sample
                          .path("properties")
                          .fields()
                          .forEachRemaining(
                              e -> env.setProperty(e.getKey(), e.getValue().asText()));
                      var catalog = new ReleaseService(new EnvironmentReleaseSource(env));
                      var release = catalog.require(catalog.currentHash());
                      assertThat(PayloadJson.canonical(release))
                          .isEqualTo(sample.path("release_json").asText())
                          .doesNotContain("synthetic-model-secret", "synthetic-r5c-key-only");
                      assertThat(catalog.currentHash())
                          .isEqualTo(sample.path("release_hash").asText());
                    }));
  }

  @TestFactory
  Stream<DynamicTest> vectorBytesKeepFloatRoundingAndNegativeZero() throws Exception {
    return StreamSupport.stream(baseline().path("vectors").spliterator(), false)
        .map(
            sample ->
                DynamicTest.dynamicTest(
                    sample.path("input").get(0).asText(),
                    () -> {
                      double[] input =
                          new ObjectMapper().treeToValue(sample.path("input"), double[].class);
                      var vector = EmbeddingVector.of(input);
                      String expected = sample.path("array").asText();
                      assertThat(MemoryVectorCodec.array(vector)).isEqualTo(expected);
                      assertThat(MemoryVectorCodec.query(vector))
                          .isEqualTo(expected.replace('{', '[').replace('}', ']'));
                      input[0] = 999;
                      assertThat(MemoryVectorCodec.array(vector)).isEqualTo(expected);
                      assertThatThrownBy(() -> vector.values().set(0, 999f))
                          .isInstanceOf(UnsupportedOperationException.class);
                    }));
  }

  @TestFactory
  Stream<DynamicTest> onlyOriginalExplicitCommandsAutoConfirm() throws Exception {
    var tests = new ArrayList<DynamicTest>();
    baseline()
        .path("explicit")
        .fields()
        .forEachRemaining(
            e ->
                tests.add(
                    DynamicTest.dynamicTest(
                        e.getKey(),
                        () ->
                            assertThat(
                                    new ObjectMapper()
                                        .<JsonNode>valueToTree(Proposal.explicit(e.getKey())))
                                .isEqualTo(e.getValue()))));
    return tests.stream();
  }

  @Test
  void publishingRetainsInFlightReleaseAndDefensivelyCopiesCollections() {
    var scopes = new ArrayList<>(ReleaseDefinition.SCOPES);
    var tools = new ArrayList<>(List.of("searchHotels"));
    var hosts = new ArrayList<>(List.of("hotel.invalid"));
    var tenants = new ArrayList<>(List.of("local"));
    var remotes =
        new ArrayList<>(
            List.of(
                new RemoteEndpoint(
                    "hotel",
                    "https://hotel.invalid/mcp",
                    hosts,
                    "TOKEN",
                    true,
                    tenants,
                    tools,
                    1000)));
    var business = new BusinessFeatures(remotes, "fixture", "{}");
    var original =
        new ReleaseDefinition(
            "first", "finance_agent", scopes, ReleaseDefinition.OFFLINE, null, 12, 32, business);
    var catalog = new ReleaseService(() -> original);
    String oldHash = catalog.currentHash();
    scopes.clear();
    tools.clear();
    hosts.clear();
    tenants.clear();
    remotes.clear();
    assertThat(catalog.require(oldHash)).isEqualTo(original);
    var next =
        new ReleaseDefinition(
            "second",
            "finance_agent",
            ReleaseDefinition.SCOPES,
            ReleaseDefinition.OFFLINE,
            null,
            12,
            32,
            business);
    catalog.publish(next);
    assertThat(catalog.currentHash()).isNotEqualTo(oldHash);
    assertThat(catalog.require(oldHash)).isSameAs(original);
    assertThat(catalog.require(catalog.currentHash())).isSameAs(next);
    String current = catalog.currentHash();
    assertThatThrownBy(
            () ->
                catalog.publish(
                    new ReleaseDefinition(
                        "invalid",
                        "wrong",
                        List.of(),
                        ReleaseDefinition.OFFLINE,
                        null,
                        12,
                        32,
                        business)))
        .hasMessage("Invalid release policy");
    assertThat(catalog.currentHash()).isEqualTo(current);
    assertThatThrownBy(() -> catalog.require("unknown")).hasMessage("release_unavailable");
  }

  @Test
  void credentialsRotateWithoutChangingReleaseIdentity() {
    var env = new MockEnvironment().withProperty("TOKEN", "synthetic-before");
    var catalog = new ReleaseService(new EnvironmentReleaseSource(env));
    var credentials = new EnvironmentCredentials(env);
    String hash = catalog.currentHash();
    assertThat(credentials.resolve("TOKEN")).isEqualTo("synthetic-before");
    env.setProperty("TOKEN", "synthetic-after");
    assertThat(credentials.resolve("TOKEN")).isEqualTo("synthetic-after");
    assertThat(catalog.currentHash()).isEqualTo(hash);
    assertThat(PayloadJson.canonical(catalog.require(hash)))
        .doesNotContain("synthetic-before", "synthetic-after");
  }

  @Test
  void releasePolicyMatchesPinnedCalculationKernel() {
    assertThat(ZiweiPolicy.CHART_ENGINE).isEqualTo(ZiweiChartEngine.ENGINE);
    assertThat(ZiweiPolicy.CALENDAR_DEPENDENCY).isEqualTo(ZiweiCalendar.ENGINE);
    assertThat(ZiweiPolicy.CALENDAR_RULES).isEqualTo(ZiweiCalendar.RULES);
  }
}
