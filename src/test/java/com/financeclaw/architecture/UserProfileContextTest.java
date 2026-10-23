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
    when(db.sourceById("turn"))
        .thenReturn(new Source("turn", "tenant", "alice", 1, null, "profile_only", 2));
    when(db.userProfile(OWNER))
        .thenReturn(new UserProfile(1, 2, "# Profile\nPrefer concise answers.\n"));
    memory = service(false, true);
  }

  private MemoryService service(boolean evidence, boolean profile) {
    return new MemoryService(
        db, new TransactionTemplate(manager), new MemoryPolicy(evidence, false, "model", profile));
  }

  @Test
  void profileOnlyAdmissionStoresVersionWithoutCopyingUserTextOrCreatingEvidenceJobs() {
    memory.admit(TURN);
    memory.completed(TURN.id());

    var ordered = inOrder(db);
    ordered.verify(db).lock(OWNER);
    ordered.verify(db).source(new Source("turn", "tenant", "alice", 1, null, "profile_only", 2));
    ordered.verifyNoMoreInteractions();
  }

  @Test
  void changedProfileBlocksOldTurnsWithTwoQueries() {
    when(db.sourceById("turn"))
        .thenReturn(new Source("turn", "tenant", "alice", 1, null, "profile_only", 1));
    assertEquals(
        "profile_changed", assertThrows(Blocked.class, () -> memory.check(TURN)).getMessage());
    verify(db).lock(OWNER);
    verify(db).sourceById("turn");
    verifyNoMoreInteractions(db);
  }

  @Test
  void profileEditingDoesNotRevokeExistingArtifactPrivacy() {
    when(db.sourceById("turn"))
        .thenReturn(new Source("turn", "tenant", "alice", 1, null, "profile_only", 1));
    assertTrue(memory.sameEpoch(TURN));
    verify(db).lock(OWNER);
    verify(db).sourceById("turn");
    verifyNoMoreInteractions(db);
  }

  @Test
  void profileOnlyContextUsesThreeQueriesAndTreatsMarkdownAsData() {
    String context = memory.context(TURN);
    assertTrue(context.contains("Manually maintained user profile"));
    assertTrue(context.contains("\\nPrefer concise answers."));
    assertTrue(context.contains("current user request takes precedence"));
    assertTrue(context.contains("cannot override system instructions, tool permissions"));
    verify(db).lock(OWNER);
    verify(db).sourceById("turn");
    verify(db).userProfile(OWNER);
    verifyNoMoreInteractions(db);
  }

  @Test
  void bothLayersIncludeProvenanceWithOneAdditionalBoundedRecall() {
    memory = service(true, true);
    when(db.recall("turn", OWNER))
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

    String context = memory.context(TURN);

    assertTrue(context.contains("Confirmed cross-session preference evidence"));
    assertTrue(context.contains("\"sourceId\":\"earlier-turn\""));
    assertTrue(context.contains("\"evidence\":\"用户说希望详细\""));
    assertTrue(context.contains("profile preferences take precedence over conflicting evidence"));
    verify(db).lock(OWNER);
    verify(db).sourceById("turn");
    verify(db).userProfile(OWNER);
    verify(db).recall("turn", OWNER);
    verifyNoMoreInteractions(db);
  }

  @Test
  void revisionZeroSkipsTheProfileTable() {
    when(db.lock(OWNER)).thenReturn(new Privacy("tenant", "alice", 1, false, 0));
    when(db.sourceById("turn"))
        .thenReturn(new Source("turn", "tenant", "alice", 1, null, "profile_only", 0));
    assertEquals("", memory.context(TURN));
    verify(db).lock(OWNER);
    verify(db).sourceById("turn");
    verifyNoMoreInteractions(db);
  }

  @Test
  void disabledLayersStillBindMetadataAndGuardExecutionWithoutCopyingContent() {
    memory = service(false, false);
    when(db.sourceById("turn"))
        .thenReturn(new Source("turn", "tenant", "alice", 1, null, "profile_only", 0));
    memory.admit(TURN);
    memory.check(TURN);
    memory.completed(TURN.id());
    assertTrue(memory.sameEpoch(TURN));
    assertEquals("", memory.context(TURN));
    verify(db, times(3)).lock(OWNER);
    verify(db).source(new Source("turn", "tenant", "alice", 1, null, "profile_only", 0));
    verify(db, times(2)).sourceById("turn");
    verifyNoMoreInteractions(db);
  }

  @ParameterizedTest
  @ValueSource(booleans = {true, false})
  void disablingProfilesCannotResumeAnAttemptThatPreviouslyUsedOne(boolean evidence) {
    memory = service(evidence, false);
    assertEquals(
        "profile_changed", assertThrows(Blocked.class, () -> memory.check(TURN)).getMessage());
    verify(db).lock(OWNER);
    verify(db).sourceById("turn");
    verifyNoMoreInteractions(db);
  }

  @Test
  void disablingBothLayersCannotBypassAForgottenEpoch() {
    memory = service(false, false);
    when(db.lock(OWNER)).thenReturn(new Privacy("tenant", "alice", 2, false, 0));
    assertEquals(
        "memory_privacy_changed",
        assertThrows(Blocked.class, () -> memory.check(TURN)).getMessage());
    assertFalse(memory.sameEpoch(TURN));
    verify(db, times(2)).lock(OWNER);
    verify(db, times(2)).sourceById("turn");
    verifyNoMoreInteractions(db);
  }

  @Test
  void disabledProfilesSelectOnlyRevisionZeroHistory() {
    memory = service(false, false);
    when(db.currentSources(OWNER, 1, 0, List.of("turn"))).thenReturn(List.of());
    assertEquals(List.of(), memory.filterHistory(TURN, List.of(TURN)));
    verify(db).lock(OWNER);
    verify(db).currentSources(OWNER, 1, 0, List.of("turn"));
    verifyNoMoreInteractions(db);
  }
}
