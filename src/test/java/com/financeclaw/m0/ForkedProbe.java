package com.financeclaw.m0;

import com.fasterxml.jackson.databind.ObjectMapper;
import com.financeclaw.domain.ExecutionScope;
import com.financeclaw.infrastructure.agentscope.ResumeInputs;
import com.financeclaw.infrastructure.state.MyBatisAgentStateStore;
import com.financeclaw.infrastructure.state.RuntimeMapper;
import com.financeclaw.m0.support.GuardedAgentRunner;
import io.agentscope.core.message.Msg;
import io.agentscope.core.message.UserMessage;
import io.agentscope.core.state.AgentState;
import java.nio.file.Path;
import java.util.List;

/** A real child JVM; halt modes deliberately bypass shutdown hooks and final state saves. */
public final class ForkedProbe {
    public record Config(
            String url,
            String user,
            String password,
            ExecutionScope scope,
            String mode,
            String crash) {}

    public static void main(String[] args) throws Exception {
        Config config = new ObjectMapper().readValue(Path.of(args[0]).toFile(), Config.class);
        var source =
                new org.springframework.jdbc.datasource.DriverManagerDataSource(
                        config.url(), config.user(), config.password());
        var database = ProbeFixtures.database(source);
        var scope = config.scope();
        String tool = config.mode().startsWith("clarify") ? "request_city" : "write_report";
        boolean ask = config.mode().startsWith("approval");
        var runner = new GuardedAgentRunner(database, scope);
        Msg result =
                runner.run(
                        ProbeFixtures.scripted(tool, config.crash()),
                        ProbeFixtures.toolkit(database, scope, tool, config.crash()),
                        ProbeFixtures.permissions(tool, ask),
                        () -> {
                            if (config.mode().endsWith("resume")
                                    || config.mode().endsWith("deny")) {
                                var store =
                                        new MyBatisAgentStateStore(
                                                database.getMapper(RuntimeMapper.class), scope);
                                var state =
                                        store.getVersioned(
                                                scope.userId(),
                                                scope.sessionId(),
                                                "agent_state",
                                                AgentState.class);
                                var pending =
                                        ResumeInputs.pending(
                                                store,
                                                scope,
                                                state.version(),
                                                ProbeFixtures.CALL_ID);
                                if (ask)
                                    return List.of(
                                            ResumeInputs.approval(
                                                    store,
                                                    scope,
                                                    state.version(),
                                                    pending.getId(),
                                                    ResumeInputs.actionHash(pending),
                                                    !config.mode().endsWith("deny")));
                                return List.of(
                                        ResumeInputs.clarification(
                                                store,
                                                scope,
                                                state.version(),
                                                pending.getId(),
                                                ResumeInputs.actionHash(pending),
                                                "Shanghai"));
                            }
                            return List.of(new UserMessage("user", "Please use " + tool));
                        });
        if ("after-save".equals(config.crash())) Runtime.getRuntime().halt(74);
        System.out.println(
                "PROBE_RESULT " + result.getGenerateReason() + " " + result.getTextContent());
    }
}
