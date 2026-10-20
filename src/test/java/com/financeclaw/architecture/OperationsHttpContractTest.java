package com.financeclaw.architecture;

import static org.mockito.Mockito.*;
import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.*;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.*;

import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import com.fasterxml.jackson.databind.PropertyNamingStrategies;
import com.financeclaw.api.*;
import com.financeclaw.app.operations.AdmissionService;
import com.financeclaw.app.operations.OperationsQueryService;
import com.financeclaw.bootstrap.HttpRequestConfiguration;
import com.financeclaw.domain.ExecutionScope;
import com.financeclaw.domain.Operations.*;
import com.financeclaw.domain.Task.Owner;
import com.financeclaw.infrastructure.operations.MyBatisAdmissionGateway;
import com.financeclaw.infrastructure.operations.MyBatisOperationsReadGateway;
import com.financeclaw.infrastructure.task.*;
import java.math.BigDecimal;
import java.util.*;
import java.util.stream.Stream;
import org.junit.jupiter.api.AfterEach;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.params.ParameterizedTest;
import org.junit.jupiter.params.provider.*;
import org.springframework.http.converter.json.MappingJackson2HttpMessageConverter;
import org.springframework.test.json.JsonCompareMode;
import org.springframework.test.web.servlet.MockMvc;
import org.springframework.test.web.servlet.request.MockHttpServletRequestBuilder;
import org.springframework.test.web.servlet.setup.MockMvcBuilders;
import org.springframework.transaction.PlatformTransactionManager;
import org.springframework.transaction.support.SimpleTransactionStatus;
import org.springframework.transaction.support.TransactionTemplate;
import org.springframework.validation.beanvalidation.LocalValidatorFactoryBean;

/** HTTP compatibility only; actual locks/rollback are verified by PostgreSQL integration tests. */
class OperationsHttpContractTest {
  private final Owner owner = new Owner("contract-tenant", "contract-subject");
  private MockMvc http;
  private JsonNode expected;
  private TaskMapper tasks;
  private LocalValidatorFactoryBean validator;

  @BeforeEach
  void setup() throws Exception {
    try (var in = getClass().getResourceAsStream("/architecture/operations-http.json")) {
      expected = new ObjectMapper().readTree(in);
    }
    tasks = mock(TaskMapper.class);
    var operations = mock(OperationsMapper.class);
    when(tasks.admission(owner)).thenReturn(new Admission(true, 2));
    when(tasks.lockAdmission(owner)).thenReturn(new Admission(true, 2));
    when(tasks.changeAdmission(owner, false, 2)).thenReturn(1);
    when(operations.tasks(owner))
        .thenReturn(Map.of("turns", 3L, "oldest_queued_ms", new BigDecimal("12.5")));
    when(operations.calls(owner)).thenReturn(Map.of("model_calls", 4L, "tool_calls", 2L));
    when(operations.outbox(owner)).thenReturn(Map.of("pending", 1L));
    when(operations.notifications(owner))
        .thenReturn(
            Map.of("pending", 0L, "sending", 0L, "unknown", 1L, "submitted_unverified", 0L));
    when(operations.drain(owner))
        .thenAnswer(
            ignored ->
                new HashMap<>(
                    Map.of("paused", true, "revision", 2L, "queued", 1L, "pending_outbox", 0L)));
    when(operations.turnBacklog(owner, "", 2))
        .thenReturn(
            List.of(
                new TurnBacklog("t-1", "c-1", "blocked", 7, null, "blocked", 1, 1, 0),
                new TurnBacklog("t-2", "c-2", "queued", 1, null, "ready", 0, 0, 0)));
    when(operations.notificationBacklog(owner, "", 2))
        .thenReturn(List.of(new NotificationBacklog("n-1", "c-1", "t-1", "unknown", 7)));
    when(operations.bindingBacklog(owner, "", 2))
        .thenReturn(List.of(new BindingBacklog("b-1", "c-1", "t-1", "blocked", 7)));
    when(operations.memoryBacklog(owner, 0, 2))
        .thenReturn(List.of(new MemoryBacklog("10", "purge", "pending", 3, 8, 2, null)));
    var manager = mock(PlatformTransactionManager.class);
    when(manager.getTransaction(any())).thenReturn(new SimpleTransactionStatus());
    var converter =
        new MappingJackson2HttpMessageConverter(
            new ObjectMapper().setPropertyNamingStrategy(PropertyNamingStrategies.SNAKE_CASE));
    new HttpRequestConfiguration().extendMessageConverters(List.of(converter));
    validator = new LocalValidatorFactoryBean();
    validator.afterPropertiesSet();
    http =
        MockMvcBuilders.standaloneSetup(
                new OperationsController(
                    new AdmissionService(
                        new MyBatisAdmissionGateway(tasks), new TransactionTemplate(manager)),
                    new OperationsQueryService(new MyBatisOperationsReadGateway(operations))))
            .setMessageConverters(converter)
            .setValidator(validator)
            .setControllerAdvice(new ApiErrors())
            .addFilters(new BearerIdentity("contract-test-only", owner.tenant(), owner.subject()))
            .build();
  }

  @AfterEach
  void closeValidator() {
    if (validator != null) validator.close();
  }

  @Test
  void overviewAdmissionAndDrainKeepExactResponseFields() throws Exception {
    for (var path :
        Map.of("overview", "", "admission", "/admission", "drain", "/drain").entrySet()) {
      http.perform(
              get("/v1/operations" + path.getValue())
                  .header("Authorization", "Bearer contract-test-only"))
          .andExpect(status().isOk())
          .andExpect(
              content().json(expected.get(path.getKey()).toString(), JsonCompareMode.STRICT));
    }
    http.perform(
            put("/v1/operations/admission")
                .header("Authorization", "Bearer contract-test-only")
                .header("Idempotency-Key", "resume")
                .contentType("application/json")
                .content("{\"paused\":false,\"revision\":2}"))
        .andExpect(status().isOk())
        .andExpect(content().json(expected.get("receipt").toString(), JsonCompareMode.STRICT));
  }

  @ParameterizedTest
  @ValueSource(strings = {"turns", "notifications", "notification_bindings", "memory_jobs"})
  void eachBacklogShapeKeepsNullsAndItsCursor(String kind) throws Exception {
    http.perform(
            get("/v1/operations/drain/items")
                .param("kind", kind)
                .param("limit", "1")
                .header("Authorization", "Bearer contract-test-only"))
        .andExpect(status().isOk())
        .andExpect(content().json(expected.get(kind).toString(), JsonCompareMode.STRICT));
  }

  @Test
  void authenticationAndStrictBodyErrorsKeepTheirStatusAndCode() throws Exception {
    for (String path : List.of("", "/admission", "/drain", "/drain/items"))
      http.perform(get("/v1/operations" + path))
          .andExpect(status().isUnauthorized())
          .andExpect(content().json("{\"error\":\"unauthorized\"}", JsonCompareMode.STRICT));
    for (String body :
        List.of(
            "{}",
            "{\"paused\":\"true\",\"revision\":2}",
            "{\"paused\":false,\"revision\":2,\"tenant\":\"injected\"}",
            "{\"paused\":false,\"revision\":9223372036854775808}"))
      http.perform(
              put("/v1/operations/admission")
                  .header("Authorization", "Bearer contract-test-only")
                  .header("Idempotency-Key", "resume")
                  .contentType("application/json")
                  .content(body))
          .andExpect(status().isUnprocessableEntity())
          .andExpect(content().json("{\"error\":\"invalid_body\"}", JsonCompareMode.STRICT));
  }

  @Test
  void authenticationPrecedesMalformedAdmissionBody() throws Exception {
    http.perform(put("/v1/operations/admission").contentType("application/json").content("{"))
        .andExpect(status().isUnauthorized())
        .andExpect(content().json("{\"error\":\"unauthorized\"}", JsonCompareMode.STRICT));
    verifyNoInteractions(tasks);
  }

  @ParameterizedTest
  @MethodSource("admissionBodyErrors")
  void admissionShapeAndSyntaxKeepDistinctCodesBeforeKeyValidation(String body, String code)
      throws Exception {
    error(body, null, code);
    verifyNoInteractions(tasks);
  }

  static Stream<Arguments> admissionBodyErrors() {
    var shapes =
        Stream.of(
                "null",
                "[]",
                "\"text\"",
                "true",
                "42",
                "{}",
                "{\"paused\":false}",
                "{\"revision\":2}",
                "{\"paused\":null,\"revision\":2}",
                "{\"paused\":\"false\",\"revision\":2}",
                "{\"paused\":0,\"revision\":2}",
                "{\"paused\":[],\"revision\":2}",
                "{\"paused\":{},\"revision\":2}",
                "{\"paused\":false,\"revision\":null}",
                "{\"paused\":false,\"revision\":\"2\"}",
                "{\"paused\":false,\"revision\":true}",
                "{\"paused\":false,\"revision\":[]}",
                "{\"paused\":false,\"revision\":{}}",
                "{\"paused\":false,\"revision\":2.0}",
                "{\"paused\":false,\"revision\":2e0}",
                "{\"paused\":false,\"revision\":9223372036854775808}",
                "{\"paused\":false,\"revision\":-9223372036854775809}",
                "{\"Paused\":false,\"revision\":2}",
                "{\"paused\":false,\"revision\":2,\"tenant\":\"injected\"}",
                "{\"paused\":false,\"paused\":\"false\",\"revision\":2}",
                "{\"paused\":false,\"revision\":2,\"revision\":null}",
                "{\"paused\":false,\"revision\":2,\"revision\":9223372036854775808}")
            .map(body -> Arguments.of(body, "invalid_body"));
    var syntax =
        Stream.of(
                "",
                " \t\n",
                "{",
                "{\"paused\":\"false\",",
                "{\"paused\":false,\"revision\":9223372036854775808,",
                "{\"tenant\":\"injected\",",
                "{\"paused\":false,\"revision\":")
            .map(body -> Arguments.of(body, "invalid_request"));
    return Stream.concat(shapes, syntax);
  }

  @ParameterizedTest
  @MethodSource("invalidKeys")
  void invalidKeyPrecedesRevisionAndDatabaseAccess(String key) throws Exception {
    for (long revision : List.of(-1L, Long.MAX_VALUE)) {
      error("{\"paused\":false,\"revision\":" + revision + "}", key, "invalid_idempotency_key");
    }
    verifyNoInteractions(tasks);
  }

  static Stream<String> invalidKeys() {
    return Stream.of(null, "", " ", "\t", "k".repeat(257));
  }

  @ParameterizedTest
  @ValueSource(longs = {Long.MIN_VALUE, -1, Long.MAX_VALUE})
  void revisionDomainErrorsRemainSeparateFromBodyErrors(long revision) throws Exception {
    error("{\"paused\":false,\"revision\":" + revision + "}", "resume", "invalid_revision");
    verifyNoInteractions(tasks);
  }

  @ParameterizedTest
  @ValueSource(
      strings = {
        "{\"paused\":true,\"paused\":false,\"revision\":2}",
        "{\"paused\":\"wrong\",\"paused\":false,\"revision\":2}",
        "{\"paused\":null,\"paused\":false,\"revision\":2}",
        "{\"paused\":false,\"revision\":1,\"revision\":2}",
        "{\"paused\":false,\"revision\":\"wrong\",\"revision\":2}",
        "{\"paused\":false,\"revision\":null,\"revision\":2}",
        "{\"paused\":false,\"revision\":{},\"revision\":2}",
        "{\"paused\":false,\"revision\":2.5,\"revision\":2}",
        "{\"paused\":false,\"revision\":9223372036854775808,\"revision\":2}",
        "{\"paused\":false,\"revision\":2} {}",
        "{\"paused\":false,\"revision\":2} ["
      })
  void duplicateFieldsUseLastValueAndTrailingTokensRemainUnchanged(String body) throws Exception {
    http.perform(request(body, "resume"))
        .andExpect(status().isOk())
        .andExpect(content().json(expected.get("receipt").toString(), JsonCompareMode.STRICT));
    verify(tasks).changeAdmission(owner, false, 2);
  }

  @ParameterizedTest
  @MethodSource("acceptedKeys")
  void admissionPreservesOpaqueIdempotencyKey(String key) throws Exception {
    http.perform(request("{\"paused\":false,\"revision\":2}", key)).andExpect(status().isOk());
    verify(tasks).admissionReplay(owner, key);
    verify(tasks).recordAdmission(owner, key, ExecutionScope.hash(List.of("false", "2")), false, 3);
  }

  static Stream<String> acceptedKeys() {
    return Stream.of(" key with surrounding spaces ", "k".repeat(256));
  }

  @Test
  void replayPrecedesCurrentRevisionAndDoesNotReapplyChange() throws Exception {
    when(tasks.lockAdmission(owner)).thenReturn(new Admission(true, 9));
    when(tasks.admissionReplay(owner, "resume"))
        .thenReturn(new Change(ExecutionScope.hash(List.of("false", "2")), false, 3));
    http.perform(request("{\"paused\":false,\"revision\":2}", "resume"))
        .andExpect(status().isOk())
        .andExpect(
            content()
                .json(
                    "{\"paused\":false,\"revision\":3,\"idempotent_replay\":true}",
                    JsonCompareMode.STRICT));
    verify(tasks, never()).changeAdmission(any(), anyBoolean(), anyLong());
    verify(tasks, never()).recordAdmission(any(), any(), any(), anyBoolean(), anyLong());
  }

  private MockHttpServletRequestBuilder request(String body, String key) {
    var request =
        put("/v1/operations/admission")
            .header("Authorization", "Bearer contract-test-only")
            .contentType("application/json")
            .content(body);
    return key == null ? request : request.header("Idempotency-Key", key);
  }

  private void error(String body, String key, String code) throws Exception {
    http.perform(request(body, key))
        .andExpect(status().isUnprocessableEntity())
        .andExpect(content().json("{\"error\":\"" + code + "\"}", JsonCompareMode.STRICT));
  }
}
