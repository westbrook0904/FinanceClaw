package com.financeclawsetup;

import javax.sql.DataSource;
import org.springframework.boot.*;
import org.springframework.boot.autoconfigure.EnableAutoConfiguration;
import org.springframework.core.io.ClassPathResource;
import org.springframework.jdbc.datasource.init.ResourceDatabasePopulator;

/**
 * 数据库结构入口：初始化新库或演进当前 Java 基线，并授予运行角色最小权限。
 *
 * <p>该进程不装配业务 Mapper、执行器或 HTTP 服务。结构所有者凭据只在这里使用， 普通运行进程不具备建表或更改结构的权限。Flyway 保留 V1 校验值并执行后续迁移，不导入
 * Python 或历史实验结构。
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
