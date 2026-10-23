package com.financeclaw.infrastructure.state;

import org.apache.ibatis.annotations.*;

/**
 * AgentScope 完整状态快照的版本写入与墓碑映射。
 *
 * <p>namespace、user、session 和 state_key 共同定位状态；scope 的身份校验由 MyBatisAgentStateStore 先执行。更新必须命中
 * expected version；冲突返回 0，禁止覆盖 其他执行尝试提交的状态。删除保留递增版本的墓碑，避免已删除状态被当成新会话恢复。
 */
@Mapper
public interface RuntimeMapper {
    record StateKey(String namespace, String user, String session, String key) {}

    record StateRow(String payload, long version, boolean deleted) {}

    record WorkRow(String payload, String transcript) {}

    /** 只读取同 owner/隐私 epoch/发布/授权的已封存完成上下文，不从活动 attempt 猜测会话结果。 */
    @Select(
            """
            SELECT w.payload::text,w.transcript::text FROM agent_runtime.work_contexts w
            JOIN business.turns t ON t.id=w.turn_id AND t.status='completed'
            JOIN business.attempts a ON a.id=w.attempt_id AND a.outcome='completed'
              AND a.state_version=w.state_version
            JOIN business.commands c ON c.id=a.command_id AND c.turn_id=t.id
            JOIN memory.sources s ON s.id=w.turn_id AND s.tenant=w.tenant AND s.subject=w.subject
              AND s.epoch=w.epoch AND s.status<>'forgotten'
            WHERE w.conversation_id=#{claim.turn.conversationId}
              AND w.tenant=#{claim.turn.tenant} AND w.subject=#{claim.turn.subject}
              AND t.conversation_id=w.conversation_id AND t.tenant=w.tenant AND t.subject=w.subject
              AND t.release_hash=w.release_hash
              AND w.release_hash=#{claim.turn.releaseHash} AND w.grant_revision=#{grantRevision}
              AND w.epoch=COALESCE((SELECT epoch FROM memory.sources WHERE id=#{claim.turn.id}),0)
            ORDER BY t.created_at DESC,t.id DESC LIMIT 1
            """)
    WorkRow getLatestCompletedWorkContext(
            @Param("claim") com.financeclaw.domain.Task.Claim claim,
            @Param("grantRevision") long grantRevision);

    /** 恢复同一 Turn 的未压缩执行轨迹；调用方已经验证真实交互及原生检查点。 */
    @Select(
            """
            SELECT payload::text,transcript::text FROM agent_runtime.work_contexts
            WHERE turn_id=#{turn.id} AND tenant=#{turn.tenant} AND subject=#{turn.subject}
              AND attempt_id=#{attempt} AND state_version=#{version}
              AND epoch=COALESCE((SELECT epoch FROM memory.sources WHERE id=#{turn.id}),0)
            """)
    WorkRow getTurnWorkContext(
            @Param("turn") com.financeclaw.domain.Task.Turn turn,
            @Param("attempt") String attempt,
            @Param("version") long version);

    /** 和 SDK CAS 写入共用执行栅栏事务；每个 Turn 只维护一份投影，轨迹只包含该 Turn 的消息。 */
    @Insert(
            """
            INSERT INTO agent_runtime.work_contexts
              (turn_id,conversation_id,tenant,subject,epoch,release_hash,grant_revision,
               attempt_id,state_version,payload,transcript)
            VALUES(#{claim.turn.id},#{claim.turn.conversationId},#{claim.turn.tenant},
              #{claim.turn.subject},COALESCE((SELECT epoch FROM memory.sources WHERE id=#{claim.turn.id}),0),
              #{claim.turn.releaseHash},#{grantRevision},#{claim.command.attemptId},#{version},
              CAST(#{payload} AS jsonb),CAST(#{transcript} AS jsonb))
            ON CONFLICT(turn_id) DO UPDATE SET attempt_id=EXCLUDED.attempt_id,
              state_version=EXCLUDED.state_version,payload=EXCLUDED.payload,
              transcript=EXCLUDED.transcript,updated_at=clock_timestamp()
            """)
    void saveTurnWorkContext(
            @Param("claim") com.financeclaw.domain.Task.Claim claim,
            @Param("grantRevision") long grantRevision,
            @Param("version") long version,
            @Param("payload") String payload,
            @Param("transcript") String transcript);

    /** 按完整 scope 键读取快照及墓碑；deleted 不能被解释为普通不存在，否则会复活删除的执行。 */
    @Select(
            """
SELECT payload::text, version, deleted FROM agent_runtime.states
WHERE namespace=#{namespace} AND user_id=#{user} AND session_id=#{session} AND state_key=#{key}
""")
    StateRow getState(StateKey id);

    /** 仅在 expected version 为 0 时尝试创建版本 1；唯一键冲突返回 0，不覆盖既有状态或墓碑。 */
    @Insert(
            """
INSERT INTO agent_runtime.states(namespace,user_id,session_id,state_key,version,payload)
VALUES (#{id.namespace},#{id.user},#{id.session},#{id.key},1,CAST(#{json} AS jsonb))
ON CONFLICT DO NOTHING
""")
    int insertStateIfAbsent(@Param("id") StateKey id, @Param("json") String json);

    /** 仅更新指定版本且未删除的快照，并推进版本。返回 0 表示版本冲突或墓碑，SDK 应按 FAIL 策略停止。 */
    @Update(
            """
            UPDATE agent_runtime.states SET payload=CAST(#{json} AS jsonb),version=version+1
            WHERE namespace=#{id.namespace} AND user_id=#{id.user} AND session_id=#{id.session}
              AND state_key=#{id.key} AND version=#{expected} AND NOT deleted
            """)
    int updateStateByExpectedVersion(
            @Param("id") StateKey id, @Param("json") String json, @Param("expected") long expected);

    /** 保留完整 scope 键并递增版本，擦除 payload；对不存在的键也建立墓碑以阻止旧写入创建。 */
    @Insert(
            """
INSERT INTO agent_runtime.states(namespace,user_id,session_id,state_key,version,payload,deleted)
VALUES (#{namespace},#{user},#{session},#{key},1,NULL,TRUE)
ON CONFLICT(namespace,user_id,session_id,state_key)
DO UPDATE SET payload=NULL,deleted=TRUE,version=agent_runtime.states.version+1
""")
    int insertOrUpdateStateTombstone(StateKey id);
}
