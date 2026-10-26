package com.financeclaw.app.runtime;

import com.financeclaw.app.runtime.port.WorkerRegistryGateway;
import com.financeclaw.domain.Governance.Blocked;
import com.financeclaw.domain.runtime.RuntimeWorker;
import java.util.List;
import java.util.UUID;

/** 同一 Command worker UUID 的有期注册，不领取命令、不推进业务状态，也不替代 Command 租约。 */
public final class WorkerRegistry {
    public static final int PROTOCOL_VERSION = 1;
    private final WorkerRegistryGateway db;
    private final String runtimeHash;
    private final long ttlMillis;

    public WorkerRegistry(WorkerRegistryGateway db, String runtimeHash, long ttlMillis) {
        if (runtimeHash == null
                || !runtimeHash.matches("[a-f0-9]{64}")
                || ttlMillis < 1000
                || ttlMillis > 300000)
            throw new IllegalArgumentException("invalid_worker_registry_limits");
        this.db = db;
        this.runtimeHash = runtimeHash;
        this.ttlMillis = ttlMillis;
    }

    /** 仅供worker保守的本机截止检查；数据库过期与command fence仍是权威。 */
    public long ttlMillis() {
        return ttlMillis;
    }

    public void register(String worker, int parallelism) {
        UUID.fromString(worker);
        if (parallelism < 1 || parallelism > 32)
            throw new IllegalArgumentException("invalid_worker_parallelism");
        db.register(worker, runtimeHash, parallelism, ttlMillis);
        var current = requireEligible(worker, "agentscope");
        // 注册提交结果不确定时，重试可能命中已有记录；成功返回必须实际刷新过有效期，
        // 不能把旧记录的剩余TTL当成从这次调用开始的一整个TTL。
        if (current.parallelism() != parallelism
                || !db.heartbeat(worker, runtimeHash, 0, ttlMillis))
            throw new Blocked("worker_unavailable");
    }

    public boolean heartbeat(String worker, int active) {
        if (active < 0 || active > 32)
            throw new IllegalArgumentException("invalid_active_attempts");
        return db.heartbeat(worker, runtimeHash, active, ttlMillis);
    }

    public RuntimeWorker requireEligible(String worker, String capability) {
        var found = db.get(worker);
        if (found == null
                || !found.live()
                || !found.state().equals("active")
                || found.protocolVersion() != PROTOCOL_VERSION
                || !found.runtimeHash().equals(runtimeHash)
                || !found.capabilities().contains(capability))
            throw new Blocked("worker_unavailable");
        return found;
    }

    public List<RuntimeWorker> list(int limit) {
        if (limit < 1 || limit > 100)
            throw new com.financeclaw.domain.Task.Problem(422, "invalid_page_limit");
        return List.copyOf(db.list(limit));
    }

    public void stop(String worker) {
        db.stop(worker);
    }
}
