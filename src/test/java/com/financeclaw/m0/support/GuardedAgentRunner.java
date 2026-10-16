package com.financeclaw.m0.support;

import com.financeclaw.domain.ExecutionScope;
import com.financeclaw.infrastructure.agentscope.IsolatedAgentFactory;
import com.financeclaw.infrastructure.state.MyBatisAgentStateStore;
import com.financeclaw.infrastructure.state.RuntimeMapper;
import io.agentscope.core.message.Msg;
import io.agentscope.core.model.Model;
import io.agentscope.core.permission.PermissionContextState;
import io.agentscope.core.tool.Toolkit;
import java.time.Duration;
import java.util.List;
import java.util.function.Supplier;
import org.mybatis.spring.SqlSessionTemplate;

/** Blocking M0 probe entry; call only from a worker, never from a Reactor event loop. */
public final class GuardedAgentRunner {
  private final SqlSessionTemplate database;
  private final ExecutionScope scope;

  public GuardedAgentRunner(SqlSessionTemplate database, ExecutionScope scope) {
    this.database = database;
    this.scope = scope;
  }

  public Msg run(
      Model model,
      Toolkit toolkit,
      PermissionContextState permissions,
      Supplier<List<Msg>> inputs) {
    try (ExecutionGate gate = ExecutionGate.acquire(database.getSqlSessionFactory(), scope)) {
      EffectLedger effects = new EffectLedger(database.getMapper(M0RuntimeMapper.class), scope);
      effects.assertNoUncertainEffects();
      var store = new MyBatisAgentStateStore(database.getMapper(RuntimeMapper.class), scope);
      // Resolve interaction inputs under the same gate as execution, avoiding version TOCTOU.
      List<Msg> messages = inputs.get();
      try (var agent = IsolatedAgentFactory.create(model, toolkit, store, permissions)) {
        Msg result =
            agent.call(messages, IsolatedAgentFactory.context(scope)).block(Duration.ofSeconds(20));
        effects.assertNoUncertainEffects();
        if (result == null) throw new IllegalStateException("Agent returned no result");
        return result;
      }
    }
  }
}
