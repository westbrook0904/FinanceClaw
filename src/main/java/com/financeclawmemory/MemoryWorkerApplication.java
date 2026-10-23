package com.financeclawmemory;

import com.financeclaw.app.memory.*;
import com.financeclaw.app.memory.port.MemoryRepository;
import com.financeclaw.infrastructure.agentscope.memory.AgentScopeMemoryModels;
import com.financeclaw.infrastructure.memory.*;
import com.financeclaw.infrastructure.memory.MemoryMapper;
import java.util.*;
import org.springframework.boot.*;
import org.springframework.boot.autoconfigure.EnableAutoConfiguration;
import org.springframework.context.annotation.*;
import org.springframework.core.env.Environment;
import org.springframework.scheduling.annotation.*;
import org.springframework.transaction.support.TransactionTemplate;

/**
 * 受限记忆处理进程：仅领取提取/索引/清理作业，不装配业务 HTTP 或任务执行能力。
 *
 * <p>启动时检查数据库角色权限与固定 embedding revision，避免错误凭据扩大访问范围， 或在模型版本变化时将旧作业写入新的向量投影。作业完成仍需 token、epoch
 * 与版本校验。
 */
@SpringBootConfiguration
@EnableAutoConfiguration
@Import({
    com.financeclaw.bootstrap.MapperConfiguration.Memory.class,
    AgentScopeMemoryModels.class,
    MyBatisMemoryRepository.class,
    MyBatisEmbeddingProfiles.class
})
@EnableScheduling
public class MemoryWorkerApplication {
    public static void main(String[] args) {
        var arguments = new ArrayList<String>(List.of(args));
        for (var pair :
                Map.of(
                                "url",
                                "FINANCECLAW_MEMORY_DATABASE_URL",
                                "username",
                                "FINANCECLAW_MEMORY_DATABASE_USER")
                        .entrySet()) {
            var value = System.getenv(pair.getValue());
            if (value == null || value.isBlank()) {
                throw new IllegalStateException("Required: " + pair.getValue());
            }
            arguments.add("--spring.datasource." + pair.getKey() + "=" + value);
        }
        // Compose uses Spring configtree secrets; retain the existing environment-based local
        // launcher.
        String password = System.getenv("FINANCECLAW_MEMORY_DATABASE_PASSWORD");
        if (password != null && !password.isBlank()) {
            arguments.add("--spring.datasource.password=" + password);
        }
        arguments.add("--spring.flyway.enabled=false");
        arguments.add(
                "--management.endpoint.health.group.readiness.include=readinessState,db,memoryStore");
        arguments.add(
                "--spring.main.web-application-type="
                        + ("true".equals(System.getenv("FINANCECLAW_MEMORY_HTTP_ENABLED"))
                                ? "servlet"
                                : "none"));
        SpringApplication.run(MemoryWorkerApplication.class, arguments.toArray(String[]::new));
    }

    @Bean
    org.springframework.boot.actuate.health.HealthIndicator memoryStoreHealthIndicator(
            MemoryMapper db) {
        return () -> {
            try {
                return db.isMemoryWorkerRoleRestricted()
                        ? org.springframework.boot.actuate.health.Health.up().build()
                        : org.springframework.boot.actuate.health.Health.down().build();
            } catch (RuntimeException e) {
                return org.springframework.boot.actuate.health.Health.down().build();
            }
        };
    }

    @Bean
    MemoryProcessingService processor(
            MemoryMapper db,
            MemoryRepository repository,
            TransactionTemplate tx,
            AgentScopeMemoryModels models,
            Environment env) {
        if (!db.isMemoryWorkerRoleRestricted()) {
            throw new IllegalStateException("Memory worker requires a restricted database role");
        }
        String revision = env.getProperty("financeclaw.memory.embedding.revision", "disabled");
        if (revision.equals("disabled") || revision.isBlank()) {
            throw new IllegalStateException("A pinned embedding revision is required");
        }
        return new MemoryProcessingService(repository, tx, models, models, revision);
    }

    @Bean
    Poller poller(MemoryProcessingService processor) {
        return new Poller(processor);
    }

    public record Poller(MemoryProcessingService processor) {
        @Scheduled(fixedDelayString = "${financeclaw.memory.poll-ms:1000}")
        public void poll() {
            processor.poll();
        }
    }
}
