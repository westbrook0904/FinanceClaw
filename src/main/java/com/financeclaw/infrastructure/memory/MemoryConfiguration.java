package com.financeclaw.infrastructure.memory;

import com.financeclaw.domain.memory.MemoryPolicy;
import org.springframework.context.annotation.*;
import org.springframework.core.env.Environment;

@Configuration(proxyBeanMethods = false)
public class MemoryConfiguration {
  @Bean
  MemoryPolicy memoryPolicy(Environment env) {
    return new MemoryPolicy(
        env.getProperty("financeclaw.memory.enabled", Boolean.class, false),
        env.getProperty("financeclaw.memory.vector-search", Boolean.class, false),
        env.getProperty("financeclaw.memory.embedding.revision", "disabled"));
  }
}
