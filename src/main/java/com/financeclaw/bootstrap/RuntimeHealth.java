package com.financeclaw.bootstrap;

import com.financeclaw.infrastructure.task.TaskMapper;
import org.springframework.boot.actuate.health.Health;
import org.springframework.boot.actuate.health.HealthIndicator;
import org.springframework.stereotype.Component;

/**
 * 数据库运行权限与关键存储可读性的就绪探针。
 *
 * <p>探针只读取必要对象，不恢复任务或写入状态；异常统一呈现 DOWN，避免通过健康端点泄露数据库细节。
 */
@Component("runtimeStoreHealthIndicator")
public class RuntimeHealth implements HealthIndicator {
    private final TaskMapper db;

    public RuntimeHealth(TaskMapper db) {
        this.db = db;
    }

    @Override
    public Health health() {
        try {
            db.isRuntimeStateTableReadable();
            db.isArtifactTableReadable();
            db.isAdmissionTableReadable();
            db.listExpiredCommandIds();
            return Health.up().build();
        } catch (RuntimeException e) {
            return Health.down().build();
        }
    }
}
