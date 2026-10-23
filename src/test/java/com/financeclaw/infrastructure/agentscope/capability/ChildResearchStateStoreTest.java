package com.financeclaw.infrastructure.agentscope.capability;

import static org.assertj.core.api.Assertions.*;
import static org.mockito.ArgumentMatchers.*;
import static org.mockito.Mockito.*;

import com.financeclaw.app.execution.ChildInvocationService.Invocation;
import com.financeclaw.app.research.MarketResearchService;
import com.financeclaw.domain.ExecutionScope;
import com.financeclaw.domain.Governance.ExecutionIdentity;
import com.financeclaw.domain.Task.*;
import com.financeclaw.domain.research.ResearchRun;
import com.financeclaw.infrastructure.state.RuntimeMapper;
import io.agentscope.core.state.AgentState;
import java.util.List;
import java.util.Map;
import java.util.function.Consumer;
import org.junit.jupiter.api.Test;

/** 每个 SDK 状态入口都先执行父调用门禁，不能只保护 CAS 写入而遗漏读或删除。 */
class ChildResearchStateStoreTest {
    @Test
    void everySupportedCheckpointOperationChecksParentBeforeTouchingSql() {
        var mapper = mock(RuntimeMapper.class);
        var research = mock(MarketResearchService.class);
        var execution = execution();
        var scope = execution.run().scope();
        when(research.checkpoint(eq(execution), any())).thenThrow(new Fenced());
        var store = new ChildResearchStateStore(mapper, scope, research, execution);
        String user = scope.userId();
        String session = scope.sessionId();
        List<Consumer<ChildResearchStateStore>> operations =
                List.of(
                        s -> s.getVersioned(user, session, "agent_state", AgentState.class),
                        s -> s.get(user, session, "agent_state", AgentState.class),
                        s -> s.getList(user, session, "agent_state", AgentState.class),
                        s -> s.exists(user, session),
                        s -> s.listSessionIds(user),
                        s -> s.saveIfVersion(user, session, "agent_state", null, 0),
                        s -> s.delete(user, session, "agent_state"),
                        s -> s.delete(user, session));
        for (var operation : operations) {
            assertThatThrownBy(() -> operation.accept(store)).isInstanceOf(Fenced.class);
        }
        verifyNoInteractions(mapper);
    }

    @Test
    void scopeFromAnotherInvocationCannotBePairedWithAnAuthorizedParent() {
        var mapper = mock(RuntimeMapper.class);
        var research = mock(MarketResearchService.class);
        var execution = execution();
        var scope = execution.run().scope();
        var wrong =
                new ExecutionScope(
                        scope.tenant(),
                        scope.subject(),
                        "other-child",
                        scope.agent(),
                        scope.release());
        assertThatThrownBy(() -> new ChildResearchStateStore(mapper, wrong, research, execution))
                .isInstanceOf(IllegalArgumentException.class);
        verifyNoInteractions(mapper, research);
    }

    private static MarketResearchService.Execution execution() {
        var claim =
                new Claim(
                        new Command("command", "turn", "running", "attempt", 1, true, null),
                        new Turn(
                                "turn",
                                "conversation",
                                "tenant",
                                "alice",
                                "key",
                                "hash",
                                "request",
                                "release",
                                "running",
                                1,
                                null,
                                null));
        var identity =
                new ExecutionIdentity(
                        "tenant",
                        "alice",
                        "conversation",
                        "turn",
                        "attempt",
                        "release",
                        1,
                        List.of("market:read"),
                        "2026-10-23T15:00:00Z");
        var run = new ResearchRun(claim.scope(), "child", identity, List.of("AAPL"));
        return new MarketResearchService.Execution(
                claim,
                "child:child",
                Map.of("symbols", List.of("AAPL")),
                run,
                null,
                new Invocation(claim, "child", "a".repeat(64), null, List.of()));
    }
}
