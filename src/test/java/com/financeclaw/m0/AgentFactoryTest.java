package com.financeclaw.m0;

import static org.assertj.core.api.Assertions.*;

import com.financeclaw.agent.IsolatedAgentFactory;
import io.agentscope.core.message.ToolResultBlock;
import io.agentscope.core.permission.PermissionContextState;
import io.agentscope.core.permission.PermissionMode;
import io.agentscope.core.tool.*;
import java.util.Map;
import org.junit.jupiter.api.Test;
import reactor.core.publisher.Mono;

class AgentFactoryTest {
  @Test
  void rejectsLegacyToolsAndPoliciesThatBypassThePermissionEngine() {
    var model = ProbeFixtures.scripted("legacy", "");
    var toolkit = new Toolkit();
    toolkit.registerAgentTool(
        new AgentTool() {
          public String getName() {
            return "legacy";
          }

          public String getDescription() {
            return "SDK legacy interface bypasses rule evaluation";
          }

          public Map<String, Object> getParameters() {
            return Map.of("type", "object");
          }

          public Mono<ToolResultBlock> callAsync(ToolCallParam param) {
            throw new AssertionError("Must never execute");
          }
        });
    assertThatThrownBy(
            () ->
                IsolatedAgentFactory.create(
                    model, toolkit, null, ProbeFixtures.permissions("legacy", true)))
        .isInstanceOf(IllegalArgumentException.class)
        .hasMessageContaining("ToolBase");
    assertThatThrownBy(
            () ->
                IsolatedAgentFactory.create(
                    model, new Toolkit(), null, PermissionContextState.builder().build()))
        .isInstanceOf(IllegalArgumentException.class)
        .hasMessageContaining("explicit non-bypass");
    assertThatThrownBy(
            () ->
                IsolatedAgentFactory.create(
                    model,
                    new Toolkit(),
                    null,
                    PermissionContextState.builder().mode(PermissionMode.BYPASS).build()))
        .isInstanceOf(IllegalArgumentException.class)
        .hasMessageContaining("explicit non-bypass");
  }
}
