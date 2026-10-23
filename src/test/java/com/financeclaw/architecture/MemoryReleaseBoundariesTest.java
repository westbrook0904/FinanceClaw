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
    private static JsonNode contract() throws Exception {
        try (var in =
                MemoryReleaseBoundariesTest.class.getResourceAsStream(
                        "/architecture/memory-contract.json")) {
            return new ObjectMapper().readTree(Objects.requireNonNull(in));
        }
    }

    @TestFactory
    Stream<DynamicTest> currentReleaseConfigurationHasCompleteStableCredentialFreeIdentity()
            throws Exception {
        return StreamSupport.stream(contract().path("configurations").spliterator(), false)
                .map(
                        sample ->
                                DynamicTest.dynamicTest(
                                        sample.path("name").asText(),
                                        () -> {
                                            var env =
                                                    new MockEnvironment()
                                                            .withProperty(
                                                                    "financeclaw.model.api-key",
                                                                    "synthetic-model-secret");
                                            var entries =
                                                    new ArrayList<Map.Entry<String, JsonNode>>();
                                            sample.path("properties")
                                                    .fields()
                                                    .forEachRemaining(entries::add);
                                            entries.forEach(
                                                    entry ->
                                                            env.setProperty(
                                                                    entry.getKey(),
                                                                    entry.getValue().asText()));
                                            var catalog =
                                                    new ReleaseService(
                                                            new EnvironmentReleaseSource(env));
                                            var release = catalog.require(catalog.currentHash());
                                            String canonical = PayloadJson.canonical(release);
                                            var fields = new ArrayList<String>();
                                            new ObjectMapper()
                                                    .readTree(canonical)
                                                    .fieldNames()
                                                    .forEachRemaining(fields::add);
                                            assertThat(fields)
                                                    .containsExactly(
                                                            "version",
                                                            "agent",
                                                            "scopes",
                                                            "primary",
                                                            "fallback",
                                                            "maxModelCalls",
                                                            "maxToolCalls",
                                                            "business");
                                            assertThat(release.version()).isEqualTo("initial-v1");
                                            assertThat(release.agent()).isEqualTo("finance_agent");
                                            assertThat(release.scopes())
                                                    .containsExactlyElementsOf(
                                                            ReleaseDefinition.SCOPES);
                                            assertThat(canonical)
                                                    .doesNotContain(
                                                            "synthetic-model-secret",
                                                            "synthetic-r5c-key-only");
                                            assertThat(catalog.currentHash())
                                                    .isEqualTo(independentReleaseDigest(canonical));
                                            if (sample.path("name").asText().equals("ziwei")) {
                                                assertThat(release.business().ziwei().calendar())
                                                        .isEqualTo("native-lunar-1.7.7/1");
                                            }
                                            var reversed = new MockEnvironment();
                                            Collections.reverse(entries);
                                            entries.forEach(
                                                    entry ->
                                                            reversed.setProperty(
                                                                    entry.getKey(),
                                                                    entry.getValue().asText()));
                                            var repeated =
                                                    new ReleaseService(
                                                            new EnvironmentReleaseSource(reversed));
                                            assertThat(repeated.currentHash())
                                                    .isEqualTo(catalog.currentHash());
                                            assertThat(
                                                            PayloadJson.canonical(
                                                                    repeated.require(
                                                                            repeated
                                                                                    .currentHash())))
                                                    .isEqualTo(canonical);
                                        }));
    }

    /** 独立按公开的 UTF-8 长度前缀规则计算摘要，不调用被测 ReleaseIdentity/ExecutionScope。 */
    private static String independentReleaseDigest(String canonical) throws Exception {
        var buffer = new java.io.ByteArrayOutputStream();
        try (var output = new java.io.DataOutputStream(buffer)) {
            for (String part : List.of("agentscope-2.0.3", canonical)) {
                byte[] bytes = part.getBytes(java.nio.charset.StandardCharsets.UTF_8);
                output.writeInt(bytes.length);
                output.write(bytes);
            }
        }
        return HexFormat.of()
                .formatHex(
                        java.security.MessageDigest.getInstance("SHA-256")
                                .digest(buffer.toByteArray()));
    }

    @Test
    void modelBudgetAndVersionChangesProduceDistinctExecutionIdentities() {
        var catalog = new ReleaseService(new EnvironmentReleaseSource(new MockEnvironment()));
        var current = catalog.require(catalog.currentHash());
        var primary = current.primary();
        var changedModel =
                new ModelProfile(
                        primary.name(),
                        primary.baseUrl(),
                        primary.inputLimit() + 1,
                        primary.window(),
                        primary.outputReserve(),
                        primary.margin(),
                        primary.thinking());
        var changed =
                List.of(
                        new ReleaseDefinition(
                                "next-v1",
                                current.agent(),
                                current.scopes(),
                                primary,
                                current.fallback(),
                                current.maxModelCalls(),
                                current.maxToolCalls(),
                                current.business()),
                        new ReleaseDefinition(
                                current.version(),
                                current.agent(),
                                current.scopes(),
                                changedModel,
                                current.fallback(),
                                current.maxModelCalls(),
                                current.maxToolCalls(),
                                current.business()),
                        new ReleaseDefinition(
                                current.version(),
                                current.agent(),
                                current.scopes(),
                                primary,
                                current.fallback(),
                                current.maxModelCalls() + 1,
                                current.maxToolCalls(),
                                current.business()),
                        new ReleaseDefinition(
                                current.version(),
                                current.agent(),
                                current.scopes(),
                                primary,
                                current.fallback(),
                                current.maxModelCalls(),
                                current.maxToolCalls() + 1,
                                current.business()));
        var hashes = changed.stream().map(ReleaseIdentity::hash).toList();
        assertThat(hashes).doesNotContain(catalog.currentHash()).doesNotHaveDuplicates();
    }

    @TestFactory
    Stream<DynamicTest> vectorBytesKeepFloatRoundingAndNegativeZero() throws Exception {
        return StreamSupport.stream(contract().path("vectors").spliterator(), false)
                .map(
                        sample ->
                                DynamicTest.dynamicTest(
                                        sample.path("input").get(0).asText(),
                                        () -> {
                                            double[] input =
                                                    new ObjectMapper()
                                                            .treeToValue(
                                                                    sample.path("input"),
                                                                    double[].class);
                                            var vector = EmbeddingVector.of(input);
                                            String expected = sample.path("array").asText();
                                            assertThat(MemoryVectorCodec.array(vector))
                                                    .isEqualTo(expected);
                                            assertThat(MemoryVectorCodec.query(vector))
                                                    .isEqualTo(
                                                            expected.replace('{', '[')
                                                                    .replace('}', ']'));
                                            input[0] = 999;
                                            assertThat(MemoryVectorCodec.array(vector))
                                                    .isEqualTo(expected);
                                            assertThatThrownBy(() -> vector.values().set(0, 999f))
                                                    .isInstanceOf(
                                                            UnsupportedOperationException.class);
                                        }));
    }

    @TestFactory
    Stream<DynamicTest> onlyExplicitRememberCommandsAutoConfirm() throws Exception {
        var tests = new ArrayList<DynamicTest>();
        contract()
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
                                                                                .<JsonNode>
                                                                                        valueToTree(
                                                                                                Proposal
                                                                                                        .explicit(
                                                                                                                e
                                                                                                                        .getKey())))
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
        var business = new BusinessFeatures(remotes, "fixture", "{}", null);
        var original =
                new ReleaseDefinition(
                        "first",
                        "finance_agent",
                        scopes,
                        ReleaseDefinition.OFFLINE,
                        null,
                        12,
                        32,
                        business);
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
