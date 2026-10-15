package com.financeclaw.m3;

import com.financeclaw.agent.TaskAgentRuntime;
import com.financeclaw.application.ReleaseCatalog;
import com.financeclaw.infrastructure.state.RuntimeMapper;
import com.financeclaw.m1.LifecycleDatabase;
import com.financeclaw.tools.*;
import com.financeclaw.worker.TaskWorker;
import java.nio.file.*;
import java.util.*;
import org.springframework.jdbc.datasource.DriverManagerDataSource;

public final class ForkedBusiness {
  public static void main(String[] args) throws Exception {
    var p = new Properties();
    try (var in = Files.newInputStream(Path.of(args[0]))) {
      p.load(in);
    }
    var db =
        new LifecycleDatabase(
            new DriverManagerDataSource(
                p.getProperty("url"), p.getProperty("user"), p.getProperty("password")));
    db.service
        .releases()
        .publish(
            new ReleaseCatalog.Release(
                "m3-tests",
                "finance_agent",
                ReleaseCatalog.SCOPES,
                ReleaseCatalog.OFFLINE,
                null,
                12,
                32,
                new BusinessCatalog.Features(
                    List.of(), BusinessCatalog.skillHash(), p.getProperty("market"))));
    var c = db.service.claim("fresh-business-jvm", 60000);
    try (var worker =
        new TaskWorker(
            db.service,
            new TaskAgentRuntime(
                db.service, db.governance, db.session.getMapper(RuntimeMapper.class), 20000),
            false,
            false,
            1,
            60000,
            2000)) {
      worker.execute(c);
    }
    var t =
        db.service.get(
            new com.financeclaw.domain.Task.Owner(c.turn().tenant(), c.turn().subject()),
            c.turn().conversationId(),
            c.turn().id());
    if (!t.status().equals(args[1]))
      throw new IllegalStateException("Unexpected business terminal status: " + t.status());
  }
}
