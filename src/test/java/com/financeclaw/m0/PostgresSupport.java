package com.financeclaw.m0;

import com.financeclaw.domain.ExecutionScope;
import com.financeclaw.infrastructure.state.RuntimeMapper;
import javax.sql.DataSource;
import org.flywaydb.core.Flyway;
import org.mybatis.spring.SqlSessionTemplate;
import org.springframework.jdbc.datasource.DriverManagerDataSource;
import org.testcontainers.containers.PostgreSQLContainer;

abstract class PostgresSupport {
  static final PostgreSQLContainer<?> POSTGRES =
      new PostgreSQLContainer<>("postgres:16.10-alpine")
          .withDatabaseName("financeclaw_m0")
          .withUsername("m0")
          .withPassword("m0-test-only");
  static final DataSource SOURCE;
  static final SqlSessionTemplate DATABASE;
  static final RuntimeMapper MAPPER;

  static {
    POSTGRES.start();
    SOURCE = source(POSTGRES.getJdbcUrl(), POSTGRES.getUsername(), POSTGRES.getPassword());
    Flyway.configure().dataSource(SOURCE).load().migrate();
    DATABASE = ProbeFixtures.database(SOURCE);
    MAPPER = DATABASE.getMapper(RuntimeMapper.class);
    DATABASE.getMapper(ProbeMapper.class).createExternalSystem();
    try {
      java.nio.file.Files.createDirectories(java.nio.file.Path.of("target/m0"));
      new com.fasterxml.jackson.databind.ObjectMapper()
          .writerWithDefaultPrettyPrinter()
          .writeValue(
              java.nio.file.Path.of("target/m0/environment.json").toFile(),
              java.util.Map.of(
                  "jdk",
                  System.getProperty("java.runtime.version"),
                  "vendor",
                  System.getProperty("java.vendor"),
                  "spring_boot",
                  org.springframework.boot.SpringBootVersion.getVersion(),
                  "postgres_image",
                  POSTGRES.getDockerImageName(),
                  "postgres_version",
                  DATABASE.getMapper(ProbeMapper.class).serverVersion()));
    } catch (java.io.IOException error) {
      throw new java.io.UncheckedIOException(error);
    }
  }

  static DataSource source(String url, String user, String password) {
    return new DriverManagerDataSource(url, user, password);
  }

  static ExecutionScope scope() {
    return new ExecutionScope(
        "tenant-a", "alice", java.util.UUID.randomUUID().toString(), "finance_agent", "m0-r1");
  }
}
