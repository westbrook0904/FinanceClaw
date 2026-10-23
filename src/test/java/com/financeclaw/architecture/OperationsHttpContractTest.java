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

/** Current operations HTTP contracts; PostgreSQL integration tests verify locks and rollback. */
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
        when(tasks.getAdmission(owner)).thenReturn(new Admission(true, 2));
        when(tasks.getAdmissionForUpdate(owner)).thenReturn(new Admission(true, 2));
        when(tasks.updateAdmissionByRevision(owner, false, 2)).thenReturn(1);
        when(operations.getTaskStatistics(owner))
                .thenReturn(Map.of("turns", 3L, "oldest_queued_ms", new BigDecimal("12.5")));
        when(operations.getCallStatistics(owner))
                .thenReturn(Map.of("model_calls", 4L, "tool_calls", 2L));
        when(operations.getNotificationStatistics(owner))
                .thenReturn(
                        Map.of(
                                "pending",
                                0L,
                                "sending",
                                0L,
                                "unknown",
                                1L,
                                "submitted_unverified",
                                0L));
        when(operations.getDrainSummary(owner))
                .thenAnswer(
                        ignored ->
                                new HashMap<>(
                                        Map.of("paused", true, "revision", 2L, "queued", 1L)));
        when(operations.listTurnBacklog(owner, "", 2))
                .thenReturn(
                        List.of(
                                new TurnBacklog("t-1", "c-1", "blocked", 7, null, "blocked", 1),
                                new TurnBacklog("t-2", "c-2", "queued", 1, null, "ready", 0)));
        when(operations.listNotificationBacklog(owner, "", 2))
                .thenReturn(List.of(new NotificationBacklog("n-1", "c-1", "t-1", "unknown", 7)));
        when(operations.listBindingBacklog(owner, "", 2))
                .thenReturn(List.of(new BindingBacklog("b-1", "c-1", "t-1", "blocked", 7)));
        when(operations.listMemoryJobBacklog(owner, 0, 2))
                .thenReturn(List.of(new MemoryBacklog("10", "purge", "pending", 3, 8, 2, null)));
        var manager = mock(PlatformTransactionManager.class);
        when(manager.getTransaction(any())).thenReturn(new SimpleTransactionStatus());
        var converter =
                new MappingJackson2HttpMessageConverter(
                        new ObjectMapper()
                                .setPropertyNamingStrategy(PropertyNamingStrategies.SNAKE_CASE));
        new HttpRequestConfiguration().extendMessageConverters(List.of(converter));
        validator = new LocalValidatorFactoryBean();
        validator.afterPropertiesSet();
        http =
                MockMvcBuilders.standaloneSetup(
                                new OperationsController(
                                        new AdmissionService(
                                                new MyBatisAdmissionGateway(tasks),
                                                new TransactionTemplate(manager)),
                                        new OperationsQueryService(
                                                new MyBatisOperationsReadGateway(operations))))
                        .setMessageConverters(converter)
                        .setValidator(validator)
                        .setControllerAdvice(new ApiErrors())
                        .addFilters(
                                new BearerIdentity(
                                        "contract-test-only", owner.tenant(), owner.subject()))
                        .build();
    }

    @AfterEach
    void closeValidator() {
        if (validator != null) validator.close();
    }

    @Test
    void overviewAdmissionAndDrainExposeOnlyCurrentResponseFields() throws Exception {
        for (var path :
                Map.of("overview", "", "admission", "/admission", "drain", "/drain").entrySet()) {
            http.perform(
                            get("/v1/operations" + path.getValue())
                                    .header("Authorization", "Bearer contract-test-only"))
                    .andExpect(status().isOk())
                    .andExpect(
                            content()
                                    .json(
                                            expected.get(path.getKey()).toString(),
                                            JsonCompareMode.STRICT));
        }
        http.perform(
                        put("/v1/operations/admission")
                                .header("Authorization", "Bearer contract-test-only")
                                .header("Idempotency-Key", "resume")
                                .contentType("application/json")
                                .content("{\"paused\":false,\"revision\":2}"))
                .andExpect(status().isOk())
                .andExpect(
                        content().json(expected.get("receipt").toString(), JsonCompareMode.STRICT));
    }

    @ParameterizedTest
    @ValueSource(strings = {"turns", "notifications", "notification_bindings", "memory_jobs"})
    void backlogShapeIncludesNullableFieldsAndItsOwnerScopedCursor(String kind) throws Exception {
        http.perform(
                        get("/v1/operations/drain/items")
                                .param("kind", kind)
                                .param("limit", "1")
                                .header("Authorization", "Bearer contract-test-only"))
                .andExpect(status().isOk())
                .andExpect(content().json(expected.get(kind).toString(), JsonCompareMode.STRICT));
    }

    @Test
    void authenticationAndTypedBodyValidationDoNotAccessRepositories() throws Exception {
        for (String path : List.of("", "/admission", "/drain", "/drain/items"))
            http.perform(get("/v1/operations" + path))
                    .andExpect(status().isUnauthorized())
                    .andExpect(
                            content().json("{\"error\":\"unauthorized\"}", JsonCompareMode.STRICT));
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
                    .andExpect(
                            content().json("{\"error\":\"invalid_body\"}", JsonCompareMode.STRICT));
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
    void admissionBindingDistinguishesTypedBodiesFromSyntaxErrors(String body, String code)
            throws Exception {
        error(body, null, code);
        verifyNoInteractions(tasks);
    }

    static Stream<Arguments> admissionBodyErrors() {
        var shapes =
                Stream.of(
                                "[]",
                                "\"text\"",
                                "true",
                                "42",
                                " \t\n",
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
                                "{\"paused\":false,\"revision\":2,\"tenant\":\"injected\"}")
                        .map(body -> Arguments.of(body, "invalid_body"));
        var syntax =
                Stream.of("", "null", "{", "{\"paused\":false,\"revision\":")
                        .map(body -> Arguments.of(body, "invalid_request"));
        return Stream.concat(shapes, syntax);
    }

    @ParameterizedTest
    @MethodSource("invalidKeys")
    void invalidKeyPrecedesRevisionAndDatabaseAccess(String key) throws Exception {
        for (long revision : List.of(-1L, Long.MAX_VALUE)) {
            error(
                    "{\"paused\":false,\"revision\":" + revision + "}",
                    key,
                    "invalid_idempotency_key");
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
                "{\"paused\":false,\"revision\":1,\"revision\":2}",
                "{\"paused\":false,\"revision\":null,\"revision\":2}"
            })
    void duplicateAdmissionFieldsAreRejectedWithoutWriting(String body) throws Exception {
        error(body, "resume", "invalid_request");
        verifyNoInteractions(tasks);
    }

    @ParameterizedTest
    @ValueSource(strings = {"{}", "[]", "null"})
    void admissionRejectsTrailingJsonValuesBeforeAnyWrite(String trailing) throws Exception {
        error("{\"paused\":false,\"revision\":2} " + trailing, "resume", "invalid_body");
        verifyNoInteractions(tasks);
    }

    @ParameterizedTest
    @MethodSource("acceptedKeys")
    void admissionPreservesOpaqueIdempotencyKey(String key) throws Exception {
        http.perform(request("{\"paused\":false,\"revision\":2}", key)).andExpect(status().isOk());
        verify(tasks).getAdmissionChangeByIdempotencyKey(owner, key);
        verify(tasks)
                .insertAdmissionChange(
                        owner, key, ExecutionScope.hash(List.of("false", "2")), false, 3);
    }

    static Stream<String> acceptedKeys() {
        return Stream.of(" key with surrounding spaces ", "k".repeat(256));
    }

    @Test
    void replayPrecedesCurrentRevisionAndDoesNotReapplyChange() throws Exception {
        when(tasks.getAdmissionForUpdate(owner)).thenReturn(new Admission(true, 9));
        when(tasks.getAdmissionChangeByIdempotencyKey(owner, "resume"))
                .thenReturn(new Change(ExecutionScope.hash(List.of("false", "2")), false, 3));
        http.perform(request("{\"paused\":false,\"revision\":2}", "resume"))
                .andExpect(status().isOk())
                .andExpect(
                        content()
                                .json(
                                        "{\"paused\":false,\"revision\":3,\"idempotent_replay\":true}",
                                        JsonCompareMode.STRICT));
        verify(tasks, never()).updateAdmissionByRevision(any(), anyBoolean(), anyLong());
        verify(tasks, never()).insertAdmissionChange(any(), any(), any(), anyBoolean(), anyLong());
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
