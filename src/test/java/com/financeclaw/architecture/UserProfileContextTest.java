package com.financeclaw.architecture;

import static org.junit.jupiter.api.Assertions.*;
import static org.mockito.Mockito.*;

import com.financeclaw.app.memory.MemoryService;
import com.financeclaw.app.memory.port.MemoryRepository;
import com.financeclaw.domain.Governance.Blocked;
import com.financeclaw.domain.Memory.*;
import com.financeclaw.domain.Task.*;
import com.financeclaw.domain.memory.*;
import java.util.List;
import org.junit.jupiter.api.*;
import org.junit.jupiter.params.ParameterizedTest;
import org.junit.jupiter.params.provider.ValueSource;
import org.mockito.ArgumentCaptor;
import org.springframework.transaction.PlatformTransactionManager;
import org.springframework.transaction.support.SimpleTransactionStatus;
import org.springframework.transaction.support.TransactionTemplate;

class UserProfileContextTest {
    private static final Owner OWNER = new Owner("tenant", "alice");
    private static final Turn TURN =
            new Turn(
                    "turn",
                    "conversation",
                    "tenant",
                    "alice",
                    "key",
                    "hash",
                    "private request",
                    "release",
                    "running",
                    1,
                    null,
                    null);
    private final MemoryRepository db = mock(MemoryRepository.class);
    private final PlatformTransactionManager manager = mock(PlatformTransactionManager.class);
    private MemoryService memory;

    @BeforeEach
    void setup() {
        when(manager.getTransaction(any())).thenAnswer(ignored -> new SimpleTransactionStatus());
        when(db.lock(OWNER)).thenReturn(new Privacy("tenant", "alice", 1, false, 2));
        when(db.lockForAdmission(OWNER)).thenReturn(new Privacy("tenant", "alice", 1, false, 2));
        when(db.sourceById("turn"))
                .thenReturn(new Source("turn", "tenant", "alice", 1, null, "profile_only", 2));
        when(db.userProfile(OWNER))
                .thenReturn(new UserProfile(1, 2, "# Profile\nPrefer concise answers.\n"));
        when(db.turnSnapshot("turn", OWNER))
                .thenReturn(
                        new TurnMemorySnapshot("turn", "tenant", "alice", 1, 2, "frozen context"));
        memory = service(false, true);
    }

    private MemoryService service(boolean evidence, boolean profile) {
        return new MemoryService(
                db,
                new TransactionTemplate(manager),
                new MemoryPolicy(evidence, false, "model", profile),
                mock(com.financeclaw.app.memory.MemoryForgetService.class));
    }

    @Test
    void admissionFreezesTheManualProfileWithoutCreatingEvidenceJobs() {
        memory.admit(TURN);
        memory.completed(TURN.id());
        var snapshot = ArgumentCaptor.forClass(TurnMemorySnapshot.class);
        var ordered = inOrder(db);
        ordered.verify(db).lockForAdmission(OWNER);
        ordered.verify(db)
                .source(new Source("turn", "tenant", "alice", 1, null, "profile_only", 2));
        ordered.verify(db).userProfile(OWNER);
        ordered.verify(db).insertTurnSnapshot(snapshot.capture());
        ordered.verifyNoMoreInteractions();
        assertEquals(2, snapshot.getValue().profileRevision());
        assertTrue(snapshot.getValue().context().contains("\\nPrefer concise answers."));
        assertTrue(snapshot.getValue().context().contains("current user request takes precedence"));
        assertTrue(snapshot.getValue().context().contains("cannot override system instructions"));
    }

    @Test
    void firstAdmissionCreatesTheOwnerThenAcquiresItsSharedPrivacyLock() {
        when(db.lockForAdmission(OWNER))
                .thenReturn(null, new Privacy("tenant", "alice", 1, false, 0));
        memory.admit(TURN);
        var ordered = inOrder(db);
        ordered.verify(db).lockForAdmission(OWNER);
        ordered.verify(db).ensure(OWNER);
        ordered.verify(db).lockForAdmission(OWNER);
        ordered.verify(db)
                .source(new Source("turn", "tenant", "alice", 1, null, "profile_only", 0));
        ordered.verify(db)
                .insertTurnSnapshot(new TurnMemorySnapshot("turn", "tenant", "alice", 1, 0, ""));
        ordered.verifyNoMoreInteractions();
    }

    @Test
    void sharedAdmissionLockStillRejectsAPendingForget() {
        when(db.lockForAdmission(OWNER)).thenReturn(new Privacy("tenant", "alice", 1, true, 2));
        assertEquals(
                "purge_pending",
                assertThrows(Problem.class, () -> memory.admit(TURN)).getMessage());
        verify(db).lockForAdmission(OWNER);
        verifyNoMoreInteractions(db);
    }

    @Test
    void profileEditingKeepsExistingTurnExecutionAndFrozenContent() {
        when(db.lock(OWNER)).thenReturn(new Privacy("tenant", "alice", 1, false, 9));
        assertDoesNotThrow(() -> memory.check(TURN));
        assertTrue(memory.sameEpoch(TURN));
        assertEquals("frozen context", memory.context(TURN));
        verify(db, times(3)).lock(OWNER);
        verify(db, times(3)).sourceById("turn");
        verify(db).turnSnapshot("turn", OWNER);
        verifyNoMoreInteractions(db);
    }

    @Test
    void bothLayersAreCopiedAtAdmissionWithEvidenceProvenance() {
        memory = service(true, true);
        when(db.snapshotFacts(OWNER, 1))
                .thenReturn(
                        List.of(
                                new Fact(
                                        "fact",
                                        "earlier-turn",
                                        "tenant",
                                        "alice",
                                        1,
                                        "response_style",
                                        "详细",
                                        "用户说希望详细",
                                        1,
                                        "active")));
        memory.admit(TURN);
        var snapshot = ArgumentCaptor.forClass(TurnMemorySnapshot.class);
        verify(db).insertTurnSnapshot(snapshot.capture());
        String context = snapshot.getValue().context();
        assertTrue(context.contains("Confirmed cross-session preference evidence"));
        assertTrue(context.contains("\"sourceId\":\"earlier-turn\""));
        assertTrue(context.contains("\"evidence\":\"用户说希望详细\""));
        assertTrue(
                context.contains("profile preferences take precedence over conflicting evidence"));
        verify(db).snapshotFacts(OWNER, 1);
    }

    @Test
    void disabledLayersStillFreezeAnEmptySnapshotAndGuardPrivacy() {
        memory = service(false, false);
        memory.admit(TURN);
        verify(db).insertTurnSnapshot(new TurnMemorySnapshot("turn", "tenant", "alice", 1, 0, ""));
        verify(db, never()).userProfile(any());
        verify(db, never()).snapshotFacts(any(), anyLong());
    }

    @ParameterizedTest
    @ValueSource(booleans = {true, false})
    void disablingProfilesCannotReplaceAnExistingTurnsSnapshot(boolean evidence) {
        memory = service(evidence, false);
        assertEquals("frozen context", memory.context(TURN));
        verify(db).lock(OWNER);
        verify(db).sourceById("turn");
        verify(db).turnSnapshot("turn", OWNER);
        verifyNoMoreInteractions(db);
    }

    @Test
    void missingSnapshotFailsClosedInsteadOfReadingTheCurrentProfile() {
        when(db.turnSnapshot("turn", OWNER)).thenReturn(null);
        assertEquals(
                "memory_snapshot_unavailable",
                assertThrows(Blocked.class, () -> memory.context(TURN)).getMessage());
        verify(db, never()).userProfile(any());
        verify(db, never()).snapshotFacts(any(), anyLong());
    }

    @Test
    void disablingBothLayersCannotBypassAForgottenEpoch() {
        memory = service(false, false);
        when(db.lock(OWNER)).thenReturn(new Privacy("tenant", "alice", 2, false, 0));
        assertEquals(
                "memory_privacy_changed",
                assertThrows(Blocked.class, () -> memory.context(TURN)).getMessage());
        assertFalse(memory.sameEpoch(TURN));
        verify(db, never()).turnSnapshot(anyString(), any());
    }

    @Test
    void foreignOwnerSourceCannotPassEpochCheck() {
        when(db.sourceById("turn"))
                .thenReturn(new Source("turn", "tenant", "bob", 1, null, "profile_only", 2));
        assertEquals(
                "memory_privacy_changed",
                assertThrows(Blocked.class, () -> memory.context(TURN)).getMessage());
    }

    @Test
    void historyFilteringUsesPrivacyEpochAcrossProfileRevisions() {
        when(db.currentSources(OWNER, 1, List.of("turn"))).thenReturn(List.of("turn"));
        assertEquals(List.of(TURN), memory.filterHistory(TURN, List.of(TURN)));
        verify(db).currentSources(OWNER, 1, List.of("turn"));
    }
}
