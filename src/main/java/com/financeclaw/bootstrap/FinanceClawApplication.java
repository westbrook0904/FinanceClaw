package com.financeclaw.bootstrap;

import com.financeclaw.infrastructure.state.RuntimeMapper;
import org.mybatis.spring.annotation.MapperScan;
import org.springframework.boot.SpringApplication;
import org.springframework.boot.autoconfigure.SpringBootApplication;

@SpringBootApplication(scanBasePackages = "com.financeclaw")
@MapperScan(basePackageClasses = RuntimeMapper.class)
public class FinanceClawApplication {
  public static void main(String[] args) {
    SpringApplication.run(FinanceClawApplication.class, args);
  }
}
