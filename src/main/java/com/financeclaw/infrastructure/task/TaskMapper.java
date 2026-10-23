package com.financeclaw.infrastructure.task;

import com.financeclaw.domain.Task.*;
import java.util.List;
import org.apache.ibatis.annotations.*;

/**
 * 任务受理、命令租约、业务事件及执行回执的 PostgreSQL 映射。
 *
 * <p>事务由应用层统一管理。领取和恢复按 command → turn 的顺序加锁；外部入口先验证 owner，再允许内部按 ID 读取。FOR SHARE
 * 允许同用户的独立会话并发受理，暂停操作使用 FOR UPDATE 等待已受理事务提交。方法返回的影响行数与可空值均属于并发控制契约。
 */
@Mapper
public interface TaskMapper {
    @Insert(
            "INSERT INTO business.admission(tenant,subject) VALUES(#{tenant},#{subject}) ON"
                    + " CONFLICT DO NOTHING")
    void insertAdmissionIfAbsent(Owner owner);

    /**
     * 共享锁一直保留到受理事务结束，允许独立会话并发，但阻止暂停操作越过未提交受理。
     *
     * @return 当前暂停状态；调用前必须确保 owner 的 admission 行已存在
     */
    @Select(
            "SELECT paused FROM business.admission WHERE tenant=#{tenant} AND subject=#{subject}"
                    + " FOR SHARE")
    boolean getAdmissionPausedForShare(Owner owner);

    @Select(
            "SELECT paused,revision FROM business.admission WHERE tenant=#{tenant} AND"
                    + " subject=#{subject}")
    com.financeclaw.domain.Operations.Admission getAdmission(Owner owner);

    @Select(
            "SELECT paused,revision FROM business.admission WHERE tenant=#{tenant} AND"
                    + " subject=#{subject} FOR UPDATE")
    com.financeclaw.domain.Operations.Admission getAdmissionForUpdate(Owner owner);

    @Select(
            "SELECT request_hash,paused,revision FROM business.admission_changes WHERE"
                    + " tenant=#{owner.tenant} AND subject=#{owner.subject} AND idempotency_key=#{key}")
    com.financeclaw.domain.Operations.Change getAdmissionChangeByIdempotencyKey(
            @Param("owner") Owner owner, @Param("key") String key);

    /** 以期望 revision 更新暂停状态并推进版本。返回 0 表示并发修改，调用方应拒绝旧版本。 */
    @Update(
            "UPDATE business.admission SET paused=#{paused},revision=revision+1 WHERE"
                    + " tenant=#{owner.tenant} AND subject=#{owner.subject} AND revision=#{revision}")
    int updateAdmissionByRevision(
            @Param("owner") Owner owner,
            @Param("paused") boolean paused,
            @Param("revision") long revision);

    @Insert(
            "INSERT INTO"
                    + " business.admission_changes(tenant,subject,idempotency_key,request_hash,paused,revision)"
                    + " VALUES(#{owner.tenant},#{owner.subject},#{key},#{hash},#{paused},#{revision})")
    void insertAdmissionChange(
            @Param("owner") Owner owner,
            @Param("key") String key,
            @Param("hash") String hash,
            @Param("paused") boolean paused,
            @Param("revision") long revision);

    @Select(
            "SELECT EXISTS(SELECT 1 FROM business.admission LIMIT 1) OR NOT EXISTS(SELECT 1 FROM"
                    + " business.admission LIMIT 1)")
    boolean isAdmissionTableReadable();

    @Insert(
            "INSERT INTO business.conversations(id,tenant,subject)"
                    + " VALUES(#{id},#{owner.tenant},#{owner.subject})")
    int insertConversation(@Param("id") String id, @Param("owner") Owner owner);

    /** 调用方先锁定并验证会话 owner；未绑定的历史空会话返回 null。 */
    @Select("SELECT release_hash FROM business.conversations WHERE id=#{id}")
    String getConversationRelease(String id);

    /** 会话创建或持有会话行锁时首次绑定版本，永不覆盖已经选定的 release。 */
    @Update(
            "UPDATE business.conversations SET release_hash=#{hash} WHERE id=#{id} AND release_hash IS NULL")
    int updateConversationReleaseIfAbsent(@Param("id") String id, @Param("hash") String hash);

    @Select(
            "SELECT id FROM business.conversations WHERE id=#{id} AND tenant=#{owner.tenant} AND"
                    + " subject=#{owner.subject} FOR UPDATE")
    String getOwnedConversationForUpdate(@Param("id") String id, @Param("owner") Owner owner);

    @Select(
            "SELECT id FROM business.conversations WHERE id=#{id} AND tenant=#{owner.tenant} AND"
                    + " subject=#{owner.subject}")
    String getOwnedConversation(@Param("id") String id, @Param("owner") Owner owner);

    String TURN_COLUMNS =
            "id,conversation_id,tenant,subject,idempotency_key,request_hash,message,release_hash,status,revision,result,reason";

    @Select("SELECT " + TURN_COLUMNS + " FROM business.turns WHERE id=#{id}")
    Turn getTurnById(String id);

    @Select(
            "SELECT "
                    + TURN_COLUMNS
                    + " FROM business.turns WHERE id=#{turn} AND conversation_id=#{conversation}"
                    + " AND tenant=#{owner.tenant} AND subject=#{owner.subject}")
    Turn getOwnedTurn(
            @Param("owner") Owner owner,
            @Param("conversation") String conversation,
            @Param("turn") String turn);

    @Select("SELECT " + TURN_COLUMNS + " FROM business.turns WHERE id=#{id} FOR UPDATE")
    Turn getTurnForUpdate(String id);

    @Select(
            "SELECT "
                    + TURN_COLUMNS
                    + " FROM business.turns WHERE conversation_id=#{conversation} AND"
                    + " idempotency_key=#{key}")
    Turn getTurnByIdempotencyKey(
            @Param("conversation") String conversation, @Param("key") String key);

    @Select(
            "SELECT EXISTS(SELECT 1 FROM business.turns WHERE conversation_id=#{id} AND status IN"
                    + " ('queued','resuming','running','waiting','cancelling','blocked'))")
    boolean existsActiveTurnByConversation(String id);

    @Insert(
            """
INSERT INTO business.turns(id,conversation_id,tenant,subject,idempotency_key,request_hash,message,release_hash,status)
VALUES(#{id},#{conversationId},#{tenant},#{subject},#{idempotencyKey},#{requestHash},#{message},#{releaseHash},'queued')
""")
    int insertTurn(Turn turn);

    @Insert("INSERT INTO business.commands(id,turn_id,status) VALUES(#{id},#{turn},'queued')")
    int insertCommand(@Param("id") String id, @Param("turn") String turn);

    String COMMAND_COLUMNS =
            "id,turn_id,status,attempt_id,fence,COALESCE(lease_until>clock_timestamp(),false) AS"
                    + " live,resume_id";

    /** 按入队顺序锁定一个可领取命令；跳过其他 Worker 已锁行，不等待它们的模型调用。 领取、写 attempt 和 turn 迁移必须在同一事务完成。 */
    @Select(
            "SELECT "
                    + COMMAND_COLUMNS
                    + " FROM business.commands WHERE status='queued' ORDER BY created_at,id LIMIT 1"
                    + " FOR UPDATE SKIP LOCKED")
    Command getNextQueuedCommandForUpdateSkipLocked();

    @Select("SELECT " + COMMAND_COLUMNS + " FROM business.commands WHERE id=#{id} FOR UPDATE")
    Command getCommandForUpdate(String id);

    @Select("SELECT " + COMMAND_COLUMNS + " FROM business.commands WHERE turn_id=#{id} FOR UPDATE")
    Command getCommandByTurnIdForUpdate(String id);

    @Select(
            "SELECT id FROM business.commands WHERE status='running' AND"
                    + " lease_until<=clock_timestamp() ORDER BY lease_until LIMIT 50")
    List<String> listExpiredCommandIds();

    /** 调用方已经锁定可领取命令和 turn 后，写入新 attempt、递增 fence 并建立租约。 本方法不是独立的无锁 CAS，不能脱离领取事务调用。 */
    @Update(
            """
UPDATE business.commands SET status='running',attempt_id=#{attempt},fence=fence+1,
worker_id=#{worker},lease_until=clock_timestamp()+#{lease}*interval '1 millisecond' WHERE id=#{id}
""")
    int updateCommandClaim(
            @Param("id") String id,
            @Param("attempt") String attempt,
            @Param("worker") String worker,
            @Param("lease") long lease);

    @Insert(
            "INSERT INTO business.attempts(id,command_id,fence,resume_id)"
                    + " VALUES(#{attemptId},#{id},#{fence},#{resumeId})")
    int insertAttempt(Command command);

    // Materialized dependencies preserve command-before-Turn lock order. The expiry predicate
    // reads locked_turn, so clock_timestamp() runs only after both row locks have been acquired.
    /**
     * 先锁 command，再锁其 turn；锁后才用数据库时钟校验原租约，禁止复活已过期执行。 同时核对 command/turn/attempt/fence，RETURNING
     * 用于携带取消状态，不能改成普通 SELECT。
     *
     * @return null 表示未续期且应 fence；false 表示续期成功；true 表示续期成功且 turn 正在取消
     */
    @Select(
            value =
                    """
                    WITH locked_command AS MATERIALIZED (
                      SELECT id,turn_id,status,attempt_id,fence,lease_until
                      FROM business.commands WHERE id=#{claim.command.id} FOR UPDATE
                    ), locked_turn AS MATERIALIZED (
                      SELECT t.id,t.status,c.id AS command_id,c.lease_until
                      FROM business.turns t JOIN locked_command c ON t.id=c.turn_id
                      WHERE c.turn_id=#{claim.turn.id} AND c.turn_id=#{claim.command.turnId}
                        AND c.status='running' AND c.attempt_id=#{claim.command.attemptId}
                        AND c.fence=#{claim.command.fence}
                      FOR UPDATE OF t
                    )
                    UPDATE business.commands c
                    SET lease_until=clock_timestamp()+#{lease}*interval '1 millisecond'
                    FROM locked_turn t
                    WHERE c.id=t.command_id AND t.lease_until>clock_timestamp()
                    RETURNING t.status='cancelling'
                    """,
            affectData = true)
    @Options(flushCache = Options.FlushCachePolicy.TRUE, useCache = false)
    Boolean updateCommandLeaseAndGetCancellation(
            @Param("claim") Claim claim, @Param("lease") long lease);

    @Update(
            "UPDATE business.commands SET status='done',lease_until=NULL,fence=fence+1 WHERE"
                    + " id=#{id}")
    int updateCommandDone(String id);

    @Update(
            "UPDATE business.turns SET"
                    + " status=#{status},revision=revision+1,result=#{result},reason=#{reason} WHERE"
                    + " id=#{id}")
    int updateTurnState(
            @Param("id") String id,
            @Param("status") String status,
            @Param("result") String result,
            @Param("reason") String reason);

    /**
     * 在业务迁移事务内持久化当前 revision 的事件并返回已写事件。 虽然使用 MyBatis Select 映射 RETURNING，但它会写数据库，必须保留
     * affectData/flushCache。
     */
    @Select(
            value =
                    "INSERT INTO business.events(turn_id,revision,status,reason) SELECT"
                            + " id,revision,status,reason FROM business.turns WHERE id=#{id} RETURNING"
                            + " turn_id,revision,status,reason,kind,text,tool",
            affectData = true)
    @Options(flushCache = Options.FlushCachePolicy.TRUE)
    Event insertTurnEventAndReturn(String id);

    @Select(
            "SELECT turn_id,revision,status,reason,kind,text,tool FROM business.events WHERE"
                    + " turn_id=#{id} AND revision>#{after} ORDER BY revision LIMIT 100")
    List<Event> listTurnEventsAfterRevision(@Param("id") String id, @Param("after") long after);

    @Insert(
            "INSERT INTO business.journal(conversation_id,turn_id,role,content)"
                    + " VALUES(#{conversation},#{turn},#{role},#{content}) ON CONFLICT DO NOTHING")
    int insertJournalEntryIfAbsent(
            @Param("conversation") String conversation,
            @Param("turn") String turn,
            @Param("role") String role,
            @Param("content") String content);

    @Select(
            "SELECT sequence,conversation_id,turn_id,role,content FROM business.journal WHERE"
                    + " conversation_id=#{conversation} AND sequence>#{after} ORDER BY sequence LIMIT"
                    + " #{limit}")
    List<Journal> listJournalEntriesAfterSequence(
            @Param("conversation") String conversation,
            @Param("after") long after,
            @Param("limit") int limit);

    /**
     * 只封存尚无 outcome 的 attempt；同一回执不得被重复执行或恢复覆盖。
     *
     * @return 1 表示首次封存，0 表示回执已存在，由上层执行一致性检查
     */
    @Update(
            "UPDATE business.attempts SET"
                    + " state_version=#{value.stateVersion},outcome=#{value.status},result=#{value.result}"
                    + " WHERE id=#{id} AND outcome IS NULL")
    int updateAttemptOutcomeIfUnsealed(@Param("id") String id, @Param("value") Outcome value);

    @Select(
            "SELECT id,command_id,fence,state_version,outcome,result FROM business.attempts WHERE"
                    + " id=#{id}")
    Receipt getAttemptReceipt(String id);

    @Select(
            "SELECT t.conversation_id,c.turn_id,c.status,c.revision,true AS idempotent_replay FROM"
                    + " business.cancel_requests c JOIN business.turns t ON t.id=c.turn_id WHERE"
                    + " c.turn_id=#{turn} AND c.idempotency_key=#{key}")
    Accepted getCancelReceiptByIdempotencyKey(@Param("turn") String turn, @Param("key") String key);

    @Insert(
            "INSERT INTO business.cancel_requests(turn_id,idempotency_key,status,revision)"
                    + " VALUES(#{turn},#{key},#{status},#{revision})")
    int insertCancelReceipt(
            @Param("turn") String turn,
            @Param("key") String key,
            @Param("status") String status,
            @Param("revision") long revision);

    /** 有界的历史观察查询；记忆层再过滤 owner/epoch。模型运行时使用持久工作投影，不调用本查询拼提示。 */
    @Select(
            "SELECT "
                    + TURN_COLUMNS
                    + " FROM business.turns WHERE conversation_id=#{conversation} AND"
                    + " status='completed' AND release_hash=#{release} AND id IN (SELECT turn_id"
                    + " FROM business.turn_policies WHERE grant_revision=#{revision}) ORDER BY"
                    + " created_at DESC,id DESC LIMIT 8")
    List<Turn> listCompletedHistoryByReleaseAndGrant(
            @Param("conversation") String conversation,
            @Param("release") String release,
            @Param("revision") long revision);

    @Select(
            "SELECT EXISTS(SELECT 1 FROM business.artifacts LIMIT 1) OR NOT EXISTS(SELECT 1 FROM"
                    + " business.artifacts LIMIT 1)")
    boolean isArtifactTableReadable();

    @Select(
            "SELECT EXISTS(SELECT 1 FROM agent_runtime.states LIMIT 1) OR NOT EXISTS(SELECT 1 FROM"
                    + " agent_runtime.states LIMIT 1)")
    boolean isRuntimeStateTableReadable();
}
