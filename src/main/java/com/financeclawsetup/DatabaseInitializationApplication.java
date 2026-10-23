package com.financeclawsetup;

import javax.sql.DataSource;
import org.springframework.boot.*;
import org.springframework.boot.autoconfigure.EnableAutoConfiguration;
import org.springframework.core.io.ClassPathResource;
import org.springframework.jdbc.datasource.init.ResourceDatabasePopulator;

/**
 * 新库初始化入口：一次性创建应用结构并授予运行角色最小权限。
 *
 * <p>该进程不装配业务 Mapper、执行器或 HTTP 服务。结构所有者凭据只在这里使用， 普通运行进程不具备建表或更改结构的权限。Flyway 用来记录初始化是否完成，不承接旧库升级。
 */
@SpringBootConfiguration
@EnableAutoConfiguration(
        exclude = org.mybatis.spring.boot.autoconfigure.MybatisAutoConfiguration.class)
public class DatabaseInitializationApplication {
    public static void main(String[] args) {
        var application = new SpringApplication(DatabaseInitializationApplication.class);
        application.setWebApplicationType(WebApplicationType.NONE);
        var arguments = new java.util.ArrayList<String>(java.util.List.of(args));
        arguments.add("--management.endpoint.health.group.readiness.include=db");
        try (var context = application.run(arguments.toArray(String[]::new))) {
            if (context.getEnvironment()
                    .getProperty("financeclaw.deployment.grants", Boolean.class, false)) {
                new ResourceDatabasePopulator(new ClassPathResource("deployment/grants.sql"))
                        .execute(context.getBean(DataSource.class));
            }
        }
    }
}
