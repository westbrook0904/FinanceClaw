package com.financeclawmemory;

import com.financeclaw.app.memory.*;
import com.financeclaw.app.memory.port.MemoryRepository;
import com.financeclaw.infrastructure.agentscope.memory.AgentScopeMemoryModels;
import com.financeclaw.infrastructure.memory.*;
import com.financeclaw.infrastructure.memory.MemoryMapper;
import java.util.*;
import org.mybatis.spring.annotation.MapperScan;
import org.springframework.boot.*;
import org.springframework.boot.autoconfigure.EnableAutoConfiguration;
import org.springframework.context.annotation.*;
import org.springframework.core.env.Environment;
import org.springframework.scheduling.annotation.*;
import org.springframework.transaction.support.TransactionTemplate;

/** Separate entrypoint: no TaskService, channel, tool registration or business mapper. */
@SpringBootConfiguration
@EnableAutoConfiguration
@MapperScan("com.financeclaw.infrastructure.memory")
@Import({
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
      if (value == null || value.isBlank())
        throw new IllegalStateException("Required: " + pair.getValue());
      arguments.add("--spring.datasource." + pair.getKey() + "=" + value);
    }
    // Compose uses Spring configtree secrets; retain the existing environment-based local launcher.
    String password = System.getenv("FINANCECLAW_MEMORY_DATABASE_PASSWORD");
    if (password != null && !password.isBlank())
      arguments.add("--spring.datasource.password=" + password);
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
        return db.restrictedWorker()
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
    if (!db.restrictedWorker())
      throw new IllegalStateException("Memory worker requires a restricted database role");
    String revision = env.getProperty("financeclaw.memory.embedding.revision", "disabled");
    if (revision.equals("disabled") || revision.isBlank())
      throw new IllegalStateException("A pinned embedding revision is required");
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
