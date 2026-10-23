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
