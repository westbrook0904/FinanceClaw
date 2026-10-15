package com.financeclaw.bootstrap;

import org.apache.ibatis.annotations.Mapper;
import org.mybatis.spring.annotation.MapperScan;
import org.springframework.boot.SpringApplication;
import org.springframework.boot.autoconfigure.SpringBootApplication;
import org.springframework.scheduling.annotation.EnableScheduling;

@SpringBootApplication(scanBasePackages = "com.financeclaw")
@MapperScan(basePackages = "com.financeclaw.infrastructure", annotationClass = Mapper.class)
@EnableScheduling
public class FinanceClawApplication {
  public static void main(String[] args) {
    SpringApplication.run(FinanceClawApplication.class, args);
  }
}
