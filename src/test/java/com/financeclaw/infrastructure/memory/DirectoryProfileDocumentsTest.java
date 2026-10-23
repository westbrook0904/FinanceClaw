package com.financeclaw.infrastructure.memory;

import static org.assertj.core.api.Assertions.*;
import static org.mockito.Mockito.*;

import com.financeclaw.app.memory.port.ProfileDocuments;
import com.financeclaw.domain.ExecutionScope;
import com.financeclaw.domain.Task.Owner;
import com.financeclaw.domain.Task.Problem;
import java.nio.charset.StandardCharsets;
import java.nio.file.*;
import java.nio.file.attribute.PosixFilePermissions;
import java.util.List;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.io.TempDir;
import org.junit.jupiter.params.ParameterizedTest;
import org.junit.jupiter.params.provider.ValueSource;

class DirectoryProfileDocumentsTest {
    private static final Owner ALICE = new Owner("tenant", "alice");
    private static final Owner BOB = new Owner("tenant", "bob");
    @TempDir Path directory;

    @Test
    void firstReadCreatesOnlyOwnerReadableEmptyFileAndDirectories() throws Exception {
        var documents = documents();
        assertThat(documents.enabled()).isTrue();
        assertThat(documents.read(ALICE, 1)).isEmpty();
        assertThat(Files.getPosixFilePermissions(owner(ALICE)))
                .isEqualTo(PosixFilePermissions.fromString("rwx------"));
        assertThat(Files.getPosixFilePermissions(file(ALICE, 1).getParent()))
                .isEqualTo(PosixFilePermissions.fromString("rwx------"));
        assertThat(Files.getPosixFilePermissions(file(ALICE, 1)))
                .isEqualTo(PosixFilePermissions.fromString("rw-------"));
        assertThat(Files.size(file(ALICE, 1))).isZero();
    }

    @Test
    void manualEditsAndAtomicReplacementAreVisibleOnTheNextReadWithoutNormalization()
            throws Exception {
        var documents = documents();
        documents.read(ALICE, 1);
        String first = "# 用户画像\r\n- 常驻：宁波\n- 简洁回答 😀\n";
        Files.writeString(file(ALICE, 1), first, StandardCharsets.UTF_8);
        assertThat(documents.read(ALICE, 1)).isEqualTo(first);
        Path saved = file(ALICE, 1).resolveSibling("profile.md.new");
        String second = "# 已修改\n默认中文，保留尾部空格  \n";
        Files.writeString(saved, second, StandardCharsets.UTF_8);
        Files.move(
                saved,
                file(ALICE, 1),
                StandardCopyOption.ATOMIC_MOVE,
                StandardCopyOption.REPLACE_EXISTING);
        assertThat(documents.read(ALICE, 1)).isEqualTo(second);
    }

    @Test
    void ownersTenantsAndEpochsCannotShareFilesOrConstructPaths() throws Exception {
        var documents = documents();
        documents.read(ALICE, 1);
        Files.writeString(file(ALICE, 1), "Alice only");
        assertThat(documents.read(ALICE, 2)).isEmpty();
        assertThat(documents.read(BOB, 1)).isEmpty();
        var sameSubject = new Owner("other-tenant", "alice");
        assertThat(documents.read(sameSubject, 1)).isEmpty();
        var pathLikeOwner = new Owner("../../outside", "../bob/epoch-1/profile.md");
        assertThat(documents.read(pathLikeOwner, 1)).isEmpty();
        assertThat(file(pathLikeOwner, 1)).exists();
        assertThat(file(pathLikeOwner, 1).startsWith(directory.resolve("profiles"))).isTrue();
        assertThat(documents.read(ALICE, 1)).isEqualTo("Alice only");
    }

    @Test
    void byteLimitIsInclusiveAndUsesUtf8RatherThanCharacters() throws Exception {
        var documents = documents();
        documents.read(ALICE, 1);
        String exact = "海".repeat(2730) + "ab";
        Files.writeString(file(ALICE, 1), exact, StandardCharsets.UTF_8);
        assertThat(documents.read(ALICE, 1)).isEqualTo(exact);
        Files.writeString(file(ALICE, 1), exact + "x", StandardCharsets.UTF_8);
        problem(() -> documents.read(ALICE, 1), 413, "profile_too_large");
    }

    @Test
    void malformedUtf8AndNulCannotBecomeProfileText() throws Exception {
        var documents = documents();
        documents.read(ALICE, 1);
        Files.write(file(ALICE, 1), new byte[] {(byte) 0xc3, 0x28});
        problem(() -> documents.read(ALICE, 1), 422, "invalid_profile_markdown");
        Files.writeString(file(ALICE, 1), "text\0tail", StandardCharsets.UTF_8);
        problem(() -> documents.read(ALICE, 1), 422, "invalid_profile_markdown");
    }

    @Test
    void aFileSymlinkCannotReadOrOverwriteItsExternalTarget() throws Exception {
        var documents = documents();
        documents.read(ALICE, 1);
        Path outside = directory.resolve("outside.md");
        Files.writeString(outside, "outside-secret");
        Files.delete(file(ALICE, 1));
        Files.createSymbolicLink(file(ALICE, 1), outside);
        problem(() -> documents.read(ALICE, 1), 503, "profile_file_unavailable");
        assertThat(Files.readString(outside)).isEqualTo("outside-secret");
    }

    @ParameterizedTest
    @ValueSource(strings = {"owner", "epoch"})
    void managedDirectorySymlinksAreRejected(String level) throws Exception {
        var documents = documents();
        documents.read(ALICE, 1);
        Files.delete(file(ALICE, 1));
        Files.delete(file(ALICE, 1).getParent());
        Path linked = file(ALICE, 1).getParent();
        Path outside = Files.createDirectory(directory.resolve("outside"));
        if (level.equals("owner")) {
            Files.delete(owner(ALICE));
            linked = owner(ALICE);
            Files.createDirectory(outside.resolve("epoch-1"));
            Files.writeString(outside.resolve("epoch-1/profile.md"), "outside-secret");
        } else {
            Files.writeString(outside.resolve("profile.md"), "outside-secret");
        }
        Files.createSymbolicLink(linked, outside);
        problem(() -> documents.read(ALICE, 1), 503, "profile_file_unavailable");
        if (level.equals("owner")) {
            problem(() -> documents.eraseThrough(ALICE, 1), 503, "profile_file_unavailable");
        } else {
            documents.eraseThrough(ALICE, 1);
            assertThat(linked).doesNotExist();
            assertThat(Files.readString(outside.resolve("profile.md"))).isEqualTo("outside-secret");
        }
    }

    @Test
    void nonRegularFilesAndInvalidRootsFailExplicitly() throws Exception {
        var documents = documents();
        documents.read(ALICE, 1);
        Files.delete(file(ALICE, 1));
        Files.createDirectory(file(ALICE, 1));
        problem(() -> documents.read(ALICE, 1), 503, "profile_file_unavailable");
        Path notDirectory = directory.resolve("plain-file");
        Files.writeString(notDirectory, "cannot host profiles");
        problem(() -> new DirectoryProfileDocuments(notDirectory), 503, "profile_file_unavailable");
    }

    @Test
    void aSaveDuringReadingFailsInsteadOfReturningAStaleOrPartlySavedBody() throws Exception {
        var documents = spy(documents());
        documents.read(ALICE, 1);
        Files.writeString(file(ALICE, 1), "old");
        doAnswer(
                        invocation -> {
                            var raw = invocation.callRealMethod();
                            Files.writeString(
                                    invocation.getArgument(0),
                                    "different body saved while reading");
                            return raw;
                        })
                .when(documents)
                .readBytes(any());
        problem(() -> documents.read(ALICE, 1), 503, "profile_file_changed");
        doCallRealMethod().when(documents).readBytes(any());
        assertThat(documents.read(ALICE, 1)).isEqualTo("different body saved while reading");
    }

    @Test
    void eraseDeletesOnlyManagedOldEpochsAndNeverFollowsLinksToOtherOwners() throws Exception {
        var documents = documents();
        for (long epoch : List.of(1L, 2L, 3L)) {
            documents.read(ALICE, epoch);
            Files.writeString(file(ALICE, epoch), "Alice epoch " + epoch);
        }
        documents.read(BOB, 1);
        Files.writeString(file(BOB, 1), "Bob survives");
        Files.createSymbolicLink(file(ALICE, 1).resolveSibling("external"), owner(BOB));
        Path other = Files.createDirectory(owner(ALICE).resolve("notes"));
        Files.writeString(other.resolve("keep.md"), "not an epoch directory");
        documents.eraseThrough(ALICE, 2);
        assertThat(file(ALICE, 1).getParent()).doesNotExist();
        assertThat(file(ALICE, 2).getParent()).doesNotExist();
        assertThat(documents.read(ALICE, 3)).isEqualTo("Alice epoch 3");
        assertThat(documents.read(BOB, 1)).isEqualTo("Bob survives");
        assertThat(other.resolve("keep.md")).exists();
        documents.eraseThrough(ALICE, 2);
        assertThat(documents.read(ALICE, 4)).isEmpty();
    }

    @Test
    void invalidEpochsAreRejectedBeforeCreatingDocumentsAndDisabledPortDoesNoIo() {
        var documents = documents();
        problem(() -> documents.read(ALICE, 0), 422, "invalid_profile_version");
        problem(() -> documents.eraseThrough(ALICE, -1), 422, "invalid_profile_version");
        assertThat(owner(ALICE)).doesNotExist();
        var disabled = ProfileDocuments.disabled();
        assertThat(disabled.enabled()).isFalse();
        assertThat(disabled.read(ALICE, 1)).isEmpty();
        disabled.eraseThrough(ALICE, 1);
    }

    private DirectoryProfileDocuments documents() {
        return new DirectoryProfileDocuments(directory.resolve("profiles"));
    }

    private Path owner(Owner owner) {
        return directory
                .resolve("profiles")
                .resolve(ExecutionScope.hash(List.of(owner.tenant(), owner.subject())));
    }

    private Path file(Owner owner, long epoch) {
        return owner(owner).resolve("epoch-" + epoch).resolve("profile.md");
    }

    private static void problem(Runnable action, int status, String code) {
        assertThatThrownBy(action::run)
                .isInstanceOfSatisfying(
                        Problem.class, failure -> assertThat(failure.status()).isEqualTo(status))
                .hasMessage(code);
    }
}
