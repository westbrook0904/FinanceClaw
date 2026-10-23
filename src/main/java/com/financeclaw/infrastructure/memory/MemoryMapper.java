package com.financeclaw.infrastructure.memory;

import com.financeclaw.domain.Memory.*;
import com.financeclaw.domain.Task.Owner;
import com.financeclaw.domain.memory.TurnMemorySnapshot;
import com.financeclaw.domain.memory.UserProfile;
import java.util.List;
import org.apache.ibatis.annotations.*;

/**
 * 用户画像、跨会话证据及异步记忆任务的持久化映射。
 *
 * <p>owner 的 epoch 是遗忘隔离边界，profile_revision 记录受理时选择的画像版本。 受理持共享锁，画像修改、遗忘和记忆提交持排他锁；异步结果按 owner → job
 * 顺序重验。 所有正文读取必须由应用层校验 owner、epoch、purge_pending 和对应 revision。 投影可重建，不能把向量命中或 SDK 状态作为事实证据。
 */
@Mapper
public interface MemoryMapper {
    /** 检查数据库角色不能访问业务、运行时和渠道 schema，也不能在 memory 内创建对象。 这不是用户授权检查，而是记忆 Worker 启动时的最小权限边界。 */
    @Select(
            "SELECT NOT has_schema_privilege(current_user,'business','USAGE') AND NOT"
                    + " has_schema_privilege(current_user,'agent_runtime','USAGE') AND NOT"
                    + " has_schema_privilege(current_user,'channel','USAGE') AND NOT"
                    + " has_schema_privilege(current_user,'memory','CREATE') AND NOT"
                    + " has_table_privilege(current_user,'memory.user_profiles','SELECT,INSERT,UPDATE,DELETE') AND NOT"
                    + " has_table_privilege(current_user,'memory.turn_snapshots','SELECT,INSERT,UPDATE,DELETE')")
    boolean isMemoryWorkerRoleRestricted();

    @Insert(
            "INSERT INTO memory.embedding_profiles(revision,fingerprint,model,dimensions)"
                    + " VALUES(#{revision},#{fingerprint},#{model},1024) ON CONFLICT DO NOTHING")
    void insertEmbeddingProfileIfAbsent(
            @Param("revision") String revision,
            @Param("fingerprint") String fingerprint,
            @Param("model") String model);

    @Select("SELECT fingerprint FROM memory.embedding_profiles WHERE revision=#{revision}")
    String getEmbeddingProfileFingerprint(String revision);

    @Insert(
            "INSERT INTO memory.owners(tenant,subject) VALUES(#{tenant},#{subject}) ON CONFLICT DO"
                    + " NOTHING")
    void insertOwnerIfAbsent(Owner owner);

    /** 独占 owner 隐私状态，用于画像修改、遗忘、事实确认及异步提交。需要在外层事务内调用。 */
    @ConstructorArgs({
        @Arg(column = "tenant", javaType = String.class),
        @Arg(column = "subject", javaType = String.class),
        @Arg(column = "epoch", javaType = long.class),
        @Arg(column = "purge_pending", javaType = boolean.class),
        @Arg(column = "profile_revision", javaType = long.class)
    })
    @Select(
            "SELECT tenant,subject,epoch,purge_pending,profile_revision FROM memory.owners WHERE"
                    + " tenant=#{tenant} AND subject=#{subject} FOR UPDATE")
    Privacy getOwnerPrivacyForUpdate(Owner owner);

    /** 只为受理冻结 owner 的 epoch 与画像版本。共享锁不升级为排他锁，允许该用户不同会话并行受理。 */
    @ConstructorArgs({
        @Arg(column = "tenant", javaType = String.class),
        @Arg(column = "subject", javaType = String.class),
        @Arg(column = "epoch", javaType = long.class),
        @Arg(column = "purge_pending", javaType = boolean.class),
        @Arg(column = "profile_revision", javaType = long.class)
    })
    @Select(
            "SELECT tenant,subject,epoch,purge_pending,profile_revision FROM memory.owners WHERE"
                    + " tenant=#{tenant} AND subject=#{subject} FOR SHARE")
    Privacy getOwnerPrivacyForShare(Owner owner);

    @Select(
            "SELECT epoch,revision,markdown FROM memory.user_profiles WHERE tenant=#{tenant} AND"
                    + " subject=#{subject}")
    UserProfile getUserProfile(Owner owner);

    /** 同时核对 epoch、期望画像版本及无待清除屏障后推进版本；返回 0 时必须报告版本冲突。 */
    @Update(
            "UPDATE memory.owners SET profile_revision=profile_revision+1 WHERE"
                    + " tenant=#{owner.tenant} AND subject=#{owner.subject} AND epoch=#{epoch} AND"
                    + " profile_revision=#{revision} AND NOT purge_pending")
    int updateProfileRevisionByEpochAndExpectedRevision(
            @Param("owner") Owner owner,
            @Param("epoch") long epoch,
            @Param("revision") long revision);

    @Insert(
            "INSERT INTO memory.user_profiles(tenant,subject,epoch,revision,markdown)"
                    + " VALUES(#{owner.tenant},#{owner.subject},#{profile.epoch},#{profile.revision},#{profile.markdown})"
                    + " ON CONFLICT(tenant,subject) DO UPDATE SET"
                    + " epoch=excluded.epoch,revision=excluded.revision,markdown=excluded.markdown,updated_at=clock_timestamp()")
    void insertOrUpdateUserProfile(
            @Param("owner") Owner owner, @Param("profile") UserProfile profile);

    @Delete("DELETE FROM memory.user_profiles WHERE tenant=#{tenant} AND subject=#{subject}")
    void deleteUserProfile(Owner owner);

    @Insert(
            "INSERT INTO memory.sources(id,tenant,subject,epoch,content,status,profile_revision)"
                    + " VALUES(#{id},#{tenant},#{subject},#{epoch},#{content},#{status},#{profileRevision})")
    void insertSource(Source source);

    @ConstructorArgs({
        @Arg(column = "id", javaType = String.class),
        @Arg(column = "tenant", javaType = String.class),
        @Arg(column = "subject", javaType = String.class),
        @Arg(column = "epoch", javaType = long.class),
        @Arg(column = "content", javaType = String.class),
        @Arg(column = "status", javaType = String.class),
        @Arg(column = "profile_revision", javaType = long.class)
    })
    @Select(
            "SELECT id,tenant,subject,epoch,content,status,profile_revision FROM memory.sources"
                    + " WHERE id=#{id}")
    Source getSourceById(String id);

    @Select({
        "<script>SELECT id FROM memory.sources WHERE tenant=#{owner.tenant} AND"
                + " subject=#{owner.subject} AND epoch=#{epoch} AND status!='forgotten'"
                + " AND id IN",
        "<foreach collection='ids' item='id' open='(' separator=',' close=')'>#{id}</foreach>",
        "</script>"
    })
    List<String> listCurrentSourceIds(
            @Param("owner") Owner owner,
            @Param("epoch") long epoch,
            @Param("ids") List<String> ids);

    @Update(
            "UPDATE memory.sources SET status='pending' WHERE id=#{id} AND status='admitted' AND"
                    + " epoch=(SELECT epoch FROM memory.owners WHERE tenant=memory.sources.tenant AND"
                    + " subject=memory.sources.subject)")
    int updateAdmittedSourcePending(String id);

    @Insert(
            "INSERT INTO memory.jobs(kind,key_id,tenant,subject,epoch) SELECT"
                    + " 'extract',id,tenant,subject,epoch FROM memory.sources WHERE id=#{id} AND"
                    + " status='pending' ON CONFLICT DO NOTHING")
    void insertExtractJobIfAbsent(String id);

    /** 仅受理时选择一次当前有效证据；已受理 Turn 的正文不再跟随事实替代或画像编辑变化。 */
    @Select(
            "SELECT id,source_id,tenant,subject,epoch,key,content,evidence,revision,status"
                    + " FROM memory.facts WHERE tenant=#{owner.tenant} AND subject=#{owner.subject}"
                    + " AND epoch=#{epoch} AND status='active' ORDER BY created_at DESC,id LIMIT 16")
    List<Fact> listFactsForTurnSnapshot(@Param("owner") Owner owner, @Param("epoch") long epoch);

    @Insert(
            "INSERT INTO memory.turn_snapshots(turn_id,tenant,subject,epoch,profile_revision,context)"
                    + " VALUES(#{turnId},#{tenant},#{subject},#{epoch},#{profileRevision},#{context})")
    void insertTurnMemorySnapshot(TurnMemorySnapshot snapshot);

    @ConstructorArgs({
        @Arg(column = "turn_id", javaType = String.class),
        @Arg(column = "tenant", javaType = String.class),
        @Arg(column = "subject", javaType = String.class),
        @Arg(column = "epoch", javaType = long.class),
        @Arg(column = "profile_revision", javaType = long.class),
        @Arg(column = "context", javaType = String.class)
    })
    @Select(
            "SELECT p.turn_id,p.tenant,p.subject,p.epoch,p.profile_revision,p.context"
                    + " FROM memory.turn_snapshots p JOIN memory.sources s ON s.id=p.turn_id"
                    + " AND s.tenant=p.tenant AND s.subject=p.subject AND s.epoch=p.epoch"
                    + " AND s.profile_revision=p.profile_revision"
                    + " WHERE p.turn_id=#{turn} AND p.tenant=#{owner.tenant} AND p.subject=#{owner.subject}")
    TurnMemorySnapshot getTurnMemorySnapshot(
            @Param("turn") String turn, @Param("owner") Owner owner);

    String COLUMNS =
            "f.id,f.source_id,f.tenant,f.subject,f.epoch,f.key,f.content,f.evidence,f.revision,f.status";

    @Select(
            "SELECT "
                    + COLUMNS
                    + " FROM memory.facts f JOIN memory.owners o ON o.tenant=f.tenant AND"
                    + " o.subject=f.subject AND o.epoch=f.epoch WHERE f.tenant=#{tenant} AND"
                    + " f.subject=#{subject} AND f.status IN ('active','candidate') AND NOT"
                    + " o.purge_pending ORDER BY f.created_at DESC LIMIT 100")
    List<Fact> listVisibleFacts(Owner owner);

    @Select("SELECT " + COLUMNS + " FROM memory.facts f WHERE id=#{id}")
    Fact getFactById(String id);

    @Select(
            "SELECT EXISTS(SELECT 1 FROM memory.facts f JOIN memory.sources s ON s.id=f.source_id"
                    + " JOIN memory.sources incoming ON incoming.id=#{sourceId} WHERE"
                    + " f.tenant=#{tenant} AND f.subject=#{subject} AND f.key=#{key} AND"
                    + " f.status='active' AND (s.created_at,s.id)>(incoming.created_at,incoming.id))")
    boolean existsNewerActiveFact(Fact fact);

    @Select(
            "SELECT "
                    + COLUMNS
                    + " FROM memory.facts f JOIN memory.owners o ON o.tenant=f.tenant AND"
                    + " o.subject=f.subject AND o.epoch=f.epoch WHERE f.tenant=#{tenant} AND"
                    + " f.subject=#{subject} AND f.status='active' AND NOT o.purge_pending ORDER BY"
                    + " f.id")
    List<Fact> listActiveFacts(Owner owner);

    @Insert(
            "INSERT INTO"
                    + " memory.facts(id,source_id,tenant,subject,epoch,key,content,evidence,status)"
                    + " VALUES(#{id},#{sourceId},#{tenant},#{subject},#{epoch},#{key},#{content},#{evidence},#{status})"
                    + " ON CONFLICT(source_id,key) DO NOTHING")
    int insertFactIfAbsent(Fact fact);

    @Update(
            "UPDATE memory.facts SET status='superseded',revision=revision+1 WHERE tenant=#{tenant}"
                    + " AND subject=#{subject} AND key=#{key} AND status='active' AND id<>#{id}")
    void updateActiveFactsSuperseded(Fact fact);

    @Update("UPDATE memory.facts SET status=#{status},revision=revision+1 WHERE id=#{id}")
    void updateFactStatus(@Param("id") String id, @Param("status") String status);

    @Insert(
            "INSERT INTO memory.jobs(kind,key_id,tenant,subject,epoch,revision,model_revision)"
                    + " SELECT 'index',id,tenant,subject,epoch,revision,#{model} FROM memory.facts"
                    + " WHERE id=#{id} AND status='active' ON"
                    + " CONFLICT(kind,key_id,revision,model_revision) DO UPDATE SET"
                    + " status='pending',next_attempt_at=clock_timestamp() WHERE"
                    + " memory.jobs.status='done'")
    void insertOrRequeueIndexJob(@Param("id") String id, @Param("model") String model);

    /**
     * 在单条 CTE 中跳过已锁任务、领取一个到期任务、更新随机租约 token 与尝试次数。 RETURNING 返回更新后的任务，避免额外读取和旧 token
     * 快照；外层事务提交后才调用模型。
     */
    @Select(
            value =
                    """
                    WITH candidate AS MATERIALIZED (
                      SELECT id FROM memory.jobs
                      WHERE (status='pending' AND next_attempt_at<=clock_timestamp())
                         OR (status='running' AND lease_until<=clock_timestamp())
                      ORDER BY id LIMIT 1 FOR UPDATE SKIP LOCKED
                    )
                    UPDATE memory.jobs j
                    SET status='running',attempts=j.attempts+1,lease_token=#{token},
                        lease_until=clock_timestamp()+interval '60 seconds'
                    FROM candidate c WHERE j.id=c.id
                    RETURNING j.id,j.kind,j.key_id,j.tenant,j.subject,j.epoch,j.revision,
                              j.model_revision,j.status,j.attempts,j.lease_token
                    """,
            affectData = true)
    @Options(flushCache = Options.FlushCachePolicy.TRUE)
    Job updateNextJobLease(String token);

    @Select(
            "SELECT"
                    + " id,kind,key_id,tenant,subject,epoch,revision,model_revision,status,attempts,lease_token"
                    + " FROM memory.jobs WHERE id=#{id} FOR UPDATE")
    Job getJobForUpdate(long id);

    // The caller locks the owner first. Evaluate database time only after the job lock is held.
    /**
     * 调用方先锁 owner，再锁 job，锁后以数据库时间检查租约。 除 token 外核对 owner、epoch、任务类型、实体 ID 和两个版本，拒绝旧 Worker
     * 或伪造任务元数据。
     */
    @Select(
            """
WITH locked AS MATERIALIZED (
  SELECT kind,key_id,tenant,subject,epoch,revision,model_revision,status,lease_token,lease_until
  FROM memory.jobs WHERE id=#{id} FOR UPDATE
)
SELECT EXISTS(SELECT 1 FROM locked
  WHERE status='running' AND lease_token=#{leaseToken}
    AND tenant=#{tenant} AND subject=#{subject} AND epoch=#{epoch}
    AND kind=#{kind} AND key_id=#{keyId} AND revision=#{revision}
    AND model_revision=#{modelRevision} AND lease_until>clock_timestamp())
""")
    @Options(flushCache = Options.FlushCachePolicy.TRUE, useCache = false)
    boolean getJobLeaseLiveForUpdate(Job job);

    /** 仅允许当前 running token 更新任务结果，旧执行不能覆盖重新领取后的新任务。 错误码由应用层限定，不能把模型正文、凭据或传输异常写入 error_code。 */
    @Update(
            "UPDATE memory.jobs SET"
                    + " status=#{status},lease_until=NULL,error_code=#{error},next_attempt_at=clock_timestamp()+interval"
                    + " '30 seconds' WHERE id=#{job.id} AND lease_token=#{job.leaseToken} AND"
                    + " status='running'")
    void updateRunningJobOutcomeByLeaseToken(
            @Param("job") Job job, @Param("status") String status, @Param("error") String error);

    @Update("UPDATE memory.sources SET status='extracted' WHERE id=#{id} AND status='pending'")
    void updatePendingSourceExtracted(String id);

    @Insert(
            "INSERT INTO"
                    + " memory.projections(fact_id,tenant,subject,epoch,revision,model_revision,dimensions,embedding)"
                    + " VALUES(#{fact.id},#{fact.tenant},#{fact.subject},#{fact.epoch},#{fact.revision},#{model},1024,CAST(#{vector}"
                    + " AS real[])) ON CONFLICT(fact_id,model_revision) DO UPDATE SET"
                    + " epoch=excluded.epoch,revision=excluded.revision,model_revision=excluded.model_revision,embedding=excluded.embedding")
    void insertOrUpdateFactProjection(
            @Param("fact") Fact fact, @Param("model") String model, @Param("vector") String vector);

    @Select(
            "SELECT "
                    + COLUMNS
                    + " FROM memory.projections p JOIN memory.facts f ON f.id=p.fact_id AND"
                    + " f.revision=p.revision AND f.epoch=p.epoch JOIN memory.owners o ON"
                    + " o.tenant=f.tenant AND o.subject=f.subject AND o.epoch=f.epoch WHERE"
                    + " p.tenant=#{owner.tenant} AND p.subject=#{owner.subject} AND"
                    + " f.tenant=p.tenant AND f.subject=p.subject AND p.model_revision=#{model} AND"
                    + " f.status='active' AND NOT o.purge_pending ORDER BY CAST(p.embedding AS"
                    + " vector(1024)) <=> CAST(#{vector} AS vector(1024)),f.id LIMIT 16")
    List<Fact> listSimilarActiveFacts(
            @Param("owner") Owner owner,
            @Param("model") String model,
            @Param("vector") String vector);

    @Select(
            "SELECT request_hash,result FROM memory.operations WHERE tenant=#{owner.tenant} AND"
                    + " subject=#{owner.subject} AND key=#{key}")
    Operation getOperationByIdempotencyKey(@Param("owner") Owner owner, @Param("key") String key);

    @Insert(
            "INSERT INTO memory.operations(tenant,subject,key,request_hash,result)"
                    + " VALUES(#{owner.tenant},#{owner.subject},#{key},#{hash},#{result})")
    void insertOperationReceipt(
            @Param("owner") Owner owner,
            @Param("key") String key,
            @Param("hash") String hash,
            @Param("result") String result);

    @Insert(
            "INSERT INTO memory.audit(tenant,subject,epoch,action,target)"
                    + " VALUES(#{owner.tenant},#{owner.subject},#{epoch},#{action},#{target})")
    void insertAuditEntry(
            @Param("owner") Owner owner,
            @Param("epoch") long epoch,
            @Param("action") String action,
            @Param("target") String target);

    @Update(
            "UPDATE memory.owners SET epoch=epoch+1,purge_pending=true,profile_revision=0 WHERE"
                    + " tenant=#{tenant} AND subject=#{subject}")
    void updateOwnerPrivacyForForget(Owner owner);

    @Update(
            "UPDATE memory.sources SET content=NULL,status='forgotten' WHERE tenant=#{tenant} AND"
                    + " subject=#{subject}")
    void updateSourcesForgotten(Owner owner);

    @Update(
            "UPDATE memory.facts SET"
                    + " content=NULL,evidence=NULL,status='forgotten',revision=revision+1 WHERE"
                    + " tenant=#{tenant} AND subject=#{subject}")
    void updateFactsForgotten(Owner owner);

    @Delete("DELETE FROM memory.projections WHERE tenant=#{tenant} AND subject=#{subject}")
    void deleteFactProjections(Owner owner);

    @Delete("DELETE FROM memory.turn_snapshots WHERE tenant=#{tenant} AND subject=#{subject}")
    void deleteTurnSnapshotsByOwner(Owner owner);

    @Delete("DELETE FROM agent_runtime.work_contexts WHERE tenant=#{tenant} AND subject=#{subject}")
    void deleteWorkContextsByOwner(Owner owner);

    @Update(
            "UPDATE memory.jobs SET status='forgotten',lease_token=NULL,lease_until=NULL WHERE"
                    + " tenant=#{tenant} AND subject=#{subject}")
    void updateJobsForgotten(Owner owner);

    /** 锁定一个可完成清除的 owner；仍有 durable forget_request 时保留屏障，不能提前恢复服务。 */
    @Select(
            "SELECT tenant,subject,epoch,purge_pending,profile_revision FROM memory.owners o WHERE"
                    + " purge_pending AND NOT EXISTS (SELECT 1 FROM memory.forget_requests r WHERE"
                    + " r.tenant=o.tenant AND r.subject=o.subject) ORDER BY tenant,subject LIMIT 1 FOR"
                    + " UPDATE SKIP LOCKED")
    @ConstructorArgs({
        @Arg(column = "tenant", javaType = String.class),
        @Arg(column = "subject", javaType = String.class),
        @Arg(column = "epoch", javaType = long.class),
        @Arg(column = "purge_pending", javaType = boolean.class),
        @Arg(column = "profile_revision", javaType = long.class)
    })
    Privacy getNextPurgeOwnerForUpdateSkipLocked();

    /** 只在无待应用遗忘请求时清除 purge_pending；调用方须先完成所有同步擦除与残留核验。 */
    @Update(
            "UPDATE memory.owners o SET purge_pending=false WHERE tenant=#{tenant} AND"
                    + " subject=#{subject} AND purge_pending AND NOT EXISTS (SELECT 1 FROM"
                    + " memory.forget_requests r WHERE r.tenant=o.tenant AND r.subject=o.subject)")
    int updateOwnerPurgeCompleted(Owner owner);
}
