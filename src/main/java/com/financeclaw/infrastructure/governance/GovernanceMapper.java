package com.financeclaw.infrastructure.governance;

import com.financeclaw.domain.Governance.*;
import java.util.List;
import org.apache.ibatis.annotations.*;

@Mapper
public interface GovernanceMapper {
  record Step(String inputHash, String result) {}

  @Update("UPDATE business.artifacts SET required_scope=#{scope} WHERE id=#{id}")
  int setArtifactScope(@Param("id") String id, @Param("scope") String scope);

  @Select("SELECT required_scope FROM business.artifacts WHERE id=#{id}")
  String artifactScope(String id);

  @Select(
      "SELECT input_hash,result FROM business.capability_steps WHERE turn_id=#{turn} AND namespace=#{namespace} AND step=#{step}")
  Step step(
      @Param("turn") String turn, @Param("namespace") String namespace, @Param("step") String step);

  @Insert(
      "INSERT INTO business.capability_steps(turn_id,namespace,step,input_hash,result) VALUES(#{turn},#{namespace},#{step},#{hash},#{result})")
  int saveStep(
      @Param("turn") String turn,
      @Param("namespace") String namespace,
      @Param("step") String step,
      @Param("hash") String hash,
      @Param("result") String result);

  @Insert(
      "INSERT INTO business.grants(conversation_id,scopes) VALUES(#{id},CAST(#{scopes} AS jsonb)) ON CONFLICT DO NOTHING")
  int ensureGrant(@Param("id") String id, @Param("scopes") String scopes);

  @Select(
      "SELECT conversation_id,revision,scopes::text FROM business.grants WHERE conversation_id=#{id} FOR UPDATE")
  Grant lockGrant(String id);

  @Select(
      "SELECT conversation_id,revision,scopes::text FROM business.grants WHERE conversation_id=#{id}")
  Grant grant(String id);

  @Update(
      "UPDATE business.grants SET revision=revision+1,scopes=CAST(#{scopes} AS jsonb) WHERE conversation_id=#{id}")
  int updateGrant(@Param("id") String id, @Param("scopes") String scopes);

  @Select(
      "SELECT request_hash FROM business.grant_changes WHERE conversation_id=#{id} AND idempotency_key=#{key}")
  String grantRequest(@Param("id") String id, @Param("key") String key);

  @Select(
      "SELECT conversation_id,revision,scopes::text FROM business.grant_changes WHERE conversation_id=#{id} AND idempotency_key=#{key}")
  Grant grantReplay(@Param("id") String id, @Param("key") String key);

  @Insert(
      "INSERT INTO business.grant_changes(conversation_id,idempotency_key,request_hash,revision,scopes) SELECT conversation_id,#{key},#{hash},revision,scopes FROM business.grants WHERE conversation_id=#{id}")
  int grantChange(@Param("id") String id, @Param("key") String key, @Param("hash") String hash);

  @Insert(
      "INSERT INTO business.turn_policies(turn_id,grant_revision,scopes) VALUES(#{turn},#{revision},CAST(#{scopes} AS jsonb))")
  int insertPolicy(
      @Param("turn") String turn, @Param("revision") long revision, @Param("scopes") String scopes);

  @Select(
      "SELECT turn_id,grant_revision,scopes::text,model_calls,tool_calls FROM business.turn_policies WHERE turn_id=#{turn}")
  Policy policy(String turn);

  @Update(
      "UPDATE business.turn_policies SET model_calls=model_calls+1 WHERE turn_id=#{id} AND model_calls<#{max}")
  int modelCall(@Param("id") String id, @Param("max") int max);

  @Update(
      "UPDATE business.turn_policies SET tool_calls=tool_calls+1 WHERE turn_id=#{id} AND tool_calls<#{max}")
  int toolCall(@Param("id") String id, @Param("max") int max);

  @Insert(
      """
    INSERT INTO business.interactions(id,turn_id,command_id,attempt_id,state_version,grant_revision,release_hash,kind,call_id,tool,action_hash,schema_json,question)
    VALUES(#{id},#{turnId},#{commandId},#{attemptId},#{stateVersion},#{grantRevision},#{releaseHash},#{kind},#{callId},#{tool},#{actionHash},#{schema},#{question})
    """)
  int insertInteraction(Interaction i);

  String INTERACTION_COLUMNS =
      "id,turn_id,command_id,attempt_id,state_version,grant_revision,release_hash,kind,call_id,tool,action_hash,schema_json AS schema,question,revision,status,expires_at>clock_timestamp() AS live,response_hash,response_json AS response";

  @Select("SELECT " + INTERACTION_COLUMNS + " FROM business.interactions WHERE id=#{id}")
  Interaction interaction(String id);

  @Select("SELECT " + INTERACTION_COLUMNS + " FROM business.interactions WHERE id=#{id} FOR UPDATE")
  Interaction lockInteraction(String id);

  @Select(
      "SELECT "
          + INTERACTION_COLUMNS
          + " FROM business.interactions WHERE turn_id=#{id} ORDER BY revision,id")
  List<Interaction> interactions(String id);

  @Update(
      "UPDATE business.interactions SET status='answered',response_hash=#{hash},response_json=#{json} WHERE id=#{id} AND status='pending'")
  int answer(@Param("id") String id, @Param("hash") String hash, @Param("json") String json);

  @Update(
      "UPDATE business.interactions SET status='invalid' WHERE turn_id=#{id} AND status='pending'")
  int invalidate(String id);

  @Update(
      "UPDATE business.commands SET status='queued',resume_id=#{interaction},attempt_id=NULL,lease_until=NULL WHERE id=#{command} AND status='done'")
  int resume(@Param("command") String command, @Param("interaction") String interaction);

  @Insert(
      "INSERT INTO business.artifacts(id,turn_id,conversation_id,tenant,subject,release_hash,hash,payload,bytes) VALUES(#{id},#{turnId},#{conversationId},#{tenant},#{subject},#{releaseHash},#{hash},#{payload},#{bytes})")
  int insertArtifact(Artifact artifact);

  @Select(
      "SELECT id,turn_id,conversation_id,tenant,subject,release_hash,hash,payload,bytes FROM business.artifacts WHERE id=#{id}")
  Artifact artifact(String id);

  @Select(
      "SELECT action_hash,status,result FROM business.tool_calls WHERE turn_id=#{turn} AND call_id=#{call}")
  Effect effect(@Param("turn") String turn, @Param("call") String call);

  @Insert(
      "INSERT INTO business.tool_calls(turn_id,call_id,action_hash,status) VALUES(#{turn},#{call},#{hash},'pending')")
  int beginEffect(
      @Param("turn") String turn, @Param("call") String call, @Param("hash") String hash);

  @Update(
      "UPDATE business.tool_calls SET status='completed',result=#{result} WHERE turn_id=#{turn} AND call_id=#{call} AND status='pending'")
  int completeEffect(
      @Param("turn") String turn, @Param("call") String call, @Param("result") String result);

  @Select(
      "SELECT EXISTS(SELECT 1 FROM business.tool_calls WHERE turn_id=#{id} AND status='pending')")
  boolean uncertain(String id);

  @Insert(
      "INSERT INTO business.reports(id,turn_id,call_id,title) VALUES(#{id},#{turn},#{call},#{title})")
  int report(
      @Param("id") String id,
      @Param("turn") String turn,
      @Param("call") String call,
      @Param("title") String title);

  @Select("SELECT started_at::text FROM business.attempts WHERE id=#{id}")
  String startedAt(String id);

  @Select("SELECT clock_timestamp()::text")
  String now();
}
