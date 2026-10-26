package com.financeclaw.agent;

import static org.assertj.core.api.Assertions.*;
import static org.mockito.Mockito.*;

import com.financeclaw.app.agent.AgentResultAccess;
import com.financeclaw.app.agent.port.AgentAccessGateway;
import com.financeclaw.app.agent.port.AgentAccessGateway.Authorization;
import com.financeclaw.app.release.ReleaseService;
import com.financeclaw.domain.Governance.Blocked;
import com.financeclaw.domain.Task.Owner;
import com.financeclaw.domain.agent.AgentExecutionPlan;
import com.financeclaw.domain.agent.ResolvedAgentRelease;
import com.financeclaw.support.PrivateAgentContracts;
import java.util.List;
import org.junit.jupiter.api.Test;

/** Same-call immutable release reuse must not become cached authority or caller impersonation. */
class AgentResultAccessTest {
    private static final Owner CALLER = new Owner("caller-tenant", "caller");
    private static final Owner PUBLISHER = new Owner("publisher-tenant", "publisher");
    private static final String ROOT = "00000000-0000-0000-0000-000000000001";
    private static final String CHILD = "00000000-0000-0000-0000-000000000002";
    private static final String ROOT_HASH = "a".repeat(64);
    private static final String CHILD_HASH = "b".repeat(64);
    private final AgentAccessGateway access = mock(AgentAccessGateway.class);
    private final ReleaseService releases = mock(ReleaseService.class);
    private final ResolvedAgentRelease child = release(CHILD, PUBLISHER, null);
    private final ResolvedAgentRelease root =
            release(
                    ROOT,
                    PUBLISHER,
                    new AgentExecutionPlan(
                            "default",
                            "c".repeat(64),
                            List.of("agent:" + CHILD),
                            List.of(PrivateAgentContracts.binding(CHILD, CHILD_HASH)),
                            null));

    @Test
    void reusesBothResolvedReleasesButRechecksRootAndChildAuthorityOnEveryCall() {
        when(access.requireInvocation(CALLER, ROOT, ROOT_HASH))
                .thenReturn(new Authorization(PUBLISHER, 1, root));
        when(access.requireInvocation(CALLER, CHILD, CHILD_HASH))
                .thenReturn(new Authorization(PUBLISHER, 3, child))
                .thenThrow(new Blocked("agent_access_denied"));

        assertThat(require()).isSameAs(root);
        assertThatThrownBy(this::require).hasMessage("agent_access_denied");

        verify(access, times(2)).requireInvocation(CALLER, ROOT, ROOT_HASH);
        verify(access, times(2)).requireInvocation(CALLER, CHILD, CHILD_HASH);
        verifyNoMoreInteractions(access);
        verifyNoInteractions(releases);
    }

    @Test
    void twoArgumentAuthorizationRetainsExplicitFixtureCatalogResolution() {
        when(access.requireInvocation(CALLER, ROOT, ROOT_HASH))
                .thenReturn(new Authorization(PUBLISHER, 1));
        when(access.requireInvocation(CALLER, CHILD, CHILD_HASH))
                .thenReturn(new Authorization(PUBLISHER, 3));
        when(releases.resolve(ROOT_HASH)).thenReturn(root);
        when(releases.resolve(CHILD_HASH)).thenReturn(child);

        assertThat(require()).isSameAs(root);

        var order = inOrder(access, releases);
        order.verify(access).requireInvocation(CALLER, ROOT, ROOT_HASH);
        order.verify(releases).resolve(ROOT_HASH);
        order.verify(access).requireInvocation(CALLER, CHILD, CHILD_HASH);
        order.verify(releases).resolve(CHILD_HASH);
        verifyNoMoreInteractions(access, releases);
    }

    @Test
    void carriedRootReleaseStillRequiresPublisherIdentityBeforeReadingChildSources() {
        when(access.requireInvocation(CALLER, ROOT, ROOT_HASH))
                .thenReturn(new Authorization(PUBLISHER, 1, release(ROOT, CALLER, null)));

        assertThatThrownBy(this::require).hasMessage("agent_identity_mismatch");

        verify(access).requireInvocation(CALLER, ROOT, ROOT_HASH);
        verifyNoMoreInteractions(access);
        verifyNoInteractions(releases);
    }

    @Test
    void carriedChildReleaseStillRequiresItsOwnPublisherIdentity() {
        when(access.requireInvocation(CALLER, ROOT, ROOT_HASH))
                .thenReturn(new Authorization(PUBLISHER, 1, root));
        when(access.requireInvocation(CALLER, CHILD, CHILD_HASH))
                .thenReturn(new Authorization(PUBLISHER, 3, release(CHILD, CALLER, null)));

        assertThatThrownBy(this::require).hasMessage("agent_identity_mismatch");

        verify(access).requireInvocation(CALLER, ROOT, ROOT_HASH);
        verify(access).requireInvocation(CALLER, CHILD, CHILD_HASH);
        verifyNoMoreInteractions(access);
        verifyNoInteractions(releases);
    }

    private ResolvedAgentRelease require() {
        return AgentResultAccess.require(access, releases, CALLER, ROOT, ROOT_HASH);
    }

    private static ResolvedAgentRelease release(String id, Owner owner, AgentExecutionPlan plan) {
        return new ResolvedAgentRelease(id, "prompt", null, owner.tenant(), owner.subject(), plan);
    }
}
