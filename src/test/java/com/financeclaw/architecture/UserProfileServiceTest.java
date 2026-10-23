package com.financeclaw.architecture;

import static org.junit.jupiter.api.Assertions.*;
import static org.mockito.Mockito.*;

import com.financeclaw.app.memory.UserProfileService;
import com.financeclaw.app.memory.port.MemoryRepository;
import com.financeclaw.domain.Memory.Privacy;
import com.financeclaw.domain.Task.*;
import com.financeclaw.domain.memory.*;
import java.util.stream.Stream;
import org.junit.jupiter.api.*;
import org.junit.jupiter.params.ParameterizedTest;
import org.junit.jupiter.params.provider.MethodSource;
import org.springframework.transaction.PlatformTransactionManager;
import org.springframework.transaction.support.SimpleTransactionStatus;
import org.springframework.transaction.support.TransactionTemplate;

class UserProfileServiceTest {
    private static final Owner OWNER = new Owner("tenant", "alice");
    private final MemoryRepository db = mock(MemoryRepository.class);
    private final PlatformTransactionManager manager = mock(PlatformTransactionManager.class);
    private UserProfileService profiles;

    @BeforeEach
    void setup() {
        when(manager.getTransaction(any())).thenAnswer(ignored -> new SimpleTransactionStatus());
        when(db.lock(OWNER)).thenReturn(new Privacy("tenant", "alice", 2, false, 0));
        profiles = new UserProfileService(db, new TransactionTemplate(manager), policy());
    }

    private static MemoryPolicy policy() {
        return new MemoryPolicy(false, false, "disabled", true);
    }

    @Test
    void uninitializedProfileNeedsOnlyOneOwnerRead() {
        assertEquals(new UserProfile(2, 0, ""), profiles.read(OWNER));
        verify(db).lock(OWNER);
        verifyNoMoreInteractions(db);
    }

    @Test
    void firstOwnerInitializationLocksAgainBeforeUse() {
        when(db.lock(OWNER)).thenReturn(null, new Privacy("tenant", "alice", 1, false, 0));
        assertEquals(new UserProfile(1, 0, ""), profiles.read(OWNER));
        var ordered = inOrder(db);
        ordered.verify(db).lock(OWNER);
        ordered.verify(db).ensure(OWNER);
        ordered.verify(db).lock(OWNER);
        ordered.verifyNoMoreInteractions();
    }

    @Test
    void firstManualEditWritesOnlyTheProfileForFutureTurns() {
        when(db.advanceProfile(OWNER, 2, 0)).thenReturn(1);
        var expected = new UserProfile(2, 1, "# 画像\n偏好简洁回答。\n");

        assertEquals(expected, profiles.update(OWNER, 2, 0, expected.markdown()));

        var ordered = inOrder(db);
        ordered.verify(db).lock(OWNER);
        ordered.verify(db).advanceProfile(OWNER, 2, 0);
        ordered.verify(db).saveUserProfile(OWNER, expected);
        ordered.verify(db).audit(OWNER, 2, "profile_update", "profile.md");
        ordered.verifyNoMoreInteractions();
    }

    @Test
    void identicalTextDoesNotIncrementTheRevisionOrInvalidateExecutions() {
        var document = new UserProfile(2, 3, "# unchanged\n");
        when(db.lock(OWNER)).thenReturn(new Privacy("tenant", "alice", 2, false, 3));
        when(db.userProfile(OWNER)).thenReturn(document);

        assertEquals(document, profiles.update(OWNER, 2, 3, document.markdown()));

        verify(db).lock(OWNER);
        verify(db).userProfile(OWNER);
        verifyNoMoreInteractions(db);
    }

    @Test
    void initialEmptyDocumentRemainsRevisionZero() {
        assertEquals(new UserProfile(2, 0, ""), profiles.update(OWNER, 2, 0, ""));
        verify(db).lock(OWNER);
        verifyNoMoreInteractions(db);
    }

    @Test
    void epochAndRevisionConflictsDoNotReadOrChangeTheDocument() {
        assertEquals(
                "memory_epoch_conflict",
                assertThrows(Problem.class, () -> profiles.update(OWNER, 1, 0, "new"))
                        .getMessage());
        assertEquals(
                "profile_revision_conflict",
                assertThrows(Problem.class, () -> profiles.update(OWNER, 2, 1, "new"))
                        .getMessage());
        verify(db, times(2)).lock(OWNER);
        verifyNoMoreInteractions(db);
    }

    @Test
    void pendingForgetPreventsReadsAndEdits() {
        when(db.lock(OWNER)).thenReturn(new Privacy("tenant", "alice", 2, true, 0));
        assertEquals(
                "purge_pending",
                assertThrows(Problem.class, () -> profiles.read(OWNER)).getMessage());
        assertEquals(
                "purge_pending",
                assertThrows(Problem.class, () -> profiles.update(OWNER, 2, 0, "new"))
                        .getMessage());
        verify(db, times(2)).lock(OWNER);
        verifyNoMoreInteractions(db);
    }

    @Test
    void missingOrMismatchedDocumentsFailClosed() {
        when(db.lock(OWNER)).thenReturn(new Privacy("tenant", "alice", 2, false, 3));
        when(db.userProfile(OWNER))
                .thenReturn(
                        null,
                        new UserProfile(1, 3, "old epoch"),
                        new UserProfile(2, 2, "old revision"));
        for (int i = 0; i < 3; i++)
            assertEquals(
                    "profile_unavailable",
                    assertThrows(Problem.class, () -> profiles.read(OWNER)).getMessage());
    }

    @Test
    void disabledProfilesNeverReachPersistence() {
        profiles =
                new UserProfileService(
                        db,
                        new TransactionTemplate(manager),
                        new MemoryPolicy(true, false, "model", false));
        assertEquals(
                "profile_disabled",
                assertThrows(Problem.class, () -> profiles.read(OWNER)).getMessage());
        assertEquals(
                "profile_disabled",
                assertThrows(Problem.class, () -> profiles.update(OWNER, 2, 0, "new"))
                        .getMessage());
        verifyNoInteractions(db);
    }

    @Test
    void byteLimitAcceptsExactly8192BytesIncludingSupplementaryUnicode() {
        String exact = "😀".repeat(2048);
        assertDoesNotThrow(() -> UserProfile.validate(1, 0, exact));
        assertEquals(
                "profile_too_large",
                assertThrows(Problem.class, () -> UserProfile.validate(1, 0, exact + "a"))
                        .getMessage());
    }

    static Stream<String> invalidMarkdown() {
        return Stream.of(null, "\0", "bad\uD800", "bad\uDC00", "\uD800a", "a\uDC00b");
    }

    @ParameterizedTest
    @MethodSource("invalidMarkdown")
    void invalidMarkdownIsRejectedBeforeAnyDatabaseCall(String markdown) {
        assertEquals(
                "invalid_profile_markdown",
                assertThrows(Problem.class, () -> profiles.update(OWNER, 2, 0, markdown))
                        .getMessage());
        verifyNoInteractions(db);
    }
}
