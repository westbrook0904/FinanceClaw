package com.financeclaw.m5;

import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.*;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.*;

import com.financeclaw.application.TaskService;
import com.financeclaw.bootstrap.FinanceClawApplication;
import com.financeclaw.domain.Task.Owner;
import org.junit.jupiter.api.Test;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.boot.test.autoconfigure.web.servlet.AutoConfigureMockMvc;
import org.springframework.boot.test.context.SpringBootTest;
import org.springframework.test.context.*;
import org.springframework.test.web.servlet.MockMvc;
import org.testcontainers.containers.PostgreSQLContainer;

@SpringBootTest(
    classes = FinanceClawApplication.class,
    properties = {
      "financeclaw.worker.enabled=false",
      "financeclaw.integrations.enabled=false",
      "financeclaw.auth.token=m5-test-only"
    })
@AutoConfigureMockMvc
class OperationsIT {
  static final PostgreSQLContainer<?> PG = new PostgreSQLContainer<>("postgres:16.10-alpine");

  static {
    PG.start();
  }

  @DynamicPropertySource
  static void database(DynamicPropertyRegistry r) {
    r.add("spring.datasource.url", PG::getJdbcUrl);
    r.add("spring.datasource.username", PG::getUsername);
    r.add("spring.datasource.password", PG::getPassword);
  }

  @Autowired MockMvc http;
  @Autowired TaskService tasks;

  @Test
  void operationsRequireAuthenticationAndOnlyCountTheCurrentOwnerWithoutContent() throws Exception {
    var owner = new Owner("local", "developer");
    var other = new Owner("another-tenant", "developer");
    tasks.submit(owner, tasks.createConversation(owner), "mine", "private-owner-text");
    tasks.submit(other, tasks.createConversation(other), "theirs", "other-private-text");
    http.perform(get("/v1/operations")).andExpect(status().isUnauthorized());
    http.perform(get("/v1/operations").header("Authorization", "Bearer m5-test-only"))
        .andExpect(status().isOk())
        .andExpect(jsonPath("$.tasks.turns").value(1))
        .andExpect(jsonPath("$.tasks.queued").value(1))
        .andExpect(jsonPath("$.calls.model_calls").value(0))
        .andExpect(jsonPath("$.provider_cost").value("unavailable"))
        .andExpect(
            content()
                .string(
                    org.hamcrest.Matchers.not(org.hamcrest.Matchers.containsString("private"))));
  }
}
