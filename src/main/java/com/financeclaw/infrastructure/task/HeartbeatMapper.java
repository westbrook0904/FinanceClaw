package com.financeclaw.infrastructure.task;

import com.financeclaw.domain.Task.Claim;
import org.apache.ibatis.annotations.Options;
import org.apache.ibatis.annotations.Param;
import org.apache.ibatis.annotations.Select;

/** 私有 SqlSessionFactory 只注册这一条续租语句；与普通 mapper 共享原 SQL 字节，不复制锁与 fencing 条件。 */
public interface HeartbeatMapper {
    @Select(value = TaskMapper.RENEW_COMMAND_LEASE, affectData = true)
    @Options(flushCache = Options.FlushCachePolicy.TRUE, useCache = false)
    Boolean updateCommandLeaseAndGetCancellation(
            @Param("claim") Claim claim, @Param("lease") long lease);
}
