package com.financeclaw.infrastructure.state;

import com.financeclaw.domain.ExecutionScope;
import com.financeclaw.infrastructure.state.RuntimeMapper.StateKey;
import io.agentscope.core.state.*;
import io.agentscope.core.util.JsonUtils;
import java.util.List;
import java.util.Optional;
import java.util.Set;

/** Scope-bound AgentScope adapter; MyBatis owns SQL mapping and connection management. */
public class MyBatisAgentStateStore implements AgentStateStore {
  private final RuntimeMapper mapper;
  private final ExecutionScope scope;

  public MyBatisAgentStateStore(RuntimeMapper mapper, ExecutionScope scope) {
    this.mapper = mapper;
    this.scope = scope;
  }

  private StateKey key(String user, String session, String key) {
    if (!scope.userId().equals(user) || !scope.sessionId().equals(session))
      throw new SecurityException("State identity does not match the bound execution scope");
    if (key == null || key.isBlank() || key.length() > 128)
      throw new IllegalArgumentException("Invalid state key");
    return new StateKey(scope.namespace(), user, session, key);
  }

  @Override
  public boolean supportsVersioning() {
    return true;
  }

  @Override
  public <T extends State> VersionedState<T> getVersioned(
      String user, String session, String key, Class<T> type) {
    var row = mapper.findState(key(user, session, key));
    if (row == null) return new VersionedState<>(null, 0);
    // SDK treats absence as a fresh execution, so tombstones must fail before tool dispatch.
    if (row.deleted())
      throw new IllegalStateException("Execution state was deleted; use a new scope");
    return new VersionedState<>(
        JsonUtils.getJsonCodec().fromJson(row.payload(), type), row.version());
  }

  @Override
  public <T extends State> Optional<T> get(String user, String session, String key, Class<T> type) {
    return Optional.ofNullable(getVersioned(user, session, key, type).value());
  }

  @Override
  public long saveIfVersion(String user, String session, String key, State value, long expected) {
    var id = key(user, session, key);
    if (!"agent_state".equals(key) || !(value instanceof AgentState))
      throw new IllegalArgumentException("M0 only stores complete AgentState snapshots");
    if (expected < 0) throw new IllegalArgumentException("Unversioned runtime writes are disabled");
    String json = JsonUtils.getJsonCodec().toJson(value);
    int changed =
        expected == 0
            ? mapper.createState(id, json)
            : mapper.compareAndSetState(id, json, expected);
    return changed == 1 ? expected + 1 : UNVERSIONED;
  }

  @Override
  public void save(String user, String session, String key, State value) {
    throw new UnsupportedOperationException("Use versioned writes and ConflictPolicy.FAIL");
  }

  @Override
  public void save(String user, String session, String key, List<? extends State> values) {
    throw new UnsupportedOperationException(
        "Legacy list state is not a FinanceClaw runtime format");
  }

  @Override
  public <T extends State> List<T> getList(String user, String session, String key, Class<T> type) {
    var row = mapper.findState(key(user, session, key));
    if (row != null && !row.deleted())
      throw new IllegalStateException("Legacy list state requires explicit migration");
    return List.of();
  }

  @Override
  public boolean exists(String user, String session) {
    var row = mapper.findState(key(user, session, "agent_state"));
    return row != null && !row.deleted();
  }

  @Override
  public void delete(String user, String session, String key) {
    mapper.tombstoneState(key(user, session, key));
  }

  @Override
  public void delete(String user, String session) {
    delete(user, session, "agent_state");
  }

  @Override
  public Set<String> listSessionIds(String user) {
    return exists(user, scope.sessionId()) ? Set.of(scope.sessionId()) : Set.of();
  }
}
