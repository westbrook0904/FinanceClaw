package com.financeclaw.infrastructure.governance;

import com.financeclaw.domain.Governance.*;
import java.util.List;
import org.apache.ibatis.annotations.*;

/**
 * 授权快照、交互、能力步骤、Artifact 与报告发布的 SQL 映射。
 *
 * <p>当前 grant 表示可变授权，turn policy 保存受理时的授权版本，二者不能互相替代。 业务写入由调用方的 command fence 事务保护，并与当前 grant
 * 锁一起防止撤权后的写入。 报告实体与幂等回执一次插入；能力步骤仅缓存本地确定性结果，外部发送的不确定性由各自投递边界处理。
 */
@Mapper
public interface GovernanceMapper {
    record Step(String inputHash, String result) {}

    record ChildInvocationRow(
            String rootTurnId,
            String callId,
            String target,
            String targetAgentId,
            String targetReleaseHash,
            String inputHash,
            String scopes,
            String status,
            int modelCalls,
            int toolCalls,
            String tenant,
            String subject,
            String conversationId,
            String releaseHash,
            long epoch,
            long grantRevision,
            String resultHash,
            String result) {}

    /** 子调用只保存身份和计数；隐私来源必须与根 Turn 的 owner 相同，不能借错误关联派生身份。 */
    @Select(
            """
            SELECT c.root_turn_id,c.call_id,c.target,c.target_agent_id,c.target_release_hash,
                   c.input_hash,c.scopes::text AS scopes,c.status,
                   c.model_calls,c.tool_calls,t.tenant,t.subject,t.conversation_id,t.release_hash,
                   s.epoch,p.grant_revision,r.input_hash AS result_hash,r.result
            FROM business.child_invocations c
            JOIN business.turns t ON t.id=c.root_turn_id
            JOIN memory.sources s ON s.id=t.id AND s.tenant=t.tenant AND s.subject=t.subject
            JOIN business.turn_policies p ON p.turn_id=t.id
            LEFT JOIN business.capability_steps r
              ON r.turn_id=c.root_turn_id AND r.namespace='child:' || c.call_id AND r.step='result'
            WHERE c.root_turn_id=#{root} AND c.call_id=#{call}
            """)
    ChildInvocationRow getChildInvocation(@Param("root") String root, @Param("call") String call);

    /** 研究目标和子权限是固定常量，私有 Agent 使用单独入口保存已经校验的冻结目标。 */
    @Insert(
            """
            INSERT INTO business.child_invocations(root_turn_id,call_id,target,input_hash,scopes)
            SELECT t.id,#{call},'market_research',#{hash},'["market:read"]'::jsonb
            FROM business.turns t
            JOIN memory.sources s ON s.id=t.id AND s.tenant=t.tenant AND s.subject=t.subject
            JOIN business.turn_policies p ON p.turn_id=t.id
            WHERE t.id=#{root}
            ON CONFLICT(root_turn_id,call_id) DO NOTHING
            """)
    int insertChildInvocationIfAbsent(
            @Param("root") String root, @Param("call") String call, @Param("hash") String hash);

    /** 保存私有叶子目标的不可变发布身份；owner、根发布和来源仍从父受理记录派生。 */
    @Insert(
            """
            INSERT INTO business.child_invocations(
              root_turn_id,call_id,target,target_agent_id,target_release_hash,input_hash,scopes)
            SELECT t.id,#{call},'private_agent',#{targetAgent},#{targetRelease},#{hash},'[]'::jsonb
            FROM business.turns t
            JOIN memory.sources s ON s.id=t.id AND s.tenant=t.tenant AND s.subject=t.subject
            JOIN business.turn_policies p ON p.turn_id=t.id
            WHERE t.id=#{root}
            ON CONFLICT(root_turn_id,call_id) DO NOTHING
            """)
    int insertPrivateChildInvocationIfAbsent(
            @Param("root") String root,
            @Param("call") String call,
            @Param("hash") String hash,
            @Param("targetAgent") String targetAgent,
            @Param("targetRelease") String targetRelease);

    /** 市场 profile 与 v1 共用身份表，使用固定 scopes 区分；不引入可由调用方扩大的工具权限参数。 */
    @Insert(
            """
            INSERT INTO business.child_invocations(
              root_turn_id,call_id,target,target_agent_id,target_release_hash,input_hash,scopes)
            SELECT t.id,#{call},'private_agent',#{targetAgent},#{targetRelease},#{hash},'["market:read"]'::jsonb
            FROM business.turns t
            JOIN memory.sources s ON s.id=t.id AND s.tenant=t.tenant AND s.subject=t.subject
            JOIN business.turn_policies p ON p.turn_id=t.id
            WHERE t.id=#{root}
            ON CONFLICT(root_turn_id,call_id) DO NOTHING
            """)
    int insertPrivateMarketChildInvocationIfAbsent(
            @Param("root") String root,
            @Param("call") String call,
            @Param("hash") String hash,
            @Param("targetAgent") String targetAgent,
            @Param("targetRelease") String targetRelease);

    @Update(
            """
            UPDATE business.child_invocations SET model_calls=model_calls+1
            WHERE root_turn_id=#{root} AND call_id=#{call} AND input_hash=#{hash}
              AND status='pending' AND #{limit} BETWEEN 1 AND 4 AND model_calls<#{limit}
            """)
    int updatePendingChildModelCountWithinLimit(
            @Param("root") String root,
            @Param("call") String call,
            @Param("hash") String hash,
            @Param("limit") int limit);

    @Update(
            """
            UPDATE business.child_invocations SET tool_calls=tool_calls+1
            WHERE root_turn_id=#{root} AND call_id=#{call} AND input_hash=#{hash}
              AND target='market_research' AND status='pending' AND tool_calls<10
            """)
    int updatePendingChildToolCountWithinLimit(
            @Param("root") String root, @Param("call") String call, @Param("hash") String hash);

    @Update(
            """
            UPDATE business.child_invocations SET tool_calls=tool_calls+1
            WHERE root_turn_id=#{root} AND call_id=#{call} AND input_hash=#{hash}
              AND target='private_agent' AND scopes='["market:read"]'::jsonb
              AND status='pending' AND tool_calls<1
            """)
    int updatePendingPrivateMarketToolCountWithinLimit(
            @Param("root") String root, @Param("call") String call, @Param("hash") String hash);

    /** 与既有 result 步骤写入同事务调用；缺少回执或摘要不同不能把调用单独标为完成。 */
    @Update(
            """
            UPDATE business.child_invocations c SET status='completed'
            WHERE c.root_turn_id=#{root} AND c.call_id=#{call} AND c.input_hash=#{hash}
              AND c.status='pending'
              AND EXISTS (
                SELECT 1 FROM business.capability_steps r
                WHERE r.turn_id=c.root_turn_id AND r.namespace='child:' || c.call_id
                  AND r.step='result' AND r.input_hash=c.input_hash)
            """)
    int updatePendingChildInvocationCompleted(
            @Param("root") String root, @Param("call") String call, @Param("hash") String hash);

    @Select(
            "SELECT EXISTS(SELECT 1 FROM business.capability_steps WHERE turn_id=#{root} AND"
                    + " namespace=#{namespace})")
    boolean existsCapabilitySteps(@Param("root") String root, @Param("namespace") String namespace);

    /** 最多十个已提交行情回执；多取一条用于识别损坏数据，不重新执行或从 SDK 文本推断证据。 */
    @Select(
            "SELECT result FROM business.capability_steps WHERE turn_id=#{root}"
                    + " AND namespace=#{namespace} AND step LIKE 'tool:%' ORDER BY step LIMIT 11")
    List<String> listChildSnapshotResults(
            @Param("root") String root, @Param("namespace") String namespace);

    @Update("UPDATE business.artifacts SET required_scope=#{scope} WHERE id=#{id}")
    int updateArtifactScope(@Param("id") String id, @Param("scope") String scope);

    @Select("SELECT required_scope FROM business.artifacts WHERE id=#{id}")
    String getArtifactScope(String id);

    @Select("SELECT required_scope,privacy_bound FROM business.artifacts WHERE id=#{id}")
    com.financeclaw.app.artifact.port.ArtifactGateway.Access getArtifactAccess(String id);

    @Select(
            "SELECT input_hash,result FROM business.capability_steps WHERE turn_id=#{turn} AND"
                    + " namespace=#{namespace} AND step=#{step}")
    Step getCapabilityStep(
            @Param("turn") String turn,
            @Param("namespace") String namespace,
            @Param("step") String step);

    @Insert(
            "INSERT INTO business.capability_steps(turn_id,namespace,step,input_hash,result)"
                    + " VALUES(#{turn},#{namespace},#{step},#{hash},#{result})")
    int insertCapabilityStep(
            @Param("turn") String turn,
            @Param("namespace") String namespace,
            @Param("step") String step,
            @Param("hash") String hash,
            @Param("result") String result);

    @Insert(
            "INSERT INTO business.grants(conversation_id,scopes) VALUES(#{id},CAST(#{scopes} AS"
                    + " jsonb)) ON CONFLICT DO NOTHING")
    int insertGrantIfAbsent(@Param("id") String id, @Param("scopes") String scopes);

    /** 锁定当前会话授权，业务写入与撤权按此锁串行；turn policy 仍需另行核对版本。 */
    @Select(
            "SELECT conversation_id,revision,scopes::text FROM business.grants WHERE"
                    + " conversation_id=#{id} FOR UPDATE")
    Grant getGrantForUpdate(String id);

    @Select(
            "SELECT conversation_id,revision,scopes::text FROM business.grants WHERE"
                    + " conversation_id=#{id}")
    Grant getGrant(String id);

    @Update(
            "UPDATE business.grants SET revision=revision+1,scopes=CAST(#{scopes} AS jsonb) WHERE"
                    + " conversation_id=#{id}")
    int updateGrant(@Param("id") String id, @Param("scopes") String scopes);

    /** 一次读取幂等请求摘要及当时授权快照，避免回放时再读可变的当前 grant。 */
    @Select(
            "SELECT request_hash,conversation_id,revision,scopes::text FROM business.grant_changes"
                    + " WHERE conversation_id=#{id} AND idempotency_key=#{key}")
    com.financeclaw.domain.governance.GrantReceipt getGrantReceipt(
            @Param("id") String id, @Param("key") String key);

    @Insert(
            "INSERT INTO"
                    + " business.grant_changes(conversation_id,idempotency_key,request_hash,revision,scopes)"
                    + " SELECT conversation_id,#{key},#{hash},revision,scopes FROM business.grants"
                    + " WHERE conversation_id=#{id}")
    int insertGrantChange(
            @Param("id") String id, @Param("key") String key, @Param("hash") String hash);

    @Insert(
            "INSERT INTO business.turn_policies(turn_id,grant_revision,scopes)"
                    + " VALUES(#{turn},#{revision},CAST(#{scopes} AS jsonb))")
    int insertTurnPolicy(
            @Param("turn") String turn,
            @Param("revision") long revision,
            @Param("scopes") String scopes);

    @Select(
            "SELECT turn_id,grant_revision,scopes::text,model_calls,tool_calls FROM"
                    + " business.turn_policies WHERE turn_id=#{turn}")
    Policy getTurnPolicy(String turn);

    /** 在 SQL 中原子预留一次模型预算；返回 0 表示预算已满，不能先查后加。 */
    @Update(
            "UPDATE business.turn_policies SET model_calls=model_calls+1 WHERE turn_id=#{id} AND"
                    + " model_calls<#{max}")
    int updateModelCallCountWithinLimit(@Param("id") String id, @Param("max") int max);

    /** 在 SQL 中原子预留一次工具预算；参数校验和授权通过后才调用，影响行数必须为 1。 */
    @Update(
            "UPDATE business.turn_policies SET tool_calls=tool_calls+1 WHERE turn_id=#{id} AND"
                    + " tool_calls<#{max}")
    int updateToolCallCountWithinLimit(@Param("id") String id, @Param("max") int max);

    @Insert(
            """
INSERT INTO business.interactions(id,turn_id,command_id,attempt_id,state_version,grant_revision,release_hash,kind,call_id,tool,action_hash,schema_json,question)
VALUES(#{id},#{turnId},#{commandId},#{attemptId},#{stateVersion},#{grantRevision},#{releaseHash},#{kind},#{callId},#{tool},#{actionHash},#{schema},#{question})
""")
    int insertInteraction(Interaction i);

    String INTERACTION_COLUMNS =
            "id,turn_id,command_id,attempt_id,state_version,grant_revision,release_hash,kind,call_id,tool,action_hash,schema_json"
                    + " AS schema,question,revision,status,expires_at>clock_timestamp() AS"
                    + " live,response_hash,response_json AS response";

    @Select("SELECT " + INTERACTION_COLUMNS + " FROM business.interactions WHERE id=#{id}")
    Interaction getInteractionById(String id);

    /** 锁定人工交互后才能接受答案；应用层还需核对 owner、revision、检查点与有效期。 */
    @Select(
            "SELECT "
                    + INTERACTION_COLUMNS
                    + " FROM business.interactions WHERE id=#{id} FOR UPDATE")
    Interaction getInteractionForUpdate(String id);

    @Select(
            "SELECT "
                    + INTERACTION_COLUMNS
                    + " FROM business.interactions WHERE turn_id=#{id} ORDER BY revision,id")
    List<Interaction> listInteractionsByTurnId(String id);

    /**
     * 通知只需当前 pending 行；通用历史读取保留给批次回答判断及执行恢复。 不在此筛除过期 pending，也不改变 live 的数据库时间表达式或原排序，渲染器仍须检查状态与有效期。
     */
    @Select(
            "SELECT "
                    + INTERACTION_COLUMNS
                    + " FROM business.interactions WHERE turn_id=#{id} AND status='pending'"
                    + " ORDER BY revision,id")
    List<Interaction> listPendingInteractionsByTurnId(String id);

    @Update(
            "UPDATE business.interactions SET"
                    + " status='answered',response_hash=#{hash},response_json=#{json} WHERE id=#{id}"
                    + " AND status='pending'")
    int updatePendingInteractionAnswer(
            @Param("id") String id, @Param("hash") String hash, @Param("json") String json);

    @Update(
            "UPDATE business.interactions SET status='invalid' WHERE turn_id=#{id} AND"
                    + " status='pending'")
    int updatePendingInteractionsInvalid(String id);

    /** 仅把已结束的挂起命令重新入队，清除旧 attempt 与租约。返回 0 表示状态已改变，不能强行重放。 */
    @Update(
            "UPDATE business.commands SET"
                    + " status='queued',resume_id=#{interaction},attempt_id=NULL,lease_until=NULL WHERE"
                    + " id=#{command} AND status='done'")
    int updateDoneCommandForResume(
            @Param("command") String command, @Param("interaction") String interaction);

    @Insert(
            "INSERT INTO"
                    + " business.artifacts(id,turn_id,conversation_id,tenant,subject,release_hash,hash,payload,bytes)"
                    + " VALUES(#{id},#{turnId},#{conversationId},#{tenant},#{subject},#{releaseHash},#{hash},#{payload},#{bytes})")
    int insertArtifact(Artifact artifact);

    /** 调用方持执行栅栏、隐私和授权锁；工具正文与其读取边界必须同一条语句提交。 */
    @Insert(
            "INSERT INTO business.artifacts(id,turn_id,conversation_id,tenant,subject,release_hash,hash,payload,bytes,required_scope,privacy_bound)"
                    + " VALUES(#{artifact.id},#{artifact.turnId},#{artifact.conversationId},#{artifact.tenant},#{artifact.subject},#{artifact.releaseHash},#{artifact.hash},#{artifact.payload},#{artifact.bytes},#{scope},true)")
    int insertToolResultArtifact(
            @Param("artifact") Artifact artifact, @Param("scope") String requiredScope);

    @Select(
            "SELECT id,turn_id,conversation_id,tenant,subject,release_hash,hash,payload,bytes FROM"
                    + " business.artifacts WHERE id=#{id}")
    Artifact getArtifactById(String id);

    /** 读取同一 turn/call 的原子报告回执；只有 actionHash 一致才能返回原始 result 字节。 */
    @Select(
            "SELECT action_hash,result FROM business.reports WHERE turn_id=#{turn} AND"
                    + " call_id=#{call}")
    com.financeclaw.domain.report.ReportReceipt getReportReceipt(
            @Param("turn") String turn, @Param("call") String call);

    /**
     * 一条 INSERT 同时发布报告实体和幂等结果，调用方持 command fence 与授权锁。 唯一键约束阻止同一 turn/call
     * 重复发布；失败时整个事务回滚，禁止生成第二个报告补偿未知结果。
     */
    @Insert(
            "INSERT INTO business.reports(id,turn_id,call_id,title,action_hash,result)"
                    + " VALUES(#{id},#{turn},#{call},#{title},#{hash},#{result})")
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
