package com.financeclaw.m1;

import com.financeclaw.agent.TaskAgentRuntime;
import com.financeclaw.infrastructure.state.RuntimeMapper;
import java.nio.file.*;
import java.util.Properties;
import org.springframework.jdbc.datasource.DriverManagerDataSource;

/** Hard-exit probe using the production MyBatis service and real AgentScope runtime. */
public final class ForkedTask {
  public static void main(String[] args) throws Exception {
    Properties p = new Properties();
    try (var in = Files.newInputStream(Path.of(args[0]))) {
      p.load(in);
    }
    var db =
        new LifecycleDatabase(
            new DriverManagerDataSource(
                p.getProperty("url"), p.getProperty("user"), p.getProperty("password")));
    String mode = args[1];
    if (mode.equals("recover")) {
      db.service.recoverExpired();
      return;
    }
    var claim = db.service.claim("forked-worker", 60000);
    if (claim == null) throw new IllegalStateException("No queued command");
    if (mode.equals("after-claim")) Runtime.getRuntime().halt(73);
    var result =
        new TaskAgentRuntime(
                db.service, db.governance, db.session.getMapper(RuntimeMapper.class), 5000)
            .execute(claim);
    if (mode.equals("after-save")) Runtime.getRuntime().halt(73);
    db.service.seal(claim, result);
    if (mode.equals("after-seal")) Runtime.getRuntime().halt(73);
    db.service.finish(claim);
  }
}
