package com.financeclawmigrate;

import javax.sql.DataSource;
import org.springframework.boot.*;
import org.springframework.boot.autoconfigure.EnableAutoConfiguration;
import org.springframework.core.io.ClassPathResource;
import org.springframework.jdbc.datasource.init.ResourceDatabasePopulator;

/** One-shot Flyway role. Runtime processes never need the schema owner's credential. */
@SpringBootConfiguration
@EnableAutoConfiguration(
    exclude = org.mybatis.spring.boot.autoconfigure.MybatisAutoConfiguration.class)
public class MigrationApplication {
  public static void main(String[] args) {
    var application = new SpringApplication(MigrationApplication.class);
    application.setWebApplicationType(WebApplicationType.NONE);
    var arguments = new java.util.ArrayList<String>(java.util.List.of(args));
    arguments.add("--management.endpoint.health.group.readiness.include=db");
    try (var context = application.run(arguments.toArray(String[]::new))) {
      if (context
          .getEnvironment()
          .getProperty("financeclaw.deployment.grants", Boolean.class, false))
        new ResourceDatabasePopulator(new ClassPathResource("deployment/grants.sql"))
            .execute(context.getBean(DataSource.class));
    }
  }
}
