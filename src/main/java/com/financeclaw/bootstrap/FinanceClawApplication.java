package com.financeclaw.bootstrap;

import org.springframework.boot.SpringApplication;
import org.springframework.boot.SpringBootConfiguration;
import org.springframework.boot.autoconfigure.EnableAutoConfiguration;
import org.springframework.context.annotation.Import;
import org.springframework.scheduling.annotation.EnableScheduling;

@SpringBootConfiguration
@EnableAutoConfiguration
@Import({
  DeploymentAccess.class,
  RoleConfiguration.Api.class,
  RoleConfiguration.Worker.class,
  RoleConfiguration.Integrations.class,
  RoleConfiguration.Standalone.class
})
@EnableScheduling
public class FinanceClawApplication {
  public static void main(String[] args) {
    SpringApplication.run(FinanceClawApplication.class, args);
  }
}
