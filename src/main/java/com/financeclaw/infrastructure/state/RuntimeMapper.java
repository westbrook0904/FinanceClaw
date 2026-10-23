package com.financeclaw.infrastructure.state;

import org.apache.ibatis.annotations.*;

/** Production AgentState CAS and tombstone SQL; Spring owns transaction boundaries. */
@Mapper
public interface RuntimeMapper {
  record StateKey(String namespace, String user, String session, String key) {}

  record StateRow(String payload, long version, boolean deleted) {}

  @Select(
      """
      SELECT payload::text, version, deleted FROM agent_runtime.states
      WHERE namespace=#{namespace} AND user_id=#{user} AND session_id=#{session} AND state_key=#{key}
      """)
  StateRow getState(StateKey id);

  @Insert(
      """
      INSERT INTO agent_runtime.states(namespace,user_id,session_id,state_key,version,payload)
      VALUES (#{id.namespace},#{id.user},#{id.session},#{id.key},1,CAST(#{json} AS jsonb))
      ON CONFLICT DO NOTHING
      """)
  int insertStateIfAbsent(@Param("id") StateKey id, @Param("json") String json);

  @Update(
      """
      UPDATE agent_runtime.states SET payload=CAST(#{json} AS jsonb),version=version+1
      WHERE namespace=#{id.namespace} AND user_id=#{id.user} AND session_id=#{id.session}
        AND state_key=#{id.key} AND version=#{expected} AND NOT deleted
      """)
  int updateStateByExpectedVersion(
      @Param("id") StateKey id, @Param("json") String json, @Param("expected") long expected);

  @Insert(
      """
      INSERT INTO agent_runtime.states(namespace,user_id,session_id,state_key,version,payload,deleted)
      VALUES (#{namespace},#{user},#{session},#{key},1,NULL,TRUE)
      ON CONFLICT(namespace,user_id,session_id,state_key)
      DO UPDATE SET payload=NULL,deleted=TRUE,version=agent_runtime.states.version+1
      """)
  int insertOrUpdateStateTombstone(StateKey id);
}
