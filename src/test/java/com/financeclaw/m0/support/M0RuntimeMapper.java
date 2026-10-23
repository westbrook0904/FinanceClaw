package com.financeclaw.m0.support;

import org.apache.ibatis.annotations.*;

/** Test-only M0 effect reconciliation and session advisory locks. */
public interface M0RuntimeMapper {
    record EffectRow(String requestHash, String status, String resultJson) {}

    @Insert(
            """
      INSERT INTO agent_runtime.tool_effects(scope_key,operation_id,request_hash,status)
      VALUES (#{scope},#{operation},#{hash},'pending') ON CONFLICT DO NOTHING
      """)
    int beginEffect(
            @Param("scope") String scope,
            @Param("operation") String operation,
            @Param("hash") String hash);

    @Select(
            """
      SELECT request_hash,status,result_json::text FROM agent_runtime.tool_effects
      WHERE scope_key=#{scope} AND operation_id=#{operation}
      """)
    EffectRow findEffect(@Param("scope") String scope, @Param("operation") String operation);

    @Update(
            """
      UPDATE agent_runtime.tool_effects SET status='completed',result_json=CAST(#{json} AS jsonb)
      WHERE scope_key=#{scope} AND operation_id=#{operation} AND request_hash=#{hash} AND status='pending'
      """)
    int completeEffect(
            @Param("scope") String scope,
            @Param("operation") String operation,
            @Param("hash") String hash,
            @Param("json") String json);

    @Select(
            "SELECT EXISTS(SELECT 1 FROM agent_runtime.tool_effects WHERE scope_key=#{scope} AND status='pending')")
    boolean hasPendingEffect(String scope);

    @Select("SELECT pg_try_advisory_lock(#{key})")
    @Options(useCache = false, flushCache = Options.FlushCachePolicy.TRUE)
    boolean tryLock(long key);

    @Select("SELECT pg_advisory_unlock(#{key})")
    @Options(useCache = false, flushCache = Options.FlushCachePolicy.TRUE)
    boolean unlock(long key);
}
