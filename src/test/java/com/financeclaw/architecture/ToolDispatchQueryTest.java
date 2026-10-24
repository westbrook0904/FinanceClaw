package com.financeclaw.architecture;

import static org.junit.jupiter.api.Assertions.*;
import static org.mockito.Mockito.*;

import com.financeclaw.app.execution.ExecutionLeaseService;
import com.financeclaw.app.execution.ExecutionPolicyService;
import com.financeclaw.app.execution.port.*;
import com.financeclaw.app.market.MarketDataService;
import com.financeclaw.app.market.port.MarketCatalog;
import com.financeclaw.app.task.TaskEventHub;
import com.financeclaw.app.task.port.TaskQueryGateway;
import com.financeclaw.domain.ExecutionPolicy;
import com.financeclaw.domain.Governance.*;
import com.financeclaw.domain.Task.*;
import java.util.List;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.params.ParameterizedTest;
import org.junit.jupiter.params.provider.ValueSource;
import org.springframework.transaction.PlatformTransactionManager;
import org.springframework.transaction.support.SimpleTransactionStatus;
import org.springframework.transaction.support.TransactionTemplate;

class ToolDispatchQueryTest {
    private static final Command COMMAND =
            new Command("command", "turn", "running", "attempt", 1, true, null);
    private static final Turn TURN =
            new Turn(
                    "turn",
                    "conversation",
                    "tenant",
                    "alice",
                    "key",
                    "hash",
                    "message",
                    "release",
                    "running",
                    1,
                    null,
                    null);
    private static final Claim CLAIM = new Claim(COMMAND, TURN);
    private final TaskQueryGateway reads = mock(TaskQueryGateway.class);
    private final ExecutionPolicyGateway policies = mock(ExecutionPolicyGateway.class);
    private final CommandGateway commands = mock(CommandGateway.class);
    private final ExecutionMemory memory = mock(ExecutionMemory.class);
    private final ExecutionRelease releases = mock(ExecutionRelease.class);
    private final TaskEventHub events = mock(TaskEventHub.class);
    private final PlatformTransactionManager manager = mock(PlatformTransactionManager.class);
    private ExecutionPolicyService service;

    @BeforeEach
    void setup() {
        when(manager.getTransaction(any())).thenAnswer(ignored -> new SimpleTransactionStatus());
        var leases =
                new ExecutionLeaseService(
                        commands, null, null, null, new TransactionTemplate(manager));
        service = new ExecutionPolicyService(reads, events, policies, releases, memory, leases);
        when(commands.lockCommand("command")).thenReturn(COMMAND);
        when(reads.turn("turn")).thenReturn(TURN);
        when(policies.policy("turn")).thenReturn(new Policy("turn", 4, "[\"catalog:read\"]", 0, 0));
        when(policies.lockGrant("conversation"))
                .thenReturn(new Grant("conversation", 4, "[\"catalog:read\"]"));
        when(releases.requireForTurn(TURN))
                .thenReturn(new ExecutionPolicy(List.of("catalog:read"), 10, 20));
        when(policies.toolCall("turn", 20)).thenReturn(1);
    }

    @Test
    void scopeAndBudgetUseTheSameSingleFencedPolicyRead() {
        service.consumeTool(CLAIM, "catalog:read");

        var order = inOrder(commands, reads, memory, policies);
        order.verify(commands).lockCommand("command");
        order.verify(reads).turn("turn");
        order.verify(memory).check(TURN);
        order.verify(policies).policy("turn");
        order.verify(policies).lockGrant("conversation");
        order.verify(policies).toolCall("turn", 20);
        order.verifyNoMoreInteractions();
        verify(manager).commit(any());
    }

    @Test
    void missingScopeDoesNotConsumeBudget() {
        assertEquals(
                "tool_denied",
                assertThrows(Blocked.class, () -> service.consumeTool(CLAIM, "report:write"))
                        .getMessage());
        verify(policies, never()).toolCall(anyString(), anyInt());
        verify(manager).rollback(any());
    }

    @Test
    void revokedGrantIsRecheckedAtEveryToolDispatch() {
        service.consumeTool(CLAIM, "catalog:read");
        when(policies.lockGrant("conversation")).thenReturn(new Grant("conversation", 5, "[]"));

        assertEquals(
                "authorization_changed",
                assertThrows(Blocked.class, () -> service.consumeTool(CLAIM, "catalog:read"))
                        .getMessage());

        verify(policies, times(2)).policy("turn");
        verify(policies, times(2)).lockGrant("conversation");
        verify(policies, times(1)).toolCall("turn", 20);
    }

    @Test
    void expiredLeaseCannotReadPolicyOrSpendBudget() {
        when(commands.lockCommand("command"))
                .thenReturn(new Command("command", "turn", "running", "attempt", 1, false, null));
        assertThrows(Fenced.class, () -> service.consumeTool(CLAIM, "catalog:read"));
        verifyNoInteractions(reads, memory, policies);
    }

    @Test
    void privacyChangeStillBlocksBeforeBudgetConsumption() {
        doThrow(new Blocked("memory_privacy_changed")).when(memory).check(TURN);
        assertEquals(
                "memory_privacy_changed",
                assertThrows(Blocked.class, () -> service.consumeTool(CLAIM, "catalog:read"))
                        .getMessage());
        verifyNoInteractions(policies);
        verify(policies, never()).toolCall(anyString(), anyInt());
    }

    @Test
    void exhaustedBudgetRemainsAnAtomicRejection() {
        when(policies.toolCall("turn", 20)).thenReturn(0);
        assertEquals(
                "execution_budget_exhausted",
                assertThrows(Blocked.class, () -> service.consumeTool(CLAIM, "catalog:read"))
                        .getMessage());
        verify(manager).rollback(any());
    }

    @Test
    void mismatchedAgentReleaseBlocksBeforePrivacyReadsOrBudgetConsumption() {
        when(releases.requireForTurn(TURN)).thenThrow(new Blocked("agent_identity_mismatch"));

        assertEquals(
                "agent_identity_mismatch",
                assertThrows(Blocked.class, () -> service.consumeTool(CLAIM, "catalog:read"))
                        .getMessage());

        verifyNoInteractions(memory, policies);
        verify(releases, never()).require(anyString());
        verify(manager).rollback(any());
    }

    @Test
    void frozenReleaseModelBudgetRemainsAuthoritativeForEachReservation() {
        when(releases.requireForTurn(TURN))
                .thenReturn(new ExecutionPolicy(List.of("catalog:read"), 1, 20));
        when(policies.modelCall("turn", 1)).thenReturn(1, 0);

        service.consume(CLAIM, true);
        assertEquals(
                "execution_budget_exhausted",
                assertThrows(Blocked.class, () -> service.consume(CLAIM, true)).getMessage());

        verify(policies, times(2)).modelCall("turn", 1);
        verify(releases, times(2)).requireForTurn(TURN);
        verify(releases, never()).require(anyString());
        verify(releases, never()).currentHash();
        verify(manager).rollback(any());
    }

    @Test
    void marketReadUsesOneBudgetReservationAndOnePolicyRead() {
        when(policies.policy("turn")).thenReturn(new Policy("turn", 4, "[\"market:read\"]", 0, 0));
        var catalog = mock(MarketCatalog.class);
        when(catalog.snapshots("release"))
                .thenReturn(
                        "{\"AAPL\":{\"price\":\"200.00\",\"currency\":\"USD\",\"provider\":\"fixture\",\"as_of\":\"2026-10-22T00:00:00Z\"}}");

        var result = new MarketDataService(service, catalog).read(CLAIM, "aapl");

        assertEquals("AAPL", result.get("symbol"));
        verify(policies).policy("turn");
        verify(policies).lockGrant("conversation");
        verify(policies).toolCall("turn", 20);
    }

    @Test
    void deniedMarketReadPreservesItsErrorAndCannotReadCatalogOrSpendBudget() {
        var catalog = mock(MarketCatalog.class);

        assertEquals(
                "market_denied",
                assertThrows(
                                Blocked.class,
                                () -> new MarketDataService(service, catalog).read(CLAIM, "AAPL"))
                        .getMessage());

        verifyNoInteractions(catalog);
        verify(policies, never()).toolCall(anyString(), anyInt());
    }

    @Test
    void liveProgressDoesNotReadDatabaseOrOpenATransaction() {
        service.progress(CLAIM, "text_delta", "hello", null);

        verify(events).progress(CLAIM, "text_delta", "hello", null);
        verifyNoInteractions(commands, reads, policies, memory, releases, manager);
    }

    @ParameterizedTest
    @ValueSource(
            strings = {
                "authorization_changed",
                "memory_privacy_changed",
                "cancelling",
                "expired",
                "closed"
            })
    void rejectedProgressChecksAuthorityOnceAndPreservesItsReason(String reason) {
        var invalidated = new Fenced();
        doThrow(invalidated).when(events).progress(CLAIM, "text_delta", "late", null);
        switch (reason) {
            case "authorization_changed" ->
                    when(policies.lockGrant("conversation"))
                            .thenReturn(new Grant("conversation", 5, "[]"));
            case "memory_privacy_changed" -> doThrow(new Blocked(reason)).when(memory).check(TURN);
            case "cancelling" ->
                    when(reads.turn("turn"))
                            .thenReturn(
                                    new Turn(
                                            TURN.id(),
                                            TURN.conversationId(),
                                            TURN.tenant(),
                                            TURN.subject(),
                                            TURN.idempotencyKey(),
                                            TURN.requestHash(),
                                            TURN.message(),
                                            TURN.releaseHash(),
                                            "cancelling",
                                            TURN.revision(),
                                            null,
                                            null));
            case "expired" ->
                    when(commands.lockCommand("command"))
                            .thenReturn(
                                    new Command(
                                            "command", "turn", "running", "attempt", 1, false,
                                            null));
            default -> {
                // A normally closed local handle must stay fenced even if SQL still allows
                // dispatch.
            }
        }

        var checked = spy(service);
        if (reason.equals("authorization_changed") || reason.equals("memory_privacy_changed")) {
            assertEquals(
                    reason,
                    assertThrows(
                                    Blocked.class,
                                    () -> checked.progress(CLAIM, "text_delta", "late", null))
                            .getMessage());
        } else {
            var failure =
                    assertThrows(
                            Fenced.class,
                            () -> checked.progress(CLAIM, "text_delta", "late", null));
            if (reason.equals("closed")) assertSame(invalidated, failure);
        }

        verify(checked).checkDispatch(CLAIM);
        verify(commands).lockCommand("command");
        verify(manager).getTransaction(any());
        verify(events).progress(CLAIM, "text_delta", "late", null);
        verify(policies, never()).toolCall(anyString(), anyInt());
        verify(policies, never()).modelCall(anyString(), anyInt());
    }
}
