package com.financeclaw.architecture;

import static org.mockito.Mockito.*;
import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.*;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.*;

import com.fasterxml.jackson.databind.ObjectMapper;
import com.fasterxml.jackson.databind.PropertyNamingStrategies;
import com.financeclaw.api.*;
import com.financeclaw.app.memory.MemorySearchService;
import com.financeclaw.app.memory.MemoryService;
import com.financeclaw.app.memory.port.MemoryRepository;
import com.financeclaw.bootstrap.HttpRequestConfiguration;
import com.financeclaw.domain.ExecutionScope;
import com.financeclaw.domain.Memory.*;
import com.financeclaw.domain.Task.Owner;
import com.financeclaw.domain.memory.MemoryPolicy;
import java.util.List;
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

/**
 * Freezes the existing typed binding and validation order before moving checks to Bean Validation.
 */
class MemoryHttpContractTest {
  private static final String DECISION = "/v1/memory/fact/decision";
  private static final String FORGET = "/v1/memory/forget";
  private static final Owner OWNER = new Owner("contract-tenant", "contract-subject");
  private MemoryRepository db;
  private MemorySearchService search;
  private PlatformTransactionManager manager;
  private LocalValidatorFactoryBean validator;
  private MockMvc http;

  @BeforeEach
  void setup() {
    db = mock(MemoryRepository.class);
    search = mock(MemorySearchService.class);
    manager = mock(PlatformTransactionManager.class);
    when(manager.getTransaction(any())).thenAnswer(ignored -> new SimpleTransactionStatus());
    when(db.lock(OWNER)).thenReturn(privacy(1));
    when(db.fact("fact")).thenReturn(candidate(1));
    validator = new LocalValidatorFactoryBean();
    validator.afterPropertiesSet();
    http = http(true);
  }

  private MockMvc http(boolean enabled) {
    var json = new ObjectMapper().setPropertyNamingStrategy(PropertyNamingStrategies.SNAKE_CASE);
    var converter = new MappingJackson2HttpMessageConverter(json);
    new HttpRequestConfiguration().extendMessageConverters(List.of(converter));
    var service =
        new MemoryService(
            db, new TransactionTemplate(manager), new MemoryPolicy(enabled, false, "embedding-v1"));
    return MockMvcBuilders.standaloneSetup(new MemoryController(service, search))
        .setMessageConverters(converter)
        .setValidator(validator)
        .setControllerAdvice(new ApiErrors())
        .addFilters(new BearerIdentity("contract-test-only", OWNER.tenant(), OWNER.subject()))
        .build();
  }

  @AfterEach
  void closeValidator() {
    if (validator != null) validator.close();
  }

  @ParameterizedTest
  @ValueSource(strings = {DECISION, FORGET})
  void authenticationPrecedesMalformedBodies(String path) throws Exception {
    http.perform(post(path).contentType("application/json").content("{"))
        .andExpect(status().isUnauthorized())
        .andExpect(content().json("{\"error\":\"unauthorized\"}", JsonCompareMode.STRICT));
    verifyNoInteractions(db, manager, search);
  }

  @ParameterizedTest
  @MethodSource("bindingErrors")
  void bindingFailuresKeepInvalidRequestAndHideInput(String path, String body) throws Exception {
    error(path, body, null, 422, "invalid_request");
    verifyNoInteractions(db, manager, search);
  }

  static Stream<Arguments> bindingErrors() {
    var common =
        Stream.of(DECISION, FORGET)
            .flatMap(
                path ->
                    Stream.of(
                            "",
                            " ",
                            "null",
                            "[]",
                            "\"private-input-canary\"",
                            "true",
                            "42",
                            "{",
                            "{\"unknown\":\"private-input-canary\"}")
                        .map(body -> Arguments.of(path, body)));
    var decisions =
        Stream.of(
                "{\"revision\":true,\"accept\":true}",
                "{\"revision\":[],\"accept\":true}",
                "{\"revision\":{},\"accept\":true}",
                "{\"revision\":\"1.0\",\"accept\":true}",
                "{\"revision\":9223372036854775808,\"accept\":true}",
                "{\"revision\":-9223372036854775809,\"accept\":true}",
                "{\"revision\":\"9223372036854775808\",\"accept\":true}",
                "{\"revision\":1,\"accept\":[]}",
                "{\"revision\":1,\"accept\":{}}",
                "{\"revision\":1,\"accept\":\"private-input-canary\"}",
                "{\"revision\":1,\"accept\":true,\"secret\":\"private-input-canary\"}",
                "{\"revision\":0,\"accept\":true,")
            .map(body -> Arguments.of(DECISION, body));
    var forgets =
        Stream.of(
                "{\"epoch\":true}",
                "{\"epoch\":[]}",
                "{\"epoch\":{}}",
                "{\"epoch\":\"1.0\"}",
                "{\"epoch\":9223372036854775808}",
                "{\"epoch\":-9223372036854775809}",
                "{\"epoch\":\"9223372036854775808\"}",
                "{\"epoch\":1,\"secret\":\"private-input-canary\"}",
                "{\"epoch\":0,")
            .map(body -> Arguments.of(FORGET, body));
    return Stream.of(common, decisions, forgets).flatMap(stream -> stream);
  }

  @ParameterizedTest
  @MethodSource("invalidDecisions")
  void decisionConstraintsPrecedeFeatureFlagAndKeyValidation(String body) throws Exception {
    http = http(false);
    error(DECISION, body, null, 422, "invalid_memory_decision");
    verifyNoInteractions(db, manager, search);
  }

  static Stream<String> invalidDecisions() {
    return Stream.of(
        "{}",
        "{\"accept\":true}",
        "{\"revision\":null,\"accept\":true}",
        "{\"revision\":0,\"accept\":true}",
        "{\"revision\":-1,\"accept\":true}",
        "{\"revision\":\"\",\"accept\":true}",
        "{\"revision\":\"  \",\"accept\":true}",
        "{\"revision\":0.9,\"accept\":true}",
        "{\"revision\":1}",
        "{\"revision\":1,\"accept\":null}",
        "{\"revision\":1,\"accept\":\"\"}");
  }

  @ParameterizedTest
  @ValueSource(
      strings = {
        "{}",
        "{\"epoch\":null}",
        "{\"epoch\":0}",
        "{\"epoch\":-1}",
        "{\"epoch\":\"\"}",
        "{\"epoch\":\"  \"}",
        "{\"epoch\":0.9}"
      })
  void epochConstraintPrecedesFeatureFlagAndKeyValidation(String body) throws Exception {
    http = http(false);
    error(FORGET, body, null, 422, "invalid_memory_epoch");
    verifyNoInteractions(db, manager, search);
  }

  @ParameterizedTest
  @MethodSource("invalidKeys")
  void validBodiesCheckKeyBeforeOpeningTransaction(String key) throws Exception {
    error(DECISION, "{\"revision\":1,\"accept\":true}", key, 422, "invalid_idempotency_key");
    error(FORGET, "{\"epoch\":1}", key, 422, "invalid_idempotency_key");
    verifyNoInteractions(db, manager, search);
  }

  static Stream<String> invalidKeys() {
    return Stream.of(null, "", " ", "\t", "k".repeat(257));
  }

  @Test
  void featureFlagStillPrecedesKeyValidationForValidBodies() throws Exception {
    http = http(false);
    error(DECISION, "{\"revision\":1,\"accept\":true}", null, 503, "memory_disabled");
    error(FORGET, "{\"epoch\":1}", null, 503, "memory_disabled");
    verifyNoInteractions(db, manager, search);
  }

  @ParameterizedTest
  @MethodSource("acceptedDecisions")
  void acceptedDecisionBindingPreservesCoercionAndOpaqueKey(
      String body, long revision, boolean accept) throws Exception {
    when(db.fact("fact")).thenReturn(candidate(revision));
    String key = " opaque decision key ";
    String result = accept ? "active" : "rejected";
    http.perform(request(DECISION, body, key).contentType("application/vnd.financeclaw+json"))
        .andExpect(status().isOk())
        .andExpect(content().json("{\"status\":\"" + result + "\"}", JsonCompareMode.STRICT));
    verify(db).status("fact", result);
    verify(db)
        .recordOperation(
            OWNER,
            key,
            ExecutionScope.hash(
                List.of("decide", "fact", Long.toString(revision), Boolean.toString(accept))),
            result);
    verify(db, times(accept ? 1 : 0)).indexJob("fact", "embedding-v1");
  }

  static Stream<Arguments> acceptedDecisions() {
    return Stream.of(
        Arguments.of("{\"revision\":1,\"accept\":true}", 1L, true),
        Arguments.of("{\"revision\":\"1\",\"accept\":\"false\"}", 1L, false),
        Arguments.of("{\"revision\":1.9,\"accept\":1}", 1L, true),
        Arguments.of("{\"revision\":1e0,\"accept\":0}", 1L, false),
        Arguments.of("{\"revision\":\" 1 \",\"accept\":\" TRUE \"}", 1L, true),
        Arguments.of("{\"revision\":9223372036854775807,\"accept\":false}", Long.MAX_VALUE, false),
        Arguments.of("{\"revision\":1,\"accept\":true} [", 1L, true));
  }

  @ParameterizedTest
  @ValueSource(
      strings = {
        "{\"epoch\":1}",
        "{\"epoch\":\"1\"}",
        "{\"epoch\":\" 1 \"}",
        "{\"epoch\":1.9}",
        "{\"epoch\":1e0}",
        "{\"epoch\":1} ["
      })
  void acceptedForgetBindingPreservesCoercionAndOpaqueKey(String body) throws Exception {
    String key = " opaque forget key ";
    http.perform(request(FORGET, body, key).contentType("application/vnd.financeclaw+json"))
        .andExpect(status().isOk())
        .andExpect(content().json("{\"epoch\":2}", JsonCompareMode.STRICT));
    verify(db).forget(OWNER);
    verify(db).eraseSources(OWNER);
    verify(db).eraseFacts(OWNER);
    verify(db).eraseRecalls(OWNER);
    verify(db).eraseIndex(OWNER);
    verify(db).eraseJobs(OWNER);
    verify(db).recordOperation(OWNER, key, ExecutionScope.hash(List.of("forget", "1")), "2");
  }

  @Test
  void maximumKeyLengthRemainsAccepted() throws Exception {
    String key = "k".repeat(256);
    http.perform(request(DECISION, "{\"revision\":1,\"accept\":false}", key))
        .andExpect(status().isOk());
    verify(db).operation(OWNER, key);
  }

  @Test
  void forgetReplayRemainsValidAfterPrivacyEpochAdvances() throws Exception {
    when(db.lock(OWNER)).thenReturn(privacy(2));
    when(db.operation(OWNER, "replay"))
        .thenReturn(new Operation(ExecutionScope.hash(List.of("forget", "1")), "2"));
    http.perform(request(FORGET, "{\"epoch\":1}", "replay"))
        .andExpect(status().isOk())
        .andExpect(content().json("{\"epoch\":2}", JsonCompareMode.STRICT));
    verify(db, never()).forget(any());
    verify(db, never()).eraseFacts(any());
  }

  @Test
  void dynamicConflictsRemainApplicationErrorsAfterValidBinding() throws Exception {
    when(db.fact("fact")).thenReturn(candidate(2));
    error(DECISION, "{\"revision\":1,\"accept\":true}", "decision", 409, "stale_memory_candidate");
    when(db.lock(OWNER)).thenReturn(privacy(2));
    error(FORGET, "{\"epoch\":1}", "forget", 409, "memory_epoch_conflict");
    verify(db, never()).status(anyString(), anyString());
    verify(db, never()).forget(any());
  }

  @Test
  void unexpectedFailuresNeverExposeRepositoryOrInputDetails() throws Exception {
    when(db.lock(OWNER)).thenThrow(new IllegalStateException("private-input-canary"));
    error(FORGET, "{\"epoch\":1}", "private-input-canary", 503, "service_unavailable");
  }

  private void error(String path, String body, String key, int status, String code)
      throws Exception {
    http.perform(request(path, body, key))
        .andExpect(status().is(status))
        .andExpect(content().json("{\"error\":\"" + code + "\"}", JsonCompareMode.STRICT));
  }

  private static MockHttpServletRequestBuilder request(String path, String body, String key) {
    var request =
        post(path)
            .header("Authorization", "Bearer contract-test-only")
            .contentType("application/json")
            .content(body);
    if (key != null) request.header("Idempotency-Key", key);
    return request;
  }

  private static Privacy privacy(long epoch) {
    return new Privacy(OWNER.tenant(), OWNER.subject(), epoch, false);
  }

  private static Fact candidate(long revision) {
    return new Fact(
        "fact",
        "source",
        OWNER.tenant(),
        OWNER.subject(),
        1,
        "response_language",
        "中文",
        "private-input-canary",
        revision,
        "candidate");
  }
}
