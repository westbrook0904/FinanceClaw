package com.financeclaw.architecture;

import static org.assertj.core.api.Assertions.assertThat;
import static org.mockito.Mockito.*;
import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.*;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.*;

import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import com.financeclaw.api.*;
import com.financeclaw.app.memory.MemorySearchService;
import com.financeclaw.app.memory.MemoryService;
import com.financeclaw.app.operations.AdmissionService;
import com.financeclaw.app.operations.OperationsQueryService;
import com.financeclaw.app.task.CancelTaskService;
import com.financeclaw.app.task.SubmitTaskService;
import com.financeclaw.app.task.TaskQueryService;
import com.financeclaw.bootstrap.HttpRequestConfiguration;
import com.financeclaw.domain.Operations.Receipt;
import com.financeclaw.domain.Task.Accepted;
import com.financeclaw.domain.Task.Owner;
import jakarta.validation.Validator;
import javax.sql.DataSource;
import org.hibernate.validator.HibernateValidatorFactory;
import org.junit.jupiter.api.Test;
import org.springframework.boot.autoconfigure.AutoConfigurations;
import org.springframework.boot.autoconfigure.http.HttpMessageConvertersAutoConfiguration;
import org.springframework.boot.autoconfigure.jackson.JacksonAutoConfiguration;
import org.springframework.boot.autoconfigure.validation.ValidationAutoConfiguration;
import org.springframework.boot.autoconfigure.web.servlet.WebMvcAutoConfiguration;
import org.springframework.boot.test.context.runner.WebApplicationContextRunner;
import org.springframework.http.converter.HttpMessageNotReadableException;
import org.springframework.test.json.JsonCompareMode;
import org.springframework.test.web.servlet.MockMvc;
import org.springframework.test.web.servlet.request.MockHttpServletRequestBuilder;
import org.springframework.test.web.servlet.setup.MockMvcBuilders;
import org.springframework.validation.beanvalidation.LocalValidatorFactoryBean;
import org.springframework.web.bind.MethodArgumentNotValidException;
import org.springframework.web.bind.annotation.PostMapping;
import org.springframework.web.bind.annotation.RequestBody;
import org.springframework.web.bind.annotation.RestController;
import org.springframework.web.context.WebApplicationContext;

/** Real Boot MVC/validation wiring, without persistence or a database. */
class HttpValidationWiringTest {
  private static final Owner OWNER = new Owner("wiring-tenant", "wiring-subject");
  private static final String TOKEN = "validation-wiring-only";
  private final WebApplicationContextRunner runner =
      new WebApplicationContextRunner()
          .withConfiguration(
              AutoConfigurations.of(
                  JacksonAutoConfiguration.class,
                  HttpMessageConvertersAutoConfiguration.class,
                  ValidationAutoConfiguration.class,
                  WebMvcAutoConfiguration.class))
          .withPropertyValues(
              "spring.jackson.property-naming-strategy=SNAKE_CASE",
              "spring.jackson.deserialization.fail-on-unknown-properties=true")
          .withUserConfiguration(
              HttpRequestConfiguration.class,
              TaskController.class,
              OperationsController.class,
              MemoryController.class,
              ApiErrors.class,
              UnmigratedController.class)
          .withBean(SubmitTaskService.class, () -> mock(SubmitTaskService.class))
          .withBean(TaskQueryService.class, () -> mock(TaskQueryService.class))
          .withBean(CancelTaskService.class, () -> mock(CancelTaskService.class))
          .withBean(TaskEvents.class, () -> mock(TaskEvents.class))
          .withBean(AdmissionService.class, () -> mock(AdmissionService.class))
          .withBean(OperationsQueryService.class, () -> mock(OperationsQueryService.class))
          .withBean(MemoryService.class, () -> mock(MemoryService.class))
          .withBean(MemorySearchService.class, () -> mock(MemorySearchService.class));

  @Test
  void bootUsesHibernateValidatorAndRejectsMissingOrNullFieldsBeforeApplicationCalls() {
    runner.run(
        context -> {
          assertThat(context).hasNotFailed().doesNotHaveBean(DataSource.class);
          assertThat(context.getBean("defaultValidator"))
              .isInstanceOf(LocalValidatorFactoryBean.class);
          assertThat(
                  ((Validator) context.getBean("defaultValidator"))
                      .unwrap(HibernateValidatorFactory.class))
              .isNotNull();
          MockMvc http = http(context);
          for (String body : new String[] {"{}", "{\"message\":null}"}) {
            var result =
                http.perform(submit(body))
                    .andExpect(status().isUnprocessableEntity())
                    .andExpect(
                        content().json("{\"error\":\"invalid_body\"}", JsonCompareMode.STRICT))
                    .andReturn();
            assertThat(result.getResolvedException())
                .isInstanceOf(MethodArgumentNotValidException.class);
          }
          for (String body :
              new String[] {
                "{}",
                "{\"paused\":false}",
                "{\"revision\":2}",
                "{\"paused\":null,\"revision\":2}",
                "{\"paused\":false,\"revision\":null}"
              }) {
            var result =
                http.perform(admission(body))
                    .andExpect(status().isUnprocessableEntity())
                    .andExpect(
                        content().json("{\"error\":\"invalid_body\"}", JsonCompareMode.STRICT))
                    .andReturn();
            assertThat(result.getResolvedException())
                .isInstanceOf(MethodArgumentNotValidException.class);
          }
          verifyNoInteractions(
              context.getBean(SubmitTaskService.class), context.getBean(AdmissionService.class));
        });
  }

  @Test
  void structuredJsonMediaTypesUseTheProductionMapperAndPreserveResponseNames() {
    runner.run(
        context -> {
          var submissions = context.getBean(SubmitTaskService.class);
          var admissions = context.getBean(AdmissionService.class);
          when(submissions.submit(OWNER, "conversation-1", "wiring-key", "hello"))
              .thenReturn(new Accepted("conversation-1", "turn-1", "queued", 1, false));
          when(admissions.change(OWNER, "wiring-key", false, 2))
              .thenReturn(new Receipt(false, 3, false));
          MockMvc http = http(context);
          http.perform(
                  submit("{\"message\":\"hello\"}").contentType("application/vnd.financeclaw+json"))
              .andExpect(status().isAccepted())
              .andExpect(
                  content()
                      .json(
                          """
                          {"conversation_id":"conversation-1","turn_id":"turn-1",
                           "status":"queued","revision":1,"idempotent_replay":false}
                          """,
                          JsonCompareMode.STRICT));
          http.perform(
                  admission("{\"paused\":false,\"revision\":2}")
                      .contentType("application/vnd.financeclaw+json"))
              .andExpect(status().isOk())
              .andExpect(
                  content()
                      .json(
                          "{\"paused\":false,\"revision\":3,\"idempotent_replay\":false}",
                          JsonCompareMode.STRICT));
          http.perform(submit("{\"message\":42}").contentType("application/vnd.financeclaw+json"))
              .andExpect(status().isUnprocessableEntity())
              .andExpect(content().json("{\"error\":\"invalid_body\"}", JsonCompareMode.STRICT));
          http.perform(
                  admission("{\"paused\":\"false\",\"revision\":2}")
                      .contentType("application/vnd.financeclaw+json"))
              .andExpect(status().isUnprocessableEntity())
              .andExpect(content().json("{\"error\":\"invalid_body\"}", JsonCompareMode.STRICT));
          verify(submissions).submit(OWNER, "conversation-1", "wiring-key", "hello");
          verify(admissions).change(OWNER, "wiring-key", false, 2);
          verifyNoMoreInteractions(submissions, admissions);
        });
  }

  @Test
  void strictRequestMappersRejectScalarCoercionAndUnknownFieldsWithoutEchoingInput() {
    runner.run(
        context -> {
          MockMvc http = http(context);
          for (String body :
              new String[] {
                "{\"message\":42}",
                "{\"message\":true}",
                "{\"message\":[\"private-input-canary\"]}",
                "{\"message\":{\"private-input-canary\":true}}",
                "{\"message\":\"hello\",\"secret\":\"private-input-canary\"}"
              })
            http.perform(submit(body))
                .andExpect(status().isUnprocessableEntity())
                .andExpect(content().json("{\"error\":\"invalid_body\"}", JsonCompareMode.STRICT));
          for (String body :
              new String[] {
                "{\"paused\":\"true\",\"revision\":2}",
                "{\"paused\":1,\"revision\":2}",
                "{\"paused\":false,\"revision\":\"2\"}",
                "{\"paused\":false,\"revision\":2.0}",
                "{\"paused\":false,\"revision\":9223372036854775808}",
                "{\"paused\":false,\"revision\":2,\"secret\":\"private-input-canary\"}"
              })
            http.perform(admission(body))
                .andExpect(status().isUnprocessableEntity())
                .andExpect(content().json("{\"error\":\"invalid_body\"}", JsonCompareMode.STRICT));
          verifyNoInteractions(
              context.getBean(SubmitTaskService.class), context.getBean(AdmissionService.class));
        });
  }

  @Test
  void malformedJsonRemainsInvalidRequestAndDoesNotExposeParserDetails() {
    runner.run(
        context -> {
          MockMvc http = http(context);
          http.perform(submit("{\"message\":\"private-input-canary\","))
              .andExpect(status().isUnprocessableEntity())
              .andExpect(content().json("{\"error\":\"invalid_request\"}", JsonCompareMode.STRICT));
          http.perform(admission("{\"paused\":false,\"revision\":"))
              .andExpect(status().isUnprocessableEntity())
              .andExpect(content().json("{\"error\":\"invalid_request\"}", JsonCompareMode.STRICT));
          verifyNoInteractions(
              context.getBean(SubmitTaskService.class), context.getBean(AdmissionService.class));
        });
  }

  @Test
  void memoryConstraintsUseTheBootValidatorAndPreserveEndpointErrorCodes() {
    runner.run(
        context -> {
          MockMvc http = http(context);
          for (String body :
              new String[] {
                "{}",
                "{\"revision\":1}",
                "{\"revision\":1,\"accept\":null}",
                "{\"revision\":0,\"accept\":true}",
                "{\"revision\":-1,\"accept\":false}"
              }) {
            var result =
                http.perform(memoryDecision(body))
                    .andExpect(status().isUnprocessableEntity())
                    .andExpect(
                        content()
                            .json(
                                "{\"error\":\"invalid_memory_decision\"}", JsonCompareMode.STRICT))
                    .andReturn();
            assertThat(result.getResolvedException())
                .isInstanceOf(MethodArgumentNotValidException.class);
          }
          for (String body : new String[] {"{}", "{\"epoch\":0}", "{\"epoch\":-1}"}) {
            var result =
                http.perform(memoryForget(body))
                    .andExpect(status().isUnprocessableEntity())
                    .andExpect(
                        content()
                            .json("{\"error\":\"invalid_memory_epoch\"}", JsonCompareMode.STRICT))
                    .andReturn();
            assertThat(result.getResolvedException())
                .isInstanceOf(MethodArgumentNotValidException.class);
          }
          verifyNoInteractions(
              context.getBean(MemoryService.class), context.getBean(MemorySearchService.class));
        });
  }

  @Test
  void memoryNullAndMalformedBodiesRemainInvalidRequestBeforeBeanValidation() {
    runner.run(
        context -> {
          MockMvc http = http(context);
          for (String body :
              new String[] {"null", "{\"revision\":1,\"accept\":true,\"private-input-canary\":"}) {
            var result =
                http.perform(memoryDecision(body))
                    .andExpect(status().isUnprocessableEntity())
                    .andExpect(
                        content().json("{\"error\":\"invalid_request\"}", JsonCompareMode.STRICT))
                    .andReturn();
            assertThat(result.getResolvedException())
                .isInstanceOf(HttpMessageNotReadableException.class);
          }
          for (String body : new String[] {"null", "{\"epoch\":1,\"private-input-canary\":"}) {
            var result =
                http.perform(memoryForget(body))
                    .andExpect(status().isUnprocessableEntity())
                    .andExpect(
                        content().json("{\"error\":\"invalid_request\"}", JsonCompareMode.STRICT))
                    .andReturn();
            assertThat(result.getResolvedException())
                .isInstanceOf(HttpMessageNotReadableException.class);
          }
          verifyNoInteractions(
              context.getBean(MemoryService.class), context.getBean(MemorySearchService.class));
        });
  }

  @Test
  void memoryRequestsKeepExistingCoercionAndStructuredJsonMediaTypes() {
    runner.run(
        context -> {
          var memories = context.getBean(MemoryService.class);
          when(memories.decide(OWNER, "fact-1", 2, true, "wiring-key")).thenReturn("active");
          when(memories.decide(OWNER, "fact-1", 3, false, "wiring-key")).thenReturn("rejected");
          when(memories.forget(OWNER, 4, "wiring-key")).thenReturn(5L);
          when(memories.forget(OWNER, 6, "wiring-key")).thenReturn(7L);
          MockMvc http = http(context);
          http.perform(memoryDecision("{\"revision\":\"2\",\"accept\":\"true\"}"))
              .andExpect(status().isOk())
              .andExpect(content().json("{\"status\":\"active\"}", JsonCompareMode.STRICT));
          http.perform(
                  memoryDecision("{\"revision\":3.9,\"accept\":false}")
                      .contentType("application/vnd.financeclaw+json"))
              .andExpect(status().isOk())
              .andExpect(content().json("{\"status\":\"rejected\"}", JsonCompareMode.STRICT));
          http.perform(memoryForget("{\"epoch\":\"4\"}"))
              .andExpect(status().isOk())
              .andExpect(content().json("{\"epoch\":5}", JsonCompareMode.STRICT));
          http.perform(
                  memoryForget("{\"epoch\":6.9}").contentType("application/vnd.financeclaw+json"))
              .andExpect(status().isOk())
              .andExpect(content().json("{\"epoch\":7}", JsonCompareMode.STRICT));
          verify(memories).decide(OWNER, "fact-1", 2, true, "wiring-key");
          verify(memories).decide(OWNER, "fact-1", 3, false, "wiring-key");
          verify(memories).forget(OWNER, 4, "wiring-key");
          verify(memories).forget(OWNER, 6, "wiring-key");
          verifyNoMoreInteractions(memories);
          verifyNoInteractions(context.getBean(MemorySearchService.class));
        });
  }

  @Test
  void scopedMapperDoesNotChangeUnmigratedTypedOrTreeRequests() {
    runner.run(
        context -> {
          MockMvc http = http(context);
          String body = "{\"message\":42,\"paused\":\"true\",\"revision\":\"2\"}";
          http.perform(
                  post("/contract/unmigrated-typed").contentType("application/json").content(body))
              .andExpect(status().isOk())
              .andExpect(
                  content()
                      .json(
                          "{\"message\":\"42\",\"paused\":true,\"revision\":2}",
                          JsonCompareMode.STRICT));
          http.perform(
                  post("/contract/unmigrated-tree").contentType("application/json").content(body))
              .andExpect(status().isOk())
              .andExpect(content().json(body, JsonCompareMode.STRICT));
          http.perform(
                  post("/contract/unmigrated-typed")
                      .contentType("application/json")
                      .content("{\"unknown\":\"private-input-canary\"}"))
              .andExpect(status().isUnprocessableEntity())
              .andExpect(content().json("{\"error\":\"invalid_request\"}", JsonCompareMode.STRICT));
          assertThat(context.getBean(ObjectMapper.class).readValue(body, UnmigratedRequest.class))
              .isEqualTo(new UnmigratedRequest("42", true, 2L));
        });
  }

  private static MockMvc http(WebApplicationContext context) {
    return MockMvcBuilders.webAppContextSetup(context)
        .addFilters(new BearerIdentity(TOKEN, OWNER.tenant(), OWNER.subject()))
        .build();
  }

  private static MockHttpServletRequestBuilder submit(String body) {
    return post("/v1/conversations/conversation-1/turns")
        .header("Authorization", "Bearer " + TOKEN)
        .header("Idempotency-Key", "wiring-key")
        .contentType("application/json")
        .content(body);
  }

  private static MockHttpServletRequestBuilder admission(String body) {
    return put("/v1/operations/admission")
        .header("Authorization", "Bearer " + TOKEN)
        .header("Idempotency-Key", "wiring-key")
        .contentType("application/json")
        .content(body);
  }

  private static MockHttpServletRequestBuilder memoryDecision(String body) {
    return post("/v1/memory/fact-1/decision")
        .header("Authorization", "Bearer " + TOKEN)
        .header("Idempotency-Key", "wiring-key")
        .contentType("application/json")
        .content(body);
  }

  private static MockHttpServletRequestBuilder memoryForget(String body) {
    return post("/v1/memory/forget")
        .header("Authorization", "Bearer " + TOKEN)
        .header("Idempotency-Key", "wiring-key")
        .contentType("application/json")
        .content(body);
  }

  record UnmigratedRequest(String message, Boolean paused, Long revision) {}

  @RestController
  static class UnmigratedController {
    @PostMapping("/contract/unmigrated-typed")
    UnmigratedRequest typed(@RequestBody UnmigratedRequest body) {
      return body;
    }

    @PostMapping("/contract/unmigrated-tree")
    JsonNode tree(@RequestBody JsonNode body) {
      return body;
    }
  }
}
