package com.financeclaw.m0;

import com.financeclaw.infrastructure.state.RuntimeMapper.StateKey;
import org.apache.ibatis.annotations.*;

/** Test-only external system; duplicate operations intentionally remain observable. */
public interface ProbeMapper {
  @Update(
      "CREATE TABLE IF NOT EXISTS agent_runtime.probe_external_effects (id BIGSERIAL PRIMARY KEY, scope_key TEXT NOT NULL, operation_id TEXT NOT NULL)")
  void createExternalSystem();

  @Insert(
      "INSERT INTO agent_runtime.probe_external_effects(scope_key,operation_id) VALUES (#{scope},#{operation})")
  void applyEffect(@Param("scope") String scope, @Param("operation") String operation);

  @Select("SELECT count(*) FROM agent_runtime.probe_external_effects WHERE scope_key=#{scope}")
  int countEffects(String scope);

  @Select("SELECT version()")
  String serverVersion();

  @Insert(
      """
      INSERT INTO agent_runtime.states VALUES (#{namespace},#{user},#{session},'agent_state',1,'{"context":42}'::jsonb,FALSE)
      """)
  void insertCorruptState(StateKey scope);
}
