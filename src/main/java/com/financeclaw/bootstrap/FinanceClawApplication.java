package com.financeclaw.bootstrap;

import org.springframework.boot.SpringApplication;
import org.springframework.boot.SpringBootConfiguration;
import org.springframework.boot.autoconfigure.EnableAutoConfiguration;
import org.springframework.context.annotation.Import;
import org.springframework.scheduling.annotation.EnableScheduling;

/** 应用主入口：HTTP、AgentScope 执行、SSE 和渠道投递共用一个业务进程。 */
@SpringBootConfiguration
@EnableAutoConfiguration
@Import(RoleConfiguration.Standalone.class)
@EnableScheduling
public class FinanceClawApplication {
    public static void main(String[] args) {
        SpringApplication.run(FinanceClawApplication.class, args);
    }
}
