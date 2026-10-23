package com.financeclaw.infrastructure.governance;

import com.financeclaw.domain.Governance.*;
import java.util.List;
import org.apache.ibatis.annotations.*;

@Mapper
public interface GovernanceMapper {
  record Step(String inputHash, String result) {}

  @Update("UPDATE business.artifacts SET required_scope=#{scope} WHERE id=#{id}")
  int updateArtifactScope(@Param("id") String id, @Param("scope") String scope);

  @Select("SELECT required_scope FROM business.artifacts WHERE id=#{id}")
  String getArtifactScope(String id);

  @Select(
      "SELECT input_hash,result FROM business.capability_steps WHERE turn_id=#{turn} AND namespace=#{namespace} AND step=#{step}")
  Step getCapabilityStep(
      @Param("turn") String turn, @Param("namespace") String namespace, @Param("step") String step);

  @Insert(
      "INSERT INTO business.capability_steps(turn_id,namespace,step,input_hash,result) VALUES(#{turn},#{namespace},#{step},#{hash},#{result})")
  int insertCapabilityStep(
      @Param("turn") String turn,
      @Param("namespace") String namespace,
      @Param("step") String step,
      @Param("hash") String hash,
      @Param("result") String result);

  @Insert(
      "INSERT INTO business.grants(conversation_id,scopes) VALUES(#{id},CAST(#{scopes} AS jsonb)) ON CONFLICT DO NOTHING")
  int insertGrantIfAbsent(@Param("id") String id, @Param("scopes") String scopes);

  @Select(
      "SELECT conversation_id,revision,scopes::text FROM business.grants WHERE conversation_id=#{id} FOR UPDATE")
  Grant getGrantForUpdate(String id);

  @Select(
      "SELECT conversation_id,revision,scopes::text FROM business.grants WHERE conversation_id=#{id}")
  Grant getGrant(String id);

  @Update(
      "UPDATE business.grants SET revision=revision+1,scopes=CAST(#{scopes} AS jsonb) WHERE conversation_id=#{id}")
  int updateGrant(@Param("id") String id, @Param("scopes") String scopes);

  @Select(
      "SELECT request_hash,conversation_id,revision,scopes::text FROM business.grant_changes WHERE conversation_id=#{id} AND idempotency_key=#{key}")
  com.financeclaw.domain.governance.GrantReceipt getGrantReceipt(
      @Param("id") String id, @Param("key") String key);

  @Insert(
      "INSERT INTO business.grant_changes(conversation_id,idempotency_key,request_hash,revision,scopes) SELECT conversation_id,#{key},#{hash},revision,scopes FROM business.grants WHERE conversation_id=#{id}")
  int insertGrantChange(
      @Param("id") String id, @Param("key") String key, @Param("hash") String hash);

  @Insert(
      "INSERT INTO business.turn_policies(turn_id,grant_revision,scopes) VALUES(#{turn},#{revision},CAST(#{scopes} AS jsonb))")
  int insertTurnPolicy(
      @Param("turn") String turn, @Param("revision") long revision, @Param("scopes") String scopes);

  @Select(
      "SELECT turn_id,grant_revision,scopes::text,model_calls,tool_calls FROM business.turn_policies WHERE turn_id=#{turn}")
  Policy getTurnPolicy(String turn);

  @Update(
      "UPDATE business.turn_policies SET model_calls=model_calls+1 WHERE turn_id=#{id} AND model_calls<#{max}")
  int updateModelCallCountWithinLimit(@Param("id") String id, @Param("max") int max);

  @Update(
      "UPDATE business.turn_policies SET tool_calls=tool_calls+1 WHERE turn_id=#{id} AND tool_calls<#{max}")
  int updateToolCallCountWithinLimit(@Param("id") String id, @Param("max") int max);

  @Insert(
      """
    INSERT INTO business.interactions(id,turn_id,command_id,attempt_id,state_version,grant_revision,release_hash,kind,call_id,tool,action_hash,schema_json,question)
    VALUES(#{id},#{turnId},#{commandId},#{attemptId},#{stateVersion},#{grantRevision},#{releaseHash},#{kind},#{callId},#{tool},#{actionHash},#{schema},#{question})
    """)
  int insertInteraction(Interaction i);

  String INTERACTION_COLUMNS =
      "id,turn_id,command_id,attempt_id,state_version,grant_revision,release_hash,kind,call_id,tool,action_hash,schema_json AS schema,question,revision,status,expires_at>clock_timestamp() AS live,response_hash,response_json AS response";

  @Select("SELECT " + INTERACTION_COLUMNS + " FROM business.interactions WHERE id=#{id}")
  Interaction getInteractionById(String id);

  @Select("SELECT " + INTERACTION_COLUMNS + " FROM business.interactions WHERE id=#{id} FOR UPDATE")
  Interaction getInteractionForUpdate(String id);

  @Select(
      "SELECT "
          + INTERACTION_COLUMNS
          + " FROM business.interactions WHERE turn_id=#{id} ORDER BY revision,id")
  List<Interaction> listInteractionsByTurnId(String id);

  @Update(
      "UPDATE business.interactions SET status='answered',response_hash=#{hash},response_json=#{json} WHERE id=#{id} AND status='pending'")
  int updatePendingInteractionAnswer(
      @Param("id") String id, @Param("hash") String hash, @Param("json") String json);

  @Update(
      "UPDATE business.interactions SET status='invalid' WHERE turn_id=#{id} AND status='pending'")
  int updatePendingInteractionsInvalid(String id);

  @Update(
      "UPDATE business.commands SET status='queued',resume_id=#{interaction},attempt_id=NULL,lease_until=NULL WHERE id=#{command} AND status='done'")
  int updateDoneCommandForResume(
      @Param("command") String command, @Param("interaction") String interaction);

  @Insert(
      "INSERT INTO business.artifacts(id,turn_id,conversation_id,tenant,subject,release_hash,hash,payload,bytes) VALUES(#{id},#{turnId},#{conversationId},#{tenant},#{subject},#{releaseHash},#{hash},#{payload},#{bytes})")
  int insertArtifact(Artifact artifact);

  @Select(
      "SELECT id,turn_id,conversation_id,tenant,subject,release_hash,hash,payload,bytes FROM business.artifacts WHERE id=#{id}")
  Artifact getArtifactById(String id);

  @Select(
      "SELECT action_hash,status,result FROM business.reports WHERE turn_id=#{turn} AND call_id=#{call}")
  Effect getToolCallEffect(@Param("turn") String turn, @Param("call") String call);

  @Select(
      "SELECT EXISTS(SELECT 1 FROM business.reports WHERE turn_id=#{id} AND (status<>'completed' OR result IS NULL))")
  boolean existsPendingToolCall(String id);

  @Insert(
      "INSERT INTO business.reports(id,turn_id,call_id,title,action_hash,status,result) VALUES(#{id},#{turn},#{call},#{title},#{hash},'completed',#{result})")
  int insertReportPublication(
      @Param("id") String id,
      @Param("turn") String turn,
      @Param("call") String call,
      @Param("title") String title,
      @Param("hash") String hash,
      @Param("result") String result);

  @Select("SELECT started_at::text FROM business.attempts WHERE id=#{id}")
  String getAttemptStartedAt(String id);

  @Select("SELECT clock_timestamp()::text")
  String getDatabaseTimestamp();

  @Select("SELECT created_at FROM business.turns WHERE id=#{id}")
  java.time.Instant getTurnCreatedAt(String id);
}
