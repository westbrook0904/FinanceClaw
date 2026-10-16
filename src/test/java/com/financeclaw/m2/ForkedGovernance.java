package com.financeclaw.m2;

import com.financeclaw.infrastructure.state.RuntimeMapper;
import com.financeclaw.m1.LifecycleDatabase;
import java.nio.file.*;
import java.util.Properties;
import org.springframework.jdbc.datasource.DriverManagerDataSource;

public final class ForkedGovernance {
  public static void main(String[] args) throws Exception {
    var p = new Properties();
    try (var in = Files.newInputStream(Path.of(args[0]))) {
      p.load(in);
    }
    var database =
        new LifecycleDatabase(
            new DriverManagerDataSource(
                p.getProperty("url"), p.getProperty("user"), p.getProperty("password")));
    database.releases.publish(
        new com.financeclaw.application.ReleaseCatalog.Release(
            "m2-v1",
            "finance_agent",
            com.financeclaw.application.ReleaseCatalog.SCOPES,
            com.financeclaw.application.ReleaseCatalog.OFFLINE,
            null,
            12,
            16));
    var c = database.service.claim("fresh-jvm", 60000);
    try (var worker =
        com.financeclaw.m1.TaskWorkerFixture.create(
            database,
            new ScriptedRuntime(
                database.service,
                database.governance,
                database.session.getMapper(RuntimeMapper.class),
                database.business),
            false,
            false,
            1,
            60000,
            2000)) {
      worker.execute(c);
    }
  }
}
