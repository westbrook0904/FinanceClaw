package com.financeclaw.runtime;

import static org.assertj.core.api.Assertions.*;
import static org.mockito.Mockito.*;

import com.financeclaw.api.*;
import com.financeclaw.app.agent.port.AgentAccessGateway;
import com.financeclaw.app.execution.port.*;
import com.financeclaw.app.runtime.*;
import com.financeclaw.app.runtime.port.*;
import com.financeclaw.app.task.*;
import com.financeclaw.app.task.port.*;
import com.financeclaw.domain.Governance.*;
import com.financeclaw.domain.Task.*;
import com.financeclaw.domain.runtime.RuntimeWorker;
import com.financeclaw.infrastructure.runtime.ObservedAgentExecution;
import io.micrometer.core.instrument.simple.SimpleMeterRegistry;
import java.util.*;
import org.junit.jupiter.api.Test;
import org.springframework.mock.web.MockHttpServletRequest;
import org.springframework.transaction.PlatformTransactionManager;
import org.springframework.transaction.support.*;

class RuntimeCoordinationTest {
    private final Owner caller = new Owner("tenant", "caller");
    private final Turn turn =
            new Turn(
                    "turn",
                    "conversation",
                    "tenant",
                    "caller",
                    "key",
                    "hash",
                    "private-input",
                    "a".repeat(64),
                    "running",
                    2,
                    null,
                    null);

    @Test
    void currentTaskOwnerGrantPrivacyAndSharedAgentAccessAllGateDelivery() {
        var reads = mock(TaskQueryGateway.class);
        var governance = mock(ExecutionPolicyGateway.class);
        var memory = mock(ExecutionMemory.class);
        var agents = mock(AgentAccessGateway.class);
        var manager = mock(PlatformTransactionManager.class);
        when(manager.getTransaction(any())).thenReturn(new SimpleTransactionStatus());
        var access =
                new LiveTaskEventAccess(
                        reads, governance, memory, new TransactionTemplate(manager), agents);
        when(reads.ownedTurn(caller, "conversation", "turn")).thenReturn(turn);
        when(governance.policy("turn")).thenReturn(new Policy("turn", 3, "[]", 0, 0));
        when(governance.lockGrant("conversation")).thenReturn(new Grant("conversation", 3, "[]"));
        when(agents.requireInvocation(caller, turn.agentId(), turn.releaseHash()))
                .thenReturn(
                        new AgentAccessGateway.Authorization(new Owner("publisher", "owner"), 9));
        access.check(turn);
        verify(memory).check(turn);
        verify(agents).requireInvocation(caller, turn.agentId(), turn.releaseHash());
        verify(reads).ownedTurn(caller, "conversation", "turn");
        when(governance.lockGrant("conversation")).thenReturn(new Grant("conversation", 4, "[]"));
        assertThatThrownBy(() -> access.check(turn)).hasMessage("authorization_changed");
        doThrow(new Blocked("memory_privacy_changed")).when(memory).check(turn);
        assertThatThrownBy(() -> access.check(turn)).hasMessage("memory_privacy_changed");
        doNothing().when(memory).check(turn);
        when(agents.requireInvocation(caller, turn.agentId(), turn.releaseHash()))
                .thenThrow(new Blocked("agent_access_denied"));
        assertThatThrownBy(() -> access.check(turn)).hasMessage("agent_access_denied");
        when(reads.ownedTurn(caller, "conversation", "turn")).thenReturn(null);
        assertThatThrownBy(() -> access.check(turn)).hasMessage("not_found");
    }

    @Test
    void eligibilityRequiresLiveMatchingProtocolRuntimeAndCapability() {
        var db = mock(WorkerRegistryGateway.class);
        var registry = new WorkerRegistry(db, "a".repeat(64), 15000);
        String id = UUID.randomUUID().toString();
        var healthy =
                new RuntimeWorker(
                        id, 1, "a".repeat(64), List.of("agentscope"), 4, 0, "active", true);
        when(db.get(id)).thenReturn(healthy);
        when(db.heartbeat(id, "a".repeat(64), 0, 15000)).thenReturn(true);
        registry.register(id, 4);
        verify(db).register(id, "a".repeat(64), 4, 15000);
        assertThat(registry.requireEligible(id, "agentscope")).isEqualTo(healthy);
        for (var invalid :
                List.of(
                        new RuntimeWorker(
                                id,
                                2,
                                healthy.runtimeHash(),
                                healthy.capabilities(),
                                4,
                                0,
                                "active",
                                true),
                        new RuntimeWorker(
                                id,
                                1,
                                "b".repeat(64),
                                healthy.capabilities(),
                                4,
                                0,
                                "active",
                                true),
                        new RuntimeWorker(
                                id, 1, healthy.runtimeHash(), List.of(), 4, 0, "active", true),
                        new RuntimeWorker(
                                id,
                                1,
                                healthy.runtimeHash(),
                                healthy.capabilities(),
                                4,
                                0,
                                "stopped",
                                true),
                        new RuntimeWorker(
                                id,
                                1,
                                healthy.runtimeHash(),
                                healthy.capabilities(),
                                4,
                                0,
                                "active",
                                false))) {
            when(db.get(id)).thenReturn(invalid);
            assertThatThrownBy(() -> registry.requireEligible(id, "agentscope"))
                    .hasMessage("worker_unavailable");
        }
    }

    @Test
    void invocationObservationForwardsTheSameLocalAttemptGate() {
        var delegate = mock(AgentExecutionPort.class);
        var metrics = new SimpleMeterRegistry();
        try {
            var observed = new ObservedAgentExecution(delegate, metrics);
            var claim =
                    new Claim(
                            new Command("command", "turn", "running", "attempt", 1, true, null),
                            turn);
            java.util.function.BooleanSupplier allowed = () -> false;
            when(delegate.execute(eq(claim), same(allowed))).thenThrow(new Fenced());
            assertThatThrownBy(() -> observed.execute(claim, allowed)).isInstanceOf(Fenced.class);
            verify(delegate).execute(eq(claim), same(allowed));
            verify(delegate, never()).execute(any());
            assertThat(
                            metrics.get("financeclaw.runtime.invocation")
                                    .tag("outcome", "fenced")
                                    .timer()
                                    .count())
                    .isOne();
        } finally {
            metrics.close();
        }
    }

    @Test
    void invocationMetersUseOnlyFixedOutcomeValuesAndDoNotClaimBusinessCommit() {
        var delegate = mock(AgentExecutionPort.class);
        var metrics = new SimpleMeterRegistry();
        try {
            var observed = new ObservedAgentExecution(delegate, metrics);
            var claim =
                    new Claim(
                            new Command("command", "turn", "running", "attempt", 1, true, null),
                            turn);
            var result = new Outcome("completed", "sensitive-result", 1);
            when(delegate.execute(claim))
                    .thenReturn(result)
                    .thenThrow(
                            new Blocked("secret-code"),
                            new Fenced(),
                            new IllegalStateException("secret-detail"));
            assertThat(observed.execute(claim)).isEqualTo(result);
            for (int i = 0; i < 3; i++)
                assertThatThrownBy(() -> observed.execute(claim))
                        .isInstanceOf(RuntimeException.class);
            observed.interrupt(claim);
            verify(delegate).interrupt(claim);
            assertThat(metrics.getMeters())
                    .hasSize(4)
                    .allSatisfy(
                            meter -> {
                                assertThat(meter.getId().getTags()).hasSize(1);
                                assertThat(meter.getId().getTags().getFirst().getKey())
                                        .isEqualTo("outcome");
                                assertThat(meter.getId().getTags().getFirst().getValue())
                                        .isIn("returned", "blocked", "fenced", "error");
                                assertThat(
                                                metrics.get(meter.getId().getName())
                                                        .tag(
                                                                "outcome",
                                                                meter.getId().getTag("outcome"))
                                                        .timer()
                                                        .count())
                                        .isOne();
                            });
        } finally {
            metrics.close();
        }
    }

    @Test
    void runtimeOperationsRequireExplicitPermissionEvenForResourceOwners() {
        var catalog = mock(RuntimeArtifactCatalog.class);
        var workers = mock(WorkerRegistry.class);
        var usage = mock(RuntimeUsageGateway.class);
        var api = new RuntimeOperationsController(catalog, workers, usage);
        var request = new MockHttpServletRequest();
        request.setAttribute(BearerIdentity.ATTRIBUTE, caller);
        request.setAttribute(
                BearerIdentity.PRINCIPAL_ATTRIBUTE,
                new TrustedIdentityDirectory.Principal(caller, Set.of()));
        assertThatThrownBy(() -> api.workers(request, 50)).hasMessage("forbidden");
        assertThatThrownBy(() -> api.artifacts(request, 50)).hasMessage("forbidden");
        assertThatThrownBy(() -> api.usage(request)).hasMessage("forbidden");
        assertThatThrownBy(() -> api.verify(request, "a".repeat(64))).hasMessage("forbidden");
        assertThatThrownBy(
                        () ->
                                api.register(
                                        request,
                                        new RuntimeOperationsController.RegisterArtifact(
                                                "a.jar", "a".repeat(64), 1, List.of("agentscope"))))
                .hasMessage("forbidden");
        verifyNoInteractions(catalog, workers, usage);
        request.setAttribute(
                BearerIdentity.PRINCIPAL_ATTRIBUTE,
                new TrustedIdentityDirectory.Principal(caller, Set.of("runtime:manage")));
        api.workers(request, 50);
        api.usage(request);
        verify(workers).list(50);
        verify(usage).snapshot();
    }

    @Test
    void productionSseRequiresPersistentNotificationSignals() {
        assertThatThrownBy(
                        () ->
                                new TaskEvents(
                                        mock(TaskQueryService.class),
                                        new TaskEventHub(),
                                        1,
                                        5000,
                                        1000,
                                        16,
                                        t -> {},
                                        null))
                .isInstanceOf(NullPointerException.class);
    }
}
