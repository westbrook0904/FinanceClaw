package com.financeclaw.architecture;

import static org.assertj.core.api.Assertions.*;
import static org.mockito.Mockito.*;

import com.financeclaw.app.operations.OperationsQueryService;
import com.financeclaw.app.operations.port.OperationsReadGateway;
import com.financeclaw.domain.IdempotencyKey;
import com.financeclaw.domain.Operations.*;
import com.financeclaw.domain.Task.Owner;
import com.financeclaw.domain.Task.Problem;
import java.util.HashMap;
import java.util.List;
import java.util.Map;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.params.ParameterizedTest;
import org.junit.jupiter.params.provider.ValueSource;

class OperationsRulesTest {
  @Test
  void replayPreservesTheOldReceiptWithoutApplyingItToANewerRevision() {
    var request = new AdmissionChange(new IdempotencyKey("pause"), true, 0);
    var receipt = new Change(request.requestHash(), true, 1);
    assertThat(receipt.replay(request)).isEqualTo(new Receipt(true, 1, true));
    assertThatThrownBy(() -> new Admission(false, 2).advance(request))
        .isInstanceOf(Problem.class)
        .hasMessage("admission_revision_changed");
    assertThatThrownBy(() -> receipt.replay(new AdmissionChange(request.key(), false, 0)))
        .isInstanceOf(Problem.class)
        .hasMessage("idempotency_conflict");
  }

  @ParameterizedTest
  @ValueSource(longs = {-1, Long.MAX_VALUE})
  void revisionCannotUnderflowOrOverflow(long revision) {
    assertThatThrownBy(() -> new AdmissionChange(new IdempotencyKey("pause"), true, revision))
        .isInstanceOf(Problem.class)
        .hasMessage("invalid_revision");
  }

  @Test
  void emptyCountsDoNotMakeAnOpenAdmissionDrainedAndSnapshotsCannotBeMutated() {
    var counts = new HashMap<>(Map.of("queued", 0L, "pending_outbox", 0L));
    assertThat(Drain.observed(new Admission(false, 0), counts).drained()).isFalse();
    var drained = Drain.observed(new Admission(true, 1), counts);
    counts.put("queued", 1L);
    assertThat(drained.drained()).isTrue();
    assertThat(drained.counts()).containsEntry("queued", 0L);
    assertThatThrownBy(() -> drained.counts().put("queued", 2L))
        .isInstanceOf(UnsupportedOperationException.class);
    assertThat(Drain.observed(new Admission(true, 1), counts).drained()).isFalse();
  }

  @ParameterizedTest
  @ValueSource(strings = {"-1", "01", "1.0", "9223372036854775808", "1e2"})
  void invalidNumericCursorNeverReachesPersistence(String cursor) {
    var gateway = mock(OperationsReadGateway.class);
    var queries = new OperationsQueryService(gateway);
    assertThatThrownBy(() -> queries.backlog(new Owner("tenant", "user"), "memory_jobs", cursor, 1))
        .isInstanceOf(Problem.class)
        .hasMessage("invalid_pagination");
    verifyNoInteractions(gateway);
  }

  @Test
  void numericPagingUsesLookaheadWithoutLosingTheLastReturnedId() {
    var gateway = mock(OperationsReadGateway.class);
    var owner = new Owner("tenant", "user");
    when(gateway.memoryJobs(owner, 9, 2))
        .thenReturn(
            List.of(
                new MemoryBacklog("10", "extract", "pending", 1, 0, 0, null),
                new MemoryBacklog("11", "extract", "pending", 1, 0, 0, null)));
    var page = new OperationsQueryService(gateway).backlog(owner, "memory_jobs", "9", 1);
    assertThat(page.nextAfter()).isEqualTo("10");
    assertThat(page.items()).extracting(BacklogItem::id).containsExactly("10");
  }
}
