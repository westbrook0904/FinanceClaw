package com.financeclaw.infrastructure.runtime;

import com.financeclaw.app.runtime.port.WorkerRegistryGateway;
import com.financeclaw.domain.runtime.RuntimeWorker;
import java.util.Arrays;
import java.util.List;
import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.jdbc.core.RowMapper;

public final class JdbcWorkerRegistry implements WorkerRegistryGateway {
    private final JdbcTemplate sql;
    private static final String COLUMNS =
            "worker_id,protocol_version,runtime_hash,capabilities,parallelism,active_attempts,state,expires_at>clock_timestamp() AS live";
    private static final RowMapper<RuntimeWorker> ROW =
            (row, index) ->
                    new RuntimeWorker(
                            row.getString("worker_id"),
                            row.getInt("protocol_version"),
                            row.getString("runtime_hash"),
                            Arrays.asList((String[]) row.getArray("capabilities").getArray()),
                            row.getInt("parallelism"),
                            row.getInt("active_attempts"),
                            row.getString("state"),
                            row.getBoolean("live"));

    public JdbcWorkerRegistry(JdbcTemplate sql) {
        // 保留 Boot 为业务模板配置的超时等设置；连接和事务仍由同一 DataSource 管理。
        this.sql = java.util.Objects.requireNonNull(sql);
    }

    @Override
    public void register(String id, String hash, int parallelism, long ttl) {
        sql.update(
                "INSERT INTO business.runtime_workers(worker_id,protocol_version,runtime_hash,capabilities,parallelism,active_attempts,state,heartbeat_at,expires_at) "
                        + "VALUES(?,1,?,ARRAY['agentscope'],?,0,'active',clock_timestamp(),clock_timestamp()+?*interval '1 millisecond') ON CONFLICT(worker_id) DO NOTHING",
                id,
                hash,
                parallelism,
                ttl);
    }

    @Override
    public boolean heartbeat(String id, String hash, int active, long ttl) {
        return sql.update(
                        "WITH locked AS MATERIALIZED (SELECT worker_id,expires_at FROM business.runtime_workers WHERE worker_id=? FOR UPDATE) "
                                + "UPDATE business.runtime_workers w SET active_attempts=?,heartbeat_at=clock_timestamp(),expires_at=clock_timestamp()+?*interval '1 millisecond' "
                                + "FROM locked l WHERE w.worker_id=l.worker_id AND l.expires_at>clock_timestamp() AND w.runtime_hash=? AND w.protocol_version=1 AND w.state='active' AND ?>=0 AND ?<=w.parallelism",
                        id,
                        active,
                        ttl,
                        hash,
                        active,
                        active)
                == 1;
    }

    @Override
    public RuntimeWorker get(String id) {
        return sql
                .query(
                        "SELECT " + COLUMNS + " FROM business.runtime_workers WHERE worker_id=?",
                        ROW,
                        id)
                .stream()
                .findFirst()
                .orElse(null);
    }

    @Override
    public List<RuntimeWorker> list(int limit) {
        return sql.query(
                "SELECT "
                        + COLUMNS
                        + " FROM business.runtime_workers ORDER BY heartbeat_at DESC,worker_id LIMIT ?",
                ROW,
                limit);
    }

    @Override
    public void stop(String id) {
        sql.update(
                "UPDATE business.runtime_workers SET state='stopped',active_attempts=0,expires_at=clock_timestamp() WHERE worker_id=?",
                id);
    }
}
