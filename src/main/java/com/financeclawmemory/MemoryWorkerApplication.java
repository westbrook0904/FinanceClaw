package com.financeclawmemory;

import com.financeclaw.infrastructure.memory.MemoryMapper;
import com.financeclaw.memory.*;
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
@Import(MemoryModels.class)
@EnableScheduling
public class MemoryWorkerApplication {
  public static void main(String[] args) {
    var arguments = new ArrayList<String>(List.of(args));
    for (var pair :
        Map.of(
                "url",
                "FINANCECLAW_MEMORY_DATABASE_URL",
                "username",
                "FINANCECLAW_MEMORY_DATABASE_USER",
                "password",
                "FINANCECLAW_MEMORY_DATABASE_PASSWORD")
            .entrySet()) {
      var value = System.getenv(pair.getValue());
      if (value == null || value.isBlank())
        throw new IllegalStateException("Required: " + pair.getValue());
      arguments.add("--spring.datasource." + pair.getKey() + "=" + value);
    }
    arguments.add("--spring.flyway.enabled=false");
    arguments.add("--management.endpoint.health.group.readiness.include=db");
    arguments.add("--spring.main.web-application-type=none");
    SpringApplication.run(MemoryWorkerApplication.class, arguments.toArray(String[]::new));
  }

  @Bean
  MemoryProcessor processor(
      MemoryMapper db, TransactionTemplate tx, MemoryModels models, Environment env) {
    if (!db.restrictedWorker())
      throw new IllegalStateException("Memory worker requires a restricted database role");
    String revision = env.getProperty("financeclaw.memory.embedding.revision", "disabled");
    if (revision.equals("disabled") || revision.isBlank())
      throw new IllegalStateException("A pinned embedding revision is required");
    return new MemoryProcessor(db, tx, models::extract, models::embedding, revision);
  }

  @Bean
  Poller poller(MemoryProcessor processor) {
    return new Poller(processor);
  }

  public record Poller(MemoryProcessor processor) {
    @Scheduled(fixedDelayString = "${financeclaw.memory.poll-ms:1000}")
    public void poll() {
      processor.poll();
    }
  }
}
