package com.financeclaw.m0;

import static org.assertj.core.api.Assertions.*;

import com.financeclaw.bootstrap.FinanceClawApplication;
import com.financeclaw.infrastructure.state.RuntimeMapper;
import com.financeclaw.m0.support.ExecutionGate;
import com.financeclaw.m0.support.M0RuntimeMapper;
import org.apache.ibatis.session.SqlSessionFactory;
import org.junit.jupiter.api.Test;
import org.mybatis.spring.SqlSessionTemplate;
import org.mybatis.spring.boot.autoconfigure.ConfigurationCustomizer;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.boot.test.context.SpringBootTest;
import org.springframework.boot.test.context.TestConfiguration;
import org.springframework.boot.test.web.client.TestRestTemplate;
import org.springframework.context.annotation.Bean;
import org.springframework.context.annotation.Import;
import org.springframework.test.context.DynamicPropertyRegistry;
import org.springframework.test.context.DynamicPropertySource;

@SpringBootTest(
    classes = FinanceClawApplication.class,
    webEnvironment = SpringBootTest.WebEnvironment.RANDOM_PORT)
@Import(ApplicationIT.ProbeConfiguration.class)
class ApplicationIT extends PostgresSupport {
  @Autowired TestRestTemplate client;
  @Autowired RuntimeMapper runtimeMapper;
  @Autowired SqlSessionFactory sessions;
  @Autowired SqlSessionTemplate database;

  @TestConfiguration
  static class ProbeConfiguration {
    @Bean
    ConfigurationCustomizer m0Mapper() {
      return configuration -> configuration.addMapper(M0RuntimeMapper.class);
    }
  }

  @DynamicPropertySource
  static void database(DynamicPropertyRegistry registry) {
    registry.add("spring.datasource.url", POSTGRES::getJdbcUrl);
    registry.add("spring.datasource.username", POSTGRES::getUsername);
    registry.add("spring.datasource.password", POSTGRES::getPassword);
  }

  @Test
  void bootStartsAgainstMigratedPostgresAndExposesReadiness() {
    var response = client.getForEntity("/actuator/health/readiness", String.class);
    assertThat(response.getStatusCode().value()).isEqualTo(200);
    assertThat(response.getBody()).contains("UP");
    assertThat(database.getMapper(M0RuntimeMapper.class).hasPendingEffect("missing-scope"))
        .isFalse();
    var scope = scope();
    try (var owned = ExecutionGate.acquire(sessions, scope)) {
      assertThatThrownBy(() -> ExecutionGate.acquire(sessions, scope))
          .isInstanceOf(IllegalStateException.class)
          .hasMessageContaining("already active");
    }
    try (var released = ExecutionGate.acquire(sessions, scope)) {
      assertThat(released).isNotNull();
    }
  }
}
