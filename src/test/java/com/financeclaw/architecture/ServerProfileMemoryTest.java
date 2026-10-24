package com.financeclaw.architecture;

import static org.junit.jupiter.api.Assertions.*;
import static org.mockito.Mockito.*;

import com.financeclaw.app.memory.MemoryForgetService;
import com.financeclaw.app.memory.MemoryService;
import com.financeclaw.app.memory.PrivacyRecoveryService;
import com.financeclaw.app.memory.UserProfileService;
import com.financeclaw.app.memory.port.ForgetLedger;
import com.financeclaw.app.memory.port.ForgetRecoveryRepository;
import com.financeclaw.app.memory.port.MemoryRepository;
import com.financeclaw.app.memory.port.ProfileDocuments;
import com.financeclaw.app.task.TaskEventHub;
import com.financeclaw.domain.Memory.*;
import com.financeclaw.domain.Task.*;
import com.financeclaw.domain.memory.*;
import java.util.HashMap;
import java.util.List;
import java.util.Map;
import java.util.concurrent.atomic.AtomicReference;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.params.ParameterizedTest;
import org.junit.jupiter.params.provider.ValueSource;
import org.mockito.ArgumentCaptor;
import org.springframework.transaction.PlatformTransactionManager;
import org.springframework.transaction.support.SimpleTransactionStatus;
import org.springframework.transaction.support.TransactionTemplate;

/** 文件权威、SQL 版本镜像和 Turn 快照的边界；不访问真实文件系统或外部账本。 */
class ServerProfileMemoryTest {
    private static final Owner OWNER = new Owner("tenant", "alice");
    private static final String LINEAGE = "11111111-1111-4111-8111-111111111111";
    private static final ForgetFact FACT =
            new ForgetFact(
                    "22222222-2222-4222-8222-222222222222",
                    LINEAGE,
                    OWNER,
                    1,
                    "forget-file",
                    ForgetFact.hash(1));
    private static final MemoryPolicy POLICY = new MemoryPolicy(false, false, "disabled", true);
    private static final Privacy PRIVACY = new Privacy("tenant", "alice", 1, false, 1);
    private static final UserProfile PROFILE = new UserProfile(1, 1, "original profile");
    private final MemoryRepository db = mock(MemoryRepository.class);
    private final ProfileDocuments documents = mock(ProfileDocuments.class);
    private final ForgetRecoveryRepository recovery = mock(ForgetRecoveryRepository.class);
    private final ForgetLedger ledger = mock(ForgetLedger.class);
    private final TaskEventHub events = mock(TaskEventHub.class);
    private final PlatformTransactionManager manager = mock(PlatformTransactionManager.class);
    private TransactionTemplate tx;
    private UserProfileService profiles;

    @BeforeEach
    void setup() {
        when(manager.getTransaction(any())).thenAnswer(ignored -> new SimpleTransactionStatus());
        tx = new TransactionTemplate(manager);
        when(db.lock(OWNER)).thenReturn(PRIVACY);
        when(db.userProfile(OWNER)).thenReturn(PROFILE);
        when(documents.enabled()).thenReturn(true);
        when(documents.read(OWNER, 1)).thenReturn(PROFILE.markdown());
        when(recovery.lockBinding())
                .thenReturn(new ForgetRecoveryRepository.Binding(LINEAGE, false));
        profiles = new UserProfileService(db, tx, POLICY, documents);
    }

    @Test
    void changedFileAdvancesTheMirrorOnceAndTheFollowingReadOnlyChecksItsContent() {
        var changed = new UserProfile(1, 2, "edited on the server");
        when(db.lock(OWNER)).thenReturn(PRIVACY, new Privacy("tenant", "alice", 1, false, 2));
        when(db.userProfile(OWNER)).thenReturn(PROFILE, changed);
        when(documents.read(OWNER, 1)).thenReturn(changed.markdown());
        when(db.advanceProfile(OWNER, 1, 1)).thenReturn(1);

        assertEquals(changed, profiles.read(OWNER));
        assertEquals(changed, profiles.read(OWNER));

        verify(db, times(2)).lock(OWNER);
        verify(db, times(2)).userProfile(OWNER);
        verify(documents, times(2)).read(OWNER, 1);
        verify(db).advanceProfile(OWNER, 1, 1);
        verify(db).saveUserProfile(OWNER, changed);
        verify(db).audit(OWNER, 1, "profile_file_update", "profile.md");
        verifyNoMoreInteractions(db);
    }

    @Test
    void unchangedFileDoesNotWriteVersionsDocumentsOrAuditRows() {
        assertEquals(PROFILE, profiles.read(OWNER));
        verify(db).lock(OWNER);
        verify(db).userProfile(OWNER);
        verify(documents).read(OWNER, 1);
        verifyNoMoreInteractions(db);
    }

    @Test
    void initialEmptyFileDoesNotCreateAProfileMirrorOrVersion() {
        when(db.lock(OWNER)).thenReturn(new Privacy("tenant", "alice", 1, false, 0));
        when(documents.read(OWNER, 1)).thenReturn("");
        assertEquals(new UserProfile(1, 0, ""), profiles.read(OWNER));
        verify(db).lock(OWNER);
        verifyNoMoreInteractions(db);
    }

    @Test
    void newTurnsFreezeTheLatestFileAndExistingTurnsNeverReadItAgain() {
        var privacy = new AtomicReference<>(PRIVACY);
        var mirror = new AtomicReference<>(PROFILE);
        var file = new AtomicReference<>(PROFILE.markdown());
        Map<String, Source> sources = new HashMap<>();
        Map<String, TurnMemorySnapshot> snapshots = new HashMap<>();
        when(db.lock(OWNER)).thenAnswer(ignored -> privacy.get());
        when(db.userProfile(OWNER)).thenAnswer(ignored -> mirror.get());
        when(documents.read(OWNER, 1)).thenAnswer(ignored -> file.get());
        when(db.advanceProfile(OWNER, 1, 1)).thenReturn(1);
        doAnswer(
                        call -> {
                            UserProfile next = call.getArgument(1);
                            mirror.set(next);
                            privacy.set(new Privacy("tenant", "alice", 1, false, next.revision()));
                            return null;
                        })
                .when(db)
                .saveUserProfile(eq(OWNER), any());
        doAnswer(
                        call -> {
                            Source source = call.getArgument(0);
                            sources.put(source.id(), source);
                            return null;
                        })
                .when(db)
                .source(any());
        doAnswer(
                        call -> {
                            TurnMemorySnapshot snapshot = call.getArgument(0);
                            snapshots.put(snapshot.turnId(), snapshot);
                            return null;
                        })
                .when(db)
                .insertTurnSnapshot(any());
        when(db.sourceById(anyString())).thenAnswer(call -> sources.get(call.getArgument(0)));
        when(db.turnSnapshot(anyString(), eq(OWNER)))
                .thenAnswer(call -> snapshots.get(call.getArgument(0)));
        var memory = new MemoryService(db, tx, POLICY, mock(MemoryForgetService.class), profiles);
        var old = turn("old");
        var current = turn("current");

        tx.executeWithoutResult(status -> memory.admit(old));
        file.set("new profile saved for the next Turn");
        tx.executeWithoutResult(status -> memory.admit(current));
        doThrow(new IllegalStateException("directory now offline")).when(documents).read(OWNER, 1);

        assertTrue(memory.context(old).contains("original profile"));
        assertFalse(memory.context(old).contains("new profile saved"));
        assertTrue(memory.context(current).contains("new profile saved for the next Turn"));
        assertEquals(1, snapshots.get("old").profileRevision());
        assertEquals(2, snapshots.get("current").profileRevision());
        verify(documents, times(2)).read(OWNER, 1);
        verify(db, times(2)).userProfile(OWNER);
        verify(db, never()).lockForAdmission(any());
        verify(db).advanceProfile(OWNER, 1, 1);
    }

    @Test
    void fileReadFailureCannotFallBackToTheMirrorOrCreateATurnSnapshot() {
        var failure = new IllegalStateException("profile directory unavailable");
        when(documents.read(OWNER, 1)).thenThrow(failure);
        var memory = new MemoryService(db, tx, POLICY, mock(MemoryForgetService.class), profiles);

        assertSame(
                failure,
                assertThrows(
                        IllegalStateException.class,
                        () -> tx.executeWithoutResult(status -> memory.admit(turn("rejected")))));
        verify(db, never()).source(any());
        verify(db, never()).insertTurnSnapshot(any());
        verify(db, never()).advanceProfile(any(), anyLong(), anyLong());
        verify(db, never()).saveUserProfile(any(), any());
        verify(manager).rollback(any());
    }

    @Test
    void invalidFileContentCannotReplaceTheValidatedMirror() {
        when(documents.read(OWNER, 1)).thenReturn("x".repeat(UserProfile.MAX_BYTES + 1));
        var problem = assertThrows(Problem.class, () -> profiles.read(OWNER));
        assertEquals("profile_too_large", problem.getMessage());
        verify(db, never()).advanceProfile(any(), anyLong(), anyLong());
        verify(db, never()).saveUserProfile(any(), any());
    }

    @Test
    void directoryManagedProfilesRejectHttpWritesBeforeDatabaseAccess() {
        var problem =
                assertThrows(Problem.class, () -> profiles.update(OWNER, 1, 1, "http replacement"));
        assertEquals(409, problem.status());
        assertEquals("profile_managed_on_server", problem.getMessage());
        verifyNoInteractions(db, manager);
    }

    @Test
    void pendingForgetPreventsEvenReadingTheServerFile() {
        when(db.lock(OWNER)).thenReturn(new Privacy("tenant", "alice", 1, true, 1));
        assertEquals(
                "purge_pending",
                assertThrows(Problem.class, () -> profiles.read(OWNER)).getMessage());
        verify(documents, never()).read(any(), anyLong());
        verify(db, never()).userProfile(any());
    }

    @Test
    void localForgetErasesFilesBeforeAdvancingEpochOrErasingSqlMemory() {
        assertEquals(2, forgetting().forget(OWNER, 1, "local-file-forget"));
        var ordered = inOrder(db, documents);
        ordered.verify(db).lock(OWNER);
        ordered.verify(documents).eraseThrough(OWNER, 1);
        ordered.verify(db).forget(OWNER);
        ordered.verify(db).eraseProfile(OWNER);
        ordered.verify(db).eraseSnapshots(OWNER);
        ordered.verify(db).eraseWorkContexts(OWNER);
        ordered.verify(db).eraseExecutionData(OWNER, 2);
        verify(db, never()).purged(any());
    }

    @Test
    void localFileEraseFailureDoesNotAdvanceEpochOrClaimSuccessfulSqlErasure() {
        var failure = new IllegalStateException("file erase unavailable");
        doThrow(failure).when(documents).eraseThrough(OWNER, 1);
        assertSame(
                failure,
                assertThrows(
                        IllegalStateException.class,
                        () -> forgetting().forget(OWNER, 1, "failed-local-file-forget")));
        verify(db, never()).forget(any());
        verify(db, never()).eraseProfile(any());
        verify(db, never()).eraseSnapshots(any());
        verify(db, never()).eraseWorkContexts(any());
        verify(db, never()).eraseExecutionData(any(), anyLong());
        verify(db, never()).recordOperation(any(), anyString(), anyString(), anyString());
        verify(db, never()).purged(any());
        verifyNoInteractions(events);
        verify(manager).rollback(any());
    }

    @Test
    void ledgerApplyCannotDeleteTheDurablePendingBeforeTheFilesAreGone() {
        ledgerSetup();
        assertEquals(2, forgetting().forget(OWNER, 1, "forget-file"));
        var fact = ArgumentCaptor.forClass(ForgetFact.class);
        var ordered = inOrder(recovery, ledger, documents);
        ordered.verify(recovery).prepare(fact.capture());
        ordered.verify(ledger).append(fact.getValue());
        ordered.verify(documents).eraseThrough(OWNER, 1);
        ordered.verify(recovery).apply(fact.getValue());
        assertEquals(OWNER, fact.getValue().owner());
        assertEquals(1, fact.getValue().fromEpoch());
        verify(recovery, never()).complete(any());
    }

    @Test
    void ledgerFileEraseFailureLeavesThePreparedFactAvailableForTheSameRetry() {
        var pending = ledgerSetup();
        var failure = new IllegalStateException("file erase unavailable");
        doThrow(failure).when(documents).eraseThrough(OWNER, 1);
        assertSame(
                failure,
                assertThrows(
                        IllegalStateException.class,
                        () -> forgetting().forget(OWNER, 1, "forget-file")));
        assertNotNull(pending.get());
        verify(ledger).append(pending.get());
        verify(recovery, never()).apply(any());
        verify(recovery, never()).complete(any());
        verify(db, never()).purged(any());
        verify(manager).commit(any());
        verify(manager).rollback(any());
    }

    @ParameterizedTest
    @ValueSource(booleans = {false, true})
    void oldReceiptOnlyErasesItsOriginalEpochAndCannotCompleteANewerPurge(
            boolean independentLedger) {
        when(ledger.enabled()).thenReturn(independentLedger);
        when(recovery.lockBinding())
                .thenReturn(new ForgetRecoveryRepository.Binding(LINEAGE, independentLedger));
        when(db.lock(OWNER)).thenReturn(new Privacy("tenant", "alice", 3, true, 4));
        when(db.operation(OWNER, "replay-file-forget"))
                .thenReturn(new Operation(ForgetFact.hash(1), "2"));

        assertEquals(2, forgetting().forget(OWNER, 1, "replay-file-forget"));
        verify(documents).eraseThrough(OWNER, 1);
        verify(documents, never()).eraseThrough(OWNER, 2);
        verify(documents, never()).eraseThrough(OWNER, 3);
        verify(db, never()).forget(any());
        verify(db, never()).purged(any());
        verify(recovery, never()).apply(any());
        verify(recovery, never()).complete(any());
        verify(ledger, never()).append(any());
    }

    @Test
    void replayErasesOldEpochFilesBeforeSqlApplyAndBeforeCompletingPurge() {
        replaySetup();
        var report = offline().replay();
        assertEquals("privacy_replayed", report.result());
        var ordered = inOrder(db, documents, recovery);
        ordered.verify(db).lock(OWNER);
        ordered.verify(documents).eraseThrough(OWNER, FACT.fromEpoch());
        ordered.verify(recovery).apply(FACT);
        ordered.verify(recovery).complete(OWNER);
        ordered.verify(recovery).unsafeRows();
    }

    @Test
    void replayFileFailureCannotApplyFactsCompletePurgeOrCertifyTheRestore() {
        replaySetup();
        var failure = new IllegalStateException("file erase unavailable");
        doThrow(failure).when(documents).eraseThrough(OWNER, FACT.fromEpoch());
        assertSame(failure, assertThrows(IllegalStateException.class, () -> offline().replay()));
        verify(recovery, never()).apply(any());
        verify(recovery, never()).complete(any());
        verify(recovery, never()).unsafeRows();
        verify(db, never()).purged(any());
        verify(manager).rollback(any());
    }

    private MemoryForgetService forgetting() {
        return new MemoryForgetService(db, tx, recovery, ledger, events, documents);
    }

    private PrivacyRecoveryService offline() {
        return new PrivacyRecoveryService(db, recovery, ledger, tx, documents);
    }

    private AtomicReference<ForgetFact> ledgerSetup() {
        var pending = new AtomicReference<ForgetFact>();
        when(ledger.enabled()).thenReturn(true);
        when(recovery.lockBinding())
                .thenReturn(new ForgetRecoveryRepository.Binding(LINEAGE, true));
        when(recovery.pending(OWNER)).thenAnswer(ignored -> pending.get());
        doAnswer(
                        call -> {
                            pending.set(call.getArgument(0));
                            return null;
                        })
                .when(recovery)
                .prepare(any());
        return pending;
    }

    private void replaySetup() {
        var binding = new ForgetRecoveryRepository.Binding(LINEAGE, true);
        var privacy = new AtomicReference<>(PRIVACY);
        when(ledger.enabled()).thenReturn(true);
        when(ledger.snapshot(LINEAGE)).thenReturn(List.of(FACT));
        when(recovery.binding()).thenReturn(binding);
        when(recovery.lockBinding()).thenReturn(binding);
        when(recovery.pending()).thenReturn(List.of());
        when(recovery.history()).thenReturn(List.of());
        when(recovery.owners()).thenAnswer(ignored -> List.of(privacy.get()));
        doAnswer(
                        call -> {
                            privacy.set(new Privacy("tenant", "alice", 2, true, 0));
                            return null;
                        })
                .when(recovery)
                .apply(FACT);
        doAnswer(
                        call -> {
                            privacy.set(new Privacy("tenant", "alice", 2, false, 0));
                            return null;
                        })
                .when(recovery)
                .complete(OWNER);
    }

    private static Turn turn(String id) {
        return new Turn(
                id,
                "conversation-" + id,
                "tenant",
                "alice",
                "key-" + id,
                "hash",
                "private request",
                "release",
                "queued",
                1,
                null,
                null);
    }
}
