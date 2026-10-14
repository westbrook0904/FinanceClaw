package com.financeclaw.infrastructure.state;

import org.apache.ibatis.annotations.*;

/** SQL contracts for AgentState, tool receipts, and M0 execution admission. */
@Mapper
public interface RuntimeMapper {
  record StateKey(String namespace, String user, String session, String key) {}

  record StateRow(String payload, long version, boolean deleted) {}

  record EffectRow(String requestHash, String status, String resultJson) {}

  @Select(
      """
      SELECT payload::text, version, deleted FROM agent_runtime.states
      WHERE namespace=#{namespace} AND user_id=#{user} AND session_id=#{session} AND state_key=#{key}
      """)
  StateRow findState(StateKey id);

  @Insert(
      """
      INSERT INTO agent_runtime.states(namespace,user_id,session_id,state_key,version,payload)
      VALUES (#{id.namespace},#{id.user},#{id.session},#{id.key},1,CAST(#{json} AS jsonb))
      ON CONFLICT DO NOTHING
      """)
  int createState(@Param("id") StateKey id, @Param("json") String json);

  @Update(
      """
      UPDATE agent_runtime.states SET payload=CAST(#{json} AS jsonb),version=version+1
      WHERE namespace=#{id.namespace} AND user_id=#{id.user} AND session_id=#{id.session}
        AND state_key=#{id.key} AND version=#{expected} AND NOT deleted
      """)
  int compareAndSetState(
      @Param("id") StateKey id, @Param("json") String json, @Param("expected") long expected);

  @Insert(
      """
      INSERT INTO agent_runtime.states(namespace,user_id,session_id,state_key,version,payload,deleted)
      VALUES (#{namespace},#{user},#{session},#{key},1,NULL,TRUE)
      ON CONFLICT(namespace,user_id,session_id,state_key)
      DO UPDATE SET payload=NULL,deleted=TRUE,version=agent_runtime.states.version+1
      """)
  int tombstoneState(StateKey id);

  @Insert(
      """
      INSERT INTO agent_runtime.tool_effects(scope_key,operation_id,request_hash,status)
      VALUES (#{scope},#{operation},#{hash},'pending') ON CONFLICT DO NOTHING
      """)
  int beginEffect(
      @Param("scope") String scope,
      @Param("operation") String operation,
      @Param("hash") String hash);

  @Select(
      """
      SELECT request_hash,status,result_json::text FROM agent_runtime.tool_effects
      WHERE scope_key=#{scope} AND operation_id=#{operation}
      """)
  EffectRow findEffect(@Param("scope") String scope, @Param("operation") String operation);

  @Update(
      """
      UPDATE agent_runtime.tool_effects SET status='completed',result_json=CAST(#{json} AS jsonb)
      WHERE scope_key=#{scope} AND operation_id=#{operation} AND request_hash=#{hash} AND status='pending'
      """)
  int completeEffect(
      @Param("scope") String scope,
      @Param("operation") String operation,
      @Param("hash") String hash,
      @Param("json") String json);

  @Select(
      "SELECT EXISTS(SELECT 1 FROM agent_runtime.tool_effects WHERE scope_key=#{scope} AND status='pending')")
  boolean hasPendingEffect(String scope);

  @Select("SELECT pg_try_advisory_lock(#{key})")
  @Options(useCache = false, flushCache = Options.FlushCachePolicy.TRUE)
  boolean tryLock(long key);

  @Select("SELECT pg_advisory_unlock(#{key})")
  @Options(useCache = false, flushCache = Options.FlushCachePolicy.TRUE)
  boolean unlock(long key);
}
