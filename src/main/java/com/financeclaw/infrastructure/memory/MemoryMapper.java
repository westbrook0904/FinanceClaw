package com.financeclaw.infrastructure.memory;

import com.financeclaw.domain.Memory.*;
import com.financeclaw.domain.Task.Owner;
import java.util.List;
import org.apache.ibatis.annotations.*;

@Mapper
public interface MemoryMapper {
  @Select(
      "SELECT NOT has_schema_privilege(current_user,'business','USAGE') AND NOT has_schema_privilege(current_user,'agent_runtime','USAGE') AND NOT has_schema_privilege(current_user,'channel','USAGE') AND NOT has_schema_privilege(current_user,'memory','CREATE')")
  boolean restrictedWorker();

  @Insert(
      "INSERT INTO memory.embedding_profiles(revision,fingerprint,model,dimensions) VALUES(#{revision},#{fingerprint},#{model},1024) ON CONFLICT DO NOTHING")
  void registerProfile(
      @Param("revision") String revision,
      @Param("fingerprint") String fingerprint,
      @Param("model") String model);

  @Select("SELECT fingerprint FROM memory.embedding_profiles WHERE revision=#{revision}")
  String profile(String revision);

  @Insert(
      "INSERT INTO memory.owners(tenant,subject) VALUES(#{tenant},#{subject}) ON CONFLICT DO NOTHING")
  void ensure(Owner owner);

  @Select("SELECT * FROM memory.owners WHERE tenant=#{tenant} AND subject=#{subject} FOR UPDATE")
  Privacy lock(Owner owner);

  @Insert(
      "INSERT INTO memory.sources(id,tenant,subject,epoch,content) VALUES(#{id},#{tenant},#{subject},#{epoch},#{content})")
  void source(Source source);

  @Select("SELECT id,tenant,subject,epoch,content,status FROM memory.sources WHERE id=#{id}")
  Source sourceById(String id);

  @Update(
      "UPDATE memory.sources SET status='pending' WHERE id=#{id} AND status='admitted' AND epoch=(SELECT epoch FROM memory.owners WHERE tenant=memory.sources.tenant AND subject=memory.sources.subject)")
  int ready(String id);

  @Insert(
      "INSERT INTO memory.jobs(kind,key_id,tenant,subject,epoch) SELECT 'extract',id,tenant,subject,epoch FROM memory.sources WHERE id=#{id} AND status='pending' ON CONFLICT DO NOTHING")
  void extractJob(String id);

  @Insert(
      "INSERT INTO memory.recalls(turn_id,fact_id,revision,epoch) SELECT #{turn},id,revision,epoch FROM memory.facts WHERE tenant=#{owner.tenant} AND subject=#{owner.subject} AND status='active' AND epoch=#{epoch} ORDER BY created_at DESC,id LIMIT 16")
  void freeze(@Param("turn") String turn, @Param("owner") Owner owner, @Param("epoch") long epoch);

  String COLUMNS =
      "f.id,f.source_id,f.tenant,f.subject,f.epoch,f.key,f.content,f.evidence,f.revision,f.status";

  @Select(
      "SELECT "
          + COLUMNS
          + " FROM memory.recalls r JOIN memory.facts f ON f.id=r.fact_id AND f.revision=r.revision AND f.epoch=r.epoch JOIN memory.owners o ON o.tenant=f.tenant AND o.subject=f.subject AND o.epoch=f.epoch WHERE r.turn_id=#{turn} AND f.tenant=#{owner.tenant} AND f.subject=#{owner.subject} AND f.status='active' AND NOT o.purge_pending ORDER BY f.id")
  List<Fact> recall(@Param("turn") String turn, @Param("owner") Owner owner);

  @Select(
      "SELECT "
          + COLUMNS
          + " FROM memory.facts f JOIN memory.owners o ON o.tenant=f.tenant AND o.subject=f.subject AND o.epoch=f.epoch WHERE f.tenant=#{tenant} AND f.subject=#{subject} AND f.status IN ('active','candidate') AND NOT o.purge_pending ORDER BY f.created_at DESC LIMIT 100")
  List<Fact> list(Owner owner);

  @Select("SELECT " + COLUMNS + " FROM memory.facts f WHERE id=#{id}")
  Fact fact(String id);

  @Select(
      "SELECT EXISTS(SELECT 1 FROM memory.facts f JOIN memory.sources s ON s.id=f.source_id JOIN memory.sources incoming ON incoming.id=#{sourceId} WHERE f.tenant=#{tenant} AND f.subject=#{subject} AND f.key=#{key} AND f.status='active' AND (s.created_at,s.id)>(incoming.created_at,incoming.id))")
  boolean newerActive(Fact fact);

  @Select(
      "SELECT "
          + COLUMNS
          + " FROM memory.facts f JOIN memory.owners o ON o.tenant=f.tenant AND o.subject=f.subject AND o.epoch=f.epoch WHERE f.tenant=#{tenant} AND f.subject=#{subject} AND f.status='active' AND NOT o.purge_pending ORDER BY f.id")
  List<Fact> active(Owner owner);

  @Insert(
      "INSERT INTO memory.facts(id,source_id,tenant,subject,epoch,key,content,evidence,status) VALUES(#{id},#{sourceId},#{tenant},#{subject},#{epoch},#{key},#{content},#{evidence},#{status}) ON CONFLICT(source_id,key) DO NOTHING")
  int insertFact(Fact fact);

  @Update(
      "UPDATE memory.facts SET status='superseded',revision=revision+1 WHERE tenant=#{tenant} AND subject=#{subject} AND key=#{key} AND status='active' AND id<>#{id}")
  void supersede(Fact fact);

  @Update("UPDATE memory.facts SET status=#{status},revision=revision+1 WHERE id=#{id}")
  void status(@Param("id") String id, @Param("status") String status);

  @Insert(
      "INSERT INTO memory.jobs(kind,key_id,tenant,subject,epoch,revision,model_revision) SELECT 'index',id,tenant,subject,epoch,revision,#{model} FROM memory.facts WHERE id=#{id} AND status='active' ON CONFLICT(kind,key_id,revision,model_revision) DO UPDATE SET status='pending',next_attempt_at=clock_timestamp() WHERE memory.jobs.status='done'")
  void indexJob(@Param("id") String id, @Param("model") String model);

  @Select(
      "SELECT id,kind,key_id,tenant,subject,epoch,revision,model_revision,status,attempts,lease_token FROM memory.jobs WHERE (status='pending' AND next_attempt_at<=clock_timestamp()) OR (status='running' AND lease_until<=clock_timestamp()) ORDER BY id LIMIT 1 FOR UPDATE SKIP LOCKED")
  Job next();

  @Update(
      "UPDATE memory.jobs SET status='running',attempts=attempts+1,lease_token=#{token},lease_until=clock_timestamp()+interval '60 seconds' WHERE id=#{id}")
  void claim(@Param("id") long id, @Param("token") String token);

  @Select(
      "SELECT id,kind,key_id,tenant,subject,epoch,revision,model_revision,status,attempts,lease_token FROM memory.jobs WHERE id=#{id} FOR UPDATE")
  Job job(long id);

  @Select(
      "SELECT EXISTS(SELECT 1 FROM memory.jobs WHERE id=#{id} AND lease_token=#{leaseToken} AND lease_until>clock_timestamp())")
  boolean liveLease(Job job);

  @Update(
      "UPDATE memory.jobs SET status=#{status},lease_until=NULL,error_code=#{error},next_attempt_at=clock_timestamp()+interval '30 seconds' WHERE id=#{job.id} AND lease_token=#{job.leaseToken} AND status='running'")
  void finish(@Param("job") Job job, @Param("status") String status, @Param("error") String error);

  @Update("UPDATE memory.sources SET status='extracted' WHERE id=#{id} AND status='pending'")
  void extracted(String id);

  @Insert(
      "INSERT INTO memory.projections(fact_id,tenant,subject,epoch,revision,model_revision,dimensions,embedding) VALUES(#{fact.id},#{fact.tenant},#{fact.subject},#{fact.epoch},#{fact.revision},#{model},1024,CAST(#{vector} AS real[])) ON CONFLICT(fact_id,model_revision) DO UPDATE SET epoch=excluded.epoch,revision=excluded.revision,model_revision=excluded.model_revision,embedding=excluded.embedding")
  void project(
      @Param("fact") Fact fact, @Param("model") String model, @Param("vector") String vector);

  @Select(
      "SELECT "
          + COLUMNS
          + " FROM memory.projections p JOIN memory.facts f ON f.id=p.fact_id AND f.revision=p.revision AND f.epoch=p.epoch JOIN memory.owners o ON o.tenant=f.tenant AND o.subject=f.subject AND o.epoch=f.epoch WHERE p.tenant=#{owner.tenant} AND p.subject=#{owner.subject} AND f.tenant=p.tenant AND f.subject=p.subject AND p.model_revision=#{model} AND f.status='active' AND NOT o.purge_pending ORDER BY CAST(p.embedding AS vector(1024)) <=> CAST(#{vector} AS vector(1024)),f.id LIMIT 16")
  List<Fact> search(
      @Param("owner") Owner owner, @Param("model") String model, @Param("vector") String vector);

  @Select(
      "SELECT request_hash,result FROM memory.operations WHERE tenant=#{owner.tenant} AND subject=#{owner.subject} AND key=#{key}")
  Operation operation(@Param("owner") Owner owner, @Param("key") String key);

  @Insert(
      "INSERT INTO memory.operations(tenant,subject,key,request_hash,result) VALUES(#{owner.tenant},#{owner.subject},#{key},#{hash},#{result})")
  void recordOperation(
      @Param("owner") Owner owner,
      @Param("key") String key,
      @Param("hash") String hash,
      @Param("result") String result);

  @Insert(
      "INSERT INTO memory.audit(tenant,subject,epoch,action,target) VALUES(#{owner.tenant},#{owner.subject},#{epoch},#{action},#{target})")
  void audit(
      @Param("owner") Owner owner,
      @Param("epoch") long epoch,
      @Param("action") String action,
      @Param("target") String target);

  @Update(
      "UPDATE memory.owners SET epoch=epoch+1,purge_pending=true WHERE tenant=#{tenant} AND subject=#{subject}")
  void forget(Owner owner);

  @Update(
      "UPDATE memory.sources SET content=NULL,status='forgotten' WHERE tenant=#{tenant} AND subject=#{subject}")
  void eraseSources(Owner owner);

  @Update(
      "UPDATE memory.facts SET content=NULL,evidence=NULL,status='forgotten',revision=revision+1 WHERE tenant=#{tenant} AND subject=#{subject}")
  void eraseFacts(Owner owner);

  @Delete("DELETE FROM memory.projections WHERE tenant=#{tenant} AND subject=#{subject}")
  void eraseIndex(Owner owner);

  @Delete(
      "DELETE FROM memory.recalls WHERE fact_id IN (SELECT id FROM memory.facts WHERE tenant=#{tenant} AND subject=#{subject})")
  void eraseRecalls(Owner owner);

  @Update(
      "UPDATE memory.jobs SET status='forgotten',lease_token=NULL,lease_until=NULL WHERE tenant=#{tenant} AND subject=#{subject}")
  void eraseJobs(Owner owner);

  @Select(
      "SELECT tenant,subject,epoch,purge_pending FROM memory.owners o WHERE purge_pending AND NOT EXISTS (SELECT 1 FROM memory.forget_requests r WHERE r.tenant=o.tenant AND r.subject=o.subject) ORDER BY tenant,subject LIMIT 1 FOR UPDATE SKIP LOCKED")
  Privacy purge();

  @Update(
      "UPDATE memory.owners o SET purge_pending=false WHERE tenant=#{tenant} AND subject=#{subject} AND purge_pending AND NOT EXISTS (SELECT 1 FROM memory.forget_requests r WHERE r.tenant=o.tenant AND r.subject=o.subject)")
  int purged(Owner owner);
}
