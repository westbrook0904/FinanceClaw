package com.financeclaw.infrastructure.operations;

import com.financeclaw.app.operations.port.OperationsReadGateway;
import com.financeclaw.domain.Operations.*;
import com.financeclaw.domain.Task.Owner;
import com.financeclaw.infrastructure.task.OperationsMapper;
import java.util.Collections;
import java.util.List;
import java.util.TreeMap;
import org.springframework.stereotype.Repository;

/**
 * 实现 OperationsReadGateway，把 owner 范围的统计与积压映射为应用观测值。 排空查询只执行一次摘要
 * SQL，随后拆分受理状态和不可变计数，保留同一数据库快照。分页接口只返回运行元数据，不返回用户消息或通知正文。
 */
@Repository
public class MyBatisOperationsReadGateway implements OperationsReadGateway {
    private final OperationsMapper operations;

    public MyBatisOperationsReadGateway(OperationsMapper operations) {
        this.operations = operations;
    }

    @Override
    public Overview overview(Owner owner) {
        return new Overview(
                operations.getTaskStatistics(owner),
                operations.getCallStatistics(owner),
                operations.getNotificationStatistics(owner));
    }

    @Override
    public DrainSnapshot drain(Owner owner) {
        // Keep this as one SQL statement so admission and all counts share one PostgreSQL MVCC
        // snapshot.
        var row = operations.getDrainSummary(owner);
        var state =
                new Admission(
                        (Boolean) row.get("paused"), ((Number) row.get("revision")).longValue());
        var counts = new TreeMap<String, Long>();
        row.forEach(
                (key, value) -> {
                    if (!key.equals("paused") && !key.equals("revision")) {
                        counts.put(key, ((Number) value).longValue());
                    }
                });
        return new DrainSnapshot(state, Collections.unmodifiableMap(counts));
    }

    @Override
    public List<TurnBacklog> turns(Owner owner, String after, int limit) {
        return operations.listTurnBacklog(owner, after, limit);
    }

    @Override
    public List<NotificationBacklog> notifications(Owner owner, String after, int limit) {
        return operations.listNotificationBacklog(owner, after, limit);
    }

    @Override
    public List<BindingBacklog> bindings(Owner owner, String after, int limit) {
        return operations.listBindingBacklog(owner, after, limit);
    }

    @Override
    public List<MemoryBacklog> memoryJobs(Owner owner, long after, int limit) {
        return operations.listMemoryJobBacklog(owner, after, limit);
    }
}
