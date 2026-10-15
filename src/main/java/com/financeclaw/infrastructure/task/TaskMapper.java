package com.financeclaw.infrastructure.task;

import com.financeclaw.domain.Task.*;
import java.util.List;
import org.apache.ibatis.annotations.*;

@Mapper
public interface TaskMapper {
  @Insert(
      "INSERT INTO business.admission(tenant,subject) VALUES(#{tenant},#{subject}) ON CONFLICT DO NOTHING")
  void ensureAdmission(Owner owner);

  @Select(
      "SELECT paused FROM business.admission WHERE tenant=#{tenant} AND subject=#{subject} FOR SHARE")
  boolean admissionPaused(Owner owner);

  @Select(
      "SELECT paused,revision FROM business.admission WHERE tenant=#{tenant} AND subject=#{subject}")
  com.financeclaw.domain.Operations.Admission admission(Owner owner);

  @Select(
      "SELECT paused,revision FROM business.admission WHERE tenant=#{tenant} AND subject=#{subject} FOR UPDATE")
  com.financeclaw.domain.Operations.Admission lockAdmission(Owner owner);

  @Select(
      "SELECT request_hash,paused,revision FROM business.admission_changes WHERE tenant=#{owner.tenant} AND subject=#{owner.subject} AND idempotency_key=#{key}")
  com.financeclaw.domain.Operations.Change admissionReplay(
      @Param("owner") Owner owner, @Param("key") String key);

  @Update(
      "UPDATE business.admission SET paused=#{paused},revision=revision+1 WHERE tenant=#{owner.tenant} AND subject=#{owner.subject} AND revision=#{revision}")
  int changeAdmission(
      @Param("owner") Owner owner,
      @Param("paused") boolean paused,
      @Param("revision") long revision);

  @Insert(
      "INSERT INTO business.admission_changes(tenant,subject,idempotency_key,request_hash,paused,revision) VALUES(#{owner.tenant},#{owner.subject},#{key},#{hash},#{paused},#{revision})")
  void recordAdmission(
      @Param("owner") Owner owner,
      @Param("key") String key,
      @Param("hash") String hash,
      @Param("paused") boolean paused,
      @Param("revision") long revision);

  @Select(
      "SELECT EXISTS(SELECT 1 FROM business.admission LIMIT 1) OR NOT EXISTS(SELECT 1 FROM business.admission LIMIT 1)")
  boolean admissionReady();

  @Insert(
      "INSERT INTO business.conversations(id,tenant,subject) VALUES(#{id},#{owner.tenant},#{owner.subject})")
  int createConversation(@Param("id") String id, @Param("owner") Owner owner);

  @Select(
      "SELECT id FROM business.conversations WHERE id=#{id} AND tenant=#{owner.tenant} AND subject=#{owner.subject} FOR UPDATE")
  String lockConversation(@Param("id") String id, @Param("owner") Owner owner);

  @Select(
      "SELECT id FROM business.conversations WHERE id=#{id} AND tenant=#{owner.tenant} AND subject=#{owner.subject}")
  String ownedConversation(@Param("id") String id, @Param("owner") Owner owner);

  String TURN_COLUMNS =
      "id,conversation_id,tenant,subject,idempotency_key,request_hash,message,release_hash,status,revision,result,reason";

  @Select("SELECT " + TURN_COLUMNS + " FROM business.turns WHERE id=#{id}")
  Turn turn(String id);

  @Select("SELECT " + TURN_COLUMNS + " FROM business.turns WHERE id=#{id} FOR UPDATE")
  Turn lockTurn(String id);

  @Select(
      "SELECT "
          + TURN_COLUMNS
          + " FROM business.turns WHERE conversation_id=#{conversation} AND idempotency_key=#{key}")
  Turn replay(@Param("conversation") String conversation, @Param("key") String key);

  @Select(
      "SELECT EXISTS(SELECT 1 FROM business.turns WHERE conversation_id=#{id} AND status IN ('queued','resuming','running','waiting','cancelling','blocked'))")
  boolean active(String id);

  @Insert(
      """
    INSERT INTO business.turns(id,conversation_id,tenant,subject,idempotency_key,request_hash,message,release_hash,status)
    VALUES(#{id},#{conversationId},#{tenant},#{subject},#{idempotencyKey},#{requestHash},#{message},#{releaseHash},'queued')
    """)
  int insertTurn(Turn turn);

  @Insert("INSERT INTO business.commands(id,turn_id,status) VALUES(#{id},#{turn},'queued')")
  int insertCommand(@Param("id") String id, @Param("turn") String turn);

  String COMMAND_COLUMNS =
      "id,turn_id,status,attempt_id,fence,COALESCE(lease_until>clock_timestamp(),false) AS live,resume_id";

  @Select(
      "SELECT "
          + COMMAND_COLUMNS
          + " FROM business.commands WHERE status='queued' ORDER BY created_at,id LIMIT 1 FOR UPDATE SKIP LOCKED")
  Command nextCommand();

  @Select("SELECT " + COMMAND_COLUMNS + " FROM business.commands WHERE id=#{id} FOR UPDATE")
  Command lockCommand(String id);

  @Select("SELECT " + COMMAND_COLUMNS + " FROM business.commands WHERE turn_id=#{id} FOR UPDATE")
  Command lockTurnCommand(String id);

  @Select(
      "SELECT id FROM business.commands WHERE status='running' AND lease_until<=clock_timestamp() ORDER BY lease_until LIMIT 50")
  List<String> expired();

  @Update(
      """
    UPDATE business.commands SET status='running',attempt_id=#{attempt},fence=fence+1,
    worker_id=#{worker},lease_until=clock_timestamp()+#{lease}*interval '1 millisecond' WHERE id=#{id}
    """)
  int claim(
      @Param("id") String id,
      @Param("attempt") String attempt,
      @Param("worker") String worker,
      @Param("lease") long lease);

  @Insert(
      "INSERT INTO business.attempts(id,command_id,fence,resume_id) VALUES(#{attemptId},#{id},#{fence},#{resumeId})")
  int insertAttempt(Command command);

  @Update(
      "UPDATE business.commands SET lease_until=clock_timestamp()+#{lease}*interval '1 millisecond' WHERE id=#{id}")
  int renew(@Param("id") String id, @Param("lease") long lease);

  @Update(
      "UPDATE business.commands SET status='done',lease_until=NULL,fence=fence+1 WHERE id=#{id}")
  int finishCommand(String id);

  @Update(
      "UPDATE business.turns SET status=#{status},revision=revision+1,result=#{result},reason=#{reason} WHERE id=#{id}")
  int transition(
      @Param("id") String id,
      @Param("status") String status,
      @Param("result") String result,
      @Param("reason") String reason);

  @Insert(
      "INSERT INTO business.events(turn_id,revision,status,reason) SELECT id,revision,status,reason FROM business.turns WHERE id=#{id}")
  int event(String id);

  @Select(
      "SELECT turn_id,revision,status,reason,kind,text,tool FROM business.events WHERE turn_id=#{id} AND revision>#{after} ORDER BY revision LIMIT 100")
  List<Event> events(@Param("id") String id, @Param("after") long after);

  @Insert(
      "INSERT INTO business.journal(conversation_id,turn_id,role,content) VALUES(#{conversation},#{turn},#{role},#{content}) ON CONFLICT DO NOTHING")
  int journal(
      @Param("conversation") String conversation,
      @Param("turn") String turn,
      @Param("role") String role,
      @Param("content") String content);

  @Select(
      "SELECT sequence,conversation_id,turn_id,role,content FROM business.journal WHERE conversation_id=#{conversation} AND sequence>#{after} ORDER BY sequence LIMIT #{limit}")
  List<Journal> messages(
      @Param("conversation") String conversation,
      @Param("after") long after,
      @Param("limit") int limit);

  @Update(
      "UPDATE business.attempts SET state_version=#{value.stateVersion},outcome=#{value.status},result=#{value.result} WHERE id=#{id} AND outcome IS NULL")
  int seal(@Param("id") String id, @Param("value") Outcome value);

  @Select(
      "SELECT id,command_id,fence,state_version,outcome,result FROM business.attempts WHERE id=#{id}")
  Receipt receipt(String id);

  @Insert(
      "INSERT INTO business.outbox(turn_id,revision,status) SELECT id,revision,status FROM business.turns WHERE id=#{id} ON CONFLICT DO NOTHING")
  int outbox(String id);

  @Select(
      "SELECT id,turn_id,revision,status FROM business.outbox WHERE NOT delivered ORDER BY id LIMIT 1 FOR UPDATE SKIP LOCKED")
  Delivery nextDelivery();

  @Insert(
      "INSERT INTO business.projections(turn_id,revision,status) VALUES(#{turnId},#{revision},#{status}) ON CONFLICT(turn_id) DO UPDATE SET revision=excluded.revision,status=excluded.status WHERE business.projections.revision<excluded.revision")
  int project(Delivery delivery);

  @Update("UPDATE business.outbox SET delivered=true WHERE id=#{id}")
  int delivered(long id);

  @Select(
      "SELECT t.conversation_id,c.turn_id,c.status,c.revision,true AS idempotent_replay FROM business.cancel_requests c JOIN business.turns t ON t.id=c.turn_id WHERE c.turn_id=#{turn} AND c.idempotency_key=#{key}")
  Accepted cancelReplay(@Param("turn") String turn, @Param("key") String key);

  @Insert(
      "INSERT INTO business.cancel_requests(turn_id,idempotency_key,status,revision) VALUES(#{turn},#{key},#{status},#{revision})")
  int recordCancel(
      @Param("turn") String turn,
      @Param("key") String key,
      @Param("status") String status,
      @Param("revision") long revision);

  @Select(
      "SELECT "
          + TURN_COLUMNS
          + " FROM business.turns WHERE conversation_id=#{conversation} AND status='completed' AND release_hash=#{release} AND id IN (SELECT turn_id FROM business.turn_policies WHERE grant_revision=#{revision}) ORDER BY created_at DESC,id DESC LIMIT 8")
  List<Turn> history(
      @Param("conversation") String conversation,
      @Param("release") String release,
      @Param("revision") long revision);

  @Update("UPDATE business.turns SET revision=revision+1 WHERE id=#{id}")
  int advance(String id);

  @Insert(
      "INSERT INTO business.events(turn_id,revision,status,kind,text,tool) SELECT id,revision,status,#{kind},#{text},#{tool} FROM business.turns WHERE id=#{id}")
  int progress(
      @Param("id") String id,
      @Param("kind") String kind,
      @Param("text") String text,
      @Param("tool") String tool);

  @Select(
      "SELECT EXISTS(SELECT 1 FROM business.artifacts LIMIT 1) OR NOT EXISTS(SELECT 1 FROM business.artifacts LIMIT 1)")
  boolean artifactsReady();

  @Select(
      "SELECT EXISTS(SELECT 1 FROM agent_runtime.states LIMIT 1) OR NOT EXISTS(SELECT 1 FROM agent_runtime.states LIMIT 1)")
  boolean stateReady();
}
