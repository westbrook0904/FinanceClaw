package com.financeclaw.architecture;

import static org.assertj.core.api.Assertions.*;

import com.financeclaw.domain.Governance.Artifact;
import com.financeclaw.domain.Governance.Blocked;
import com.financeclaw.domain.Task.Problem;
import com.financeclaw.support.ArtifactPayload;
import com.financeclaw.support.PayloadDigest;
import com.financeclaw.support.PayloadJson;
import java.nio.charset.StandardCharsets;
import java.util.List;
import java.util.Map;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.params.ParameterizedTest;
import org.junit.jupiter.params.provider.CsvSource;
import org.junit.jupiter.params.provider.NullSource;
import org.junit.jupiter.params.provider.ValueSource;

/** Preserve the stored Artifact hash, JSON Pointer and byte-bounded response contracts. */
class ArtifactPayloadTest {
    @Test
    void archiveUsesCanonicalUtf8BytesAndAcceptsExactlyTwoMiB() {
        var payload = ArtifactPayload.encode("海".repeat(699050));
        assertThat(payload.bytes()).isEqualTo(2097152);
        assertThat(payload.json().getBytes(StandardCharsets.UTF_8)).hasSize(2097152);
        assertThat(payload.hash()).isEqualTo(PayloadDigest.sha256(payload.json()));
        assertThat(ArtifactPayload.encode(Map.of("b", 2, "a", 1)).json())
                .isEqualTo("{\"a\":1,\"b\":2}");
        assertThatThrownBy(() -> ArtifactPayload.encode("海".repeat(699050) + "x"))
                .isInstanceOf(Blocked.class)
                .hasMessage("artifact_too_large");
    }

    @Test
    void completeObjectIgnoresArrayPaginationArguments() {
        var value = Map.of("items", List.of(1, 2), "status", "ready");
        var artifact = artifact(value);
        var page = ArtifactPayload.read(artifact, -1, 0, "");
        assertThat(page.artifactId()).isEqualTo(artifact.id());
        assertThat(page.hash()).isEqualTo(artifact.hash());
        assertThat(page.total()).isEqualTo(1);
        assertThat(page.start()).isZero();
        assertThat(page.count()).isEqualTo(1);
        assertThat(page.data()).isEqualTo(value);
    }

    @Test
    void scalarAndNullAreCompleteValues() {
        assertThat(ArtifactPayload.read(artifact(42), 99, 0, "").data()).isEqualTo(42);
        var page = ArtifactPayload.read(artifact(null), -1, -1, "");
        assertThat(page.total()).isEqualTo(1);
        assertThat(page.count()).isEqualTo(1);
        assertThat(page.data()).isNull();
    }

    @Test
    void wholeObjectLimitUsesUtf8BytesAndAllowsExactBoundary() {
        // {"value":""} occupies 12 bytes, leaving 16,372 bytes for the value.
        var value = "海".repeat(5457) + "x";
        var exact = Map.of("value", value);
        assertThat(PayloadJson.canonical(exact).getBytes(StandardCharsets.UTF_8)).hasSize(16384);
        assertThat(ArtifactPayload.read(artifact(exact), 0, 1, "").data()).isEqualTo(exact);
        problem(
                () -> ArtifactPayload.read(artifact(Map.of("value", value + "x")), 0, 1, ""),
                422,
                "artifact_object_too_large");
    }

    @Test
    void arrayPageShrinksByEncodedBytesAndKeepsOriginalTotalAndHash() {
        var item = "海".repeat(3000);
        var artifact = artifact(List.of(item, item, item));
        var page = ArtifactPayload.read(artifact, 1, 100, "");
        assertThat(page.hash()).isEqualTo(artifact.hash());
        assertThat(page.total()).isEqualTo(3);
        assertThat(page.start()).isEqualTo(1);
        assertThat(page.count()).isEqualTo(1);
        assertThat(page.data()).isEqualTo(List.of(item));
    }

    @Test
    void arrayItemLimitIncludesArrayAndStringSyntax() {
        var exact = "海".repeat(5460);
        assertThat(PayloadJson.canonical(List.of(exact)).getBytes(StandardCharsets.UTF_8))
                .hasSize(16384);
        assertThat(ArtifactPayload.read(artifact(List.of(exact)), 0, 1, "").count()).isEqualTo(1);
        problem(
                () -> ArtifactPayload.read(artifact(List.of(exact + "x")), 0, 1, ""),
                422,
                "artifact_item_too_large");
    }

    @ParameterizedTest
    @CsvSource({"-1,1", "0,0", "0,101"})
    void invalidArrayPaginationKeepsItsError(int start, int limit) {
        problem(
                () -> ArtifactPayload.read(artifact(List.of(1)), start, limit, ""),
                422,
                "invalid_pagination");
    }

    @Test
    void outOfRangeAndEmptyArraysReturnAnEmptyPage() {
        var page = ArtifactPayload.read(artifact(List.of(1, 2)), Integer.MAX_VALUE, 100, "");
        assertThat(page.total()).isEqualTo(2);
        assertThat(page.start()).isEqualTo(2);
        assertThat(page.count()).isZero();
        assertThat(page.data()).isEqualTo(List.of());
        assertThat(ArtifactPayload.read(artifact(List.of()), 0, 1, "").count()).isZero();
    }

    @Test
    void pointerSelectsEscapedObjectKeysAndPagesOnlyTheSelectedArray() {
        var artifact = artifact(Map.of("a/b", Map.of("~items", List.of(1, 2, 3))));
        var page = ArtifactPayload.read(artifact, 1, 1, "/a~1b/~0items");
        assertThat(page.hash()).isEqualTo(artifact.hash());
        assertThat(page.total()).isEqualTo(3);
        assertThat(page.start()).isEqualTo(1);
        assertThat(page.data()).isEqualTo(List.of(2));
        assertThat(ArtifactPayload.read(artifact, -1, 0, "/a~1b/~0items/0").data()).isEqualTo(1);
    }

    @ParameterizedTest
    @NullSource
    @ValueSource(strings = {"items", "#/items"})
    void malformedPathsAreRejected(String path) {
        problem(
                () -> ArtifactPayload.read(artifact(Map.of("items", List.of(1))), 0, 1, path),
                422,
                "invalid_artifact_path");
    }

    @Test
    void pathLengthLimitIsInclusive() {
        String key = "x".repeat(255);
        assertThat(ArtifactPayload.read(artifact(Map.of(key, 1)), 0, 1, "/" + key).data())
                .isEqualTo(1);
        problem(
                () -> ArtifactPayload.read(artifact(Map.of()), 0, 1, "/" + key + "x"),
                422,
                "invalid_artifact_path");
    }

    @ParameterizedTest
    @ValueSource(strings = {"/missing", "/items/2", "/items/-", "/items/no"})
    void missingPathKeepsItsOwnErrorBeforePaginationValidation(String path) {
        problem(
                () -> ArtifactPayload.read(artifact(Map.of("items", List.of(1))), -1, 0, path),
                422,
                "artifact_path_missing");
    }

    @Test
    void hashValidationPrecedesJsonParsingAndPathValidation() {
        var corrupted =
                new Artifact(
                        "artifact",
                        "turn",
                        "conversation",
                        "tenant",
                        "subject",
                        "release",
                        "bad",
                        "{",
                        1);
        problem(() -> ArtifactPayload.read(corrupted, -1, 0, null), 409, "artifact_corrupt");
        var invalidJson =
                new Artifact(
                        "artifact",
                        "turn",
                        "conversation",
                        "tenant",
                        "subject",
                        "release",
                        PayloadDigest.sha256("{"),
                        "{",
                        1);
        assertThatThrownBy(() -> ArtifactPayload.read(invalidJson, -1, 0, null))
                .isInstanceOf(Blocked.class)
                .hasMessage("invalid_stored_json");
    }

    private static Artifact artifact(Object value) {
        var encoded = ArtifactPayload.encode(value);
        return new Artifact(
                "artifact",
                "turn",
                "conversation",
                "tenant",
                "subject",
                "release",
                encoded.hash(),
                encoded.json(),
                encoded.bytes());
    }

    private static void problem(Runnable action, int status, String code) {
        assertThatThrownBy(action::run)
                .isInstanceOfSatisfying(
                        Problem.class, error -> assertThat(error.status()).isEqualTo(status))
                .hasMessage(code);
    }
}
