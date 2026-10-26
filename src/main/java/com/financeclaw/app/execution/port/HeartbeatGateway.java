package com.financeclaw.app.execution.port;

import com.financeclaw.domain.Task.Claim;

/**
 * 单个 attempt 的续租短事务边界。实现必须在同一事务中执行原 command → Turn 双锁 SQL， 未命中租约须在事务内抛出 Fenced
 * 并回滚；连接、锁或提交异常必须传播，不能改借业务池重试。 此端口不提供领取、批量续租、权限缓存或模型执行能力。
 */
@FunctionalInterface
public interface HeartbeatGateway {
    /** 返回锁后观察到的 cancelling；正常 false 与失去租约不能混为一值。 */
    boolean renew(Claim claim, long leaseMillis);
}
